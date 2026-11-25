"""What the tests of the courier adapters share: a job context and one adapter call."""

from __future__ import annotations

from typing import Any

from courier_adapter_sdk import Adapter, JobContext, TrackRequest
from courier_adapter_sdk.contracts import new_id
from courier_adapter_sdk.credentials import Secret
from courier_adapter_sdk.logging import get_logger


class StaticResolver:
    def __init__(self, secret: Secret) -> None:
        self.secret = secret

    async def resolve(self, credential_ref: str, job_id: str, attempt: int) -> Secret:
        return self.secret

    async def intervention_response(
        self, intervention_id: str, session_ref: str, job_id: str, attempt: int
    ) -> str:
        raise AssertionError("not used")


def job_ctx(secret: dict[str, str] | None = None) -> JobContext:
    """A job's context: with a credential for an API adapter, without one for a website."""
    if secret is None:
        return JobContext(new_id(), 1, new_id(), 0, new_id(), get_logger(), None, None)
    return JobContext(
        new_id(), 1, new_id(), 0, new_id(), get_logger(), new_id(), StaticResolver(Secret(secret))
    )


async def run_track(adapter: Adapter, number: str, secret: dict[str, str] | None = None) -> Any:
    context = job_ctx(secret)
    try:
        return await adapter.track(TrackRequest(adapter.key, number), context)
    finally:
        await context._close(1.0)
