"""Per-job context handed to an adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import structlog


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
    _stack: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False)

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
