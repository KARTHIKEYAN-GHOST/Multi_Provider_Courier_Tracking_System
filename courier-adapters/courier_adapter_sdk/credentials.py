"""Getting a provider credential for the job being executed.

Secrets never arrive in a message. ``job.track`` carries only an opaque ``credential_ref``;
the worker exchanges it for the secret over the Engine's internal HTTP API, identifying
itself with its worker token. The Engine allows this only for a job of this worker's adapter
that is currently in flight, and records every request in its audit log.

The secret lives in memory for the duration of one job. It is never logged, never cached
across jobs and never written to disk.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Protocol

import httpx

from .contracts import ErrorClass
from .errors import AdapterError


class CredentialDenied(AdapterError):
    """The Engine refused the credential, or this worker is not set up to ask for one.
    Trying again will not help until an operator has fixed the cause."""

    error_class = ErrorClass.AUTH_INVALID
    retryable = False


class CredentialServiceUnavailable(AdapterError):
    """The Engine's internal API could not be reached. Worth retrying."""

    error_class = ErrorClass.INTERNAL
    retryable = True


class Secret(Mapping[str, str]):
    """Named secret values, e.g. ``secret["api_key"]``. Printing it never shows a value."""

    __slots__ = ("_fields",)

    def __init__(self, fields: Mapping[str, str]) -> None:
        self._fields = dict(fields)

    def __getitem__(self, name: str) -> str:
        try:
            return self._fields[name]
        except KeyError:
            # Not KeyError: its message would be reported as an unexpected adapter bug.
            raise CredentialDenied(f"the stored credential has no field {name!r}") from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._fields)

    def __len__(self) -> int:
        return len(self._fields)

    def __repr__(self) -> str:
        return f"Secret(fields={sorted(self._fields)})"

    __str__ = __repr__


class CredentialResolver(Protocol):
    async def resolve(self, credential_ref: str, job_id: str, attempt: int) -> Secret: ...


class HttpCredentialResolver:
    """Asks the Engine's internal API. One instance (and one connection pool) per worker."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
            transport=transport,
        )

    async def resolve(self, credential_ref: str, job_id: str, attempt: int) -> Secret:
        body = {"credential_ref": credential_ref, "job_id": job_id, "attempt": attempt}
        try:
            response = await self._client.post("/internal/v1/credentials/resolve", json=body)
        except httpx.HTTPError as error:
            # The exception text can include the URL but never the token (it is a header).
            raise CredentialServiceUnavailable(
                f"the Engine's internal API is unreachable ({type(error).__name__})"
            ) from None
        if response.status_code == 200:
            try:
                fields = response.json()["secret"]
                return Secret({str(k): str(v) for k, v in fields.items()})
            except (ValueError, KeyError, AttributeError):
                raise CredentialServiceUnavailable("the Engine returned an unreadable credential") from None
        if response.status_code in (401, 403):
            reason = "worker token rejected" if response.status_code == 401 else "not permitted for this job"
            raise CredentialDenied(f"the Engine refused the credential: {reason}")
        if response.status_code >= 500:
            raise CredentialServiceUnavailable(f"the Engine's internal API answered {response.status_code}")
        raise CredentialDenied(f"the Engine's internal API answered {response.status_code}")

    async def aclose(self) -> None:
        await self._client.aclose()
