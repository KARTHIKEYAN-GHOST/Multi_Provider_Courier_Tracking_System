"""Per-job context handed to an adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from .contracts import RawResponse
from .credentials import CredentialDenied, CredentialResolver, Secret
from .http import new_client

#: A provider answer kept as evidence is cut to this many characters.
MAX_EVIDENCE_CHARS = 100_000


@dataclass
class JobContext:
    """Everything an adapter may know about the execution it is running.

    Resources an adapter opens for a job (HTTP clients, later browser contexts) must be
    registered here. The SDK closes them when the job ends — on success, error, timeout and
    shutdown alike — so a failed or cancelled job cannot leak them.
    """

    job_id: str
    attempt: int
    execution_id: str
    #: Broker redelivery count: 0 on the first delivery of this message.
    delivery_count: int
    correlation_id: str
    log: structlog.stdlib.BoundLogger
    #: Opaque reference from the job message; exchanged for the secret by ``credentials()``.
    credential_ref: str | None = None
    _resolver: CredentialResolver | None = field(default=None, repr=False)
    _stack: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False)
    #: The provider answer the adapter is about to read (see ``keep_evidence``).
    evidence: RawResponse | None = field(default=None, repr=False)

    def keep_evidence(self, response: httpx.Response) -> None:
        """Remember the provider's answer before trying to read it.

        If reading it then fails (``ParseError``, ``SchemaDrift``), the worker sends this
        answer to the Engine with the failure, so that the adapter can be repaired against
        what the provider really sent. Call it for the tracking answer only, never for an
        answer that carries a secret (a login or token answer).
        """
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        text = response.text[:MAX_EVIDENCE_CHARS]
        body: Any = text
        if "json" in content_type and len(response.text) <= MAX_EVIDENCE_CHARS:
            try:
                body = json.loads(text)
            except ValueError:
                body = text
        self.evidence = RawResponse(content_type=content_type[:200] or "text/plain", body=body)

    async def credentials(self) -> Secret:
        """The provider credential for this job, fetched from the Engine.

        Call it when the secret is needed and keep it in a local variable. Do not log it,
        store it on the adapter, or put it into the result.
        """
        if self.credential_ref is None:
            raise CredentialDenied("the job carries no credential reference; none is stored for the provider")
        if self._resolver is None:
            raise CredentialDenied("this worker is not configured to resolve credentials")
        return await self._resolver.resolve(self.credential_ref, self.job_id, self.attempt)

    def http(self, **options: Any) -> httpx.AsyncClient:
        """An HTTP client for this job. It is closed when the job ends, however it ends."""
        client = new_client(**options)
        self._stack.push_async_callback(client.aclose)
        return client

    async def enter[T](self, resource: AbstractAsyncContextManager[T]) -> T:
        """Enter an async context manager; it is exited when the job ends."""
        return await self._stack.enter_async_context(resource)

    def on_cleanup(self, callback: Callable[..., Awaitable[Any]], *args: Any) -> None:
        """Register an async callback to run when the job ends (last registered runs first)."""
        self._stack.push_async_callback(callback, *args)

    async def _close(self, timeout_s: float) -> None:
        """Run all cleanup. Shielded: a cancelled job still releases what it opened."""
        closing = asyncio.ensure_future(self._stack.aclose())
        try:
            await asyncio.wait_for(asyncio.shield(closing), timeout_s)
        except TimeoutError:
            closing.cancel()
            self.log.error("cleanup_timed_out", timeout_s=timeout_s)
        except asyncio.CancelledError:
            # Cancelled while cleaning up: give the cleanup its bounded time anyway.
            try:
                await asyncio.wait_for(closing, timeout_s)
            except Exception:
                self.log.exception("cleanup_failed")
            raise
        except Exception:
            self.log.exception("cleanup_failed")
