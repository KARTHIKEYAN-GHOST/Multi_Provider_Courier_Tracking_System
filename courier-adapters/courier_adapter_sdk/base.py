"""The interface every courier adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .context import JobContext
from .contracts import RawResponse, TrackingEvent


@dataclass(frozen=True)
class TrackRequest:
    """What to look up."""

    provider_code: str
    tracking_number: str
    #: Provider-specific, non-secret parameters from the tracking request.
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrackingResult:
    """A successful answer from the provider.

    ``events`` carry the provider's own status wording; adapters never normalize statuses —
    the Engine does that with its mapping table.
    """

    raw_response: RawResponse
    events: list[TrackingEvent]


class Adapter(ABC):
    """One courier integration.

    An adapter only fetches and extracts. It never touches the Engine's database, never
    decides about retries, and is handed no credentials through messages.

    ``track`` must be async and must not block the event loop: several jobs run on one loop,
    so a blocking call would stall all of them. CPU-heavy or blocking work belongs in
    ``asyncio.to_thread``.
    """

    #: Matches ``adapters.key`` in the Engine and the routing key ``job.track.v1.<key>``.
    key: ClassVar[str]
    #: Semantic version, reported with every execution.
    version: ClassVar[str]

    async def startup(self) -> None:  # noqa: B027 - optional hook
        """Open process-wide resources (called once before consuming)."""

    async def shutdown(self) -> None:  # noqa: B027 - optional hook
        """Release process-wide resources (called once after the last job)."""

    @abstractmethod
    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        """Look the shipment up.

        Return a ``TrackingResult``; raise ``ShipmentNotFound`` if the provider does not know
        the number; raise an ``AdapterError`` subclass for failures. The call is cancelled if
        it exceeds the job's execution budget.

        If the provider asks for a one-time code or a CAPTCHA, raise ``otp_required(...)`` or
        ``captcha_required(...)`` with the session needed to continue, and implement ``resume``.
        """

    async def resume(
        self, request: TrackRequest, ctx: JobContext, session: dict[str, Any], response: str
    ) -> TrackingResult:
        """Continue a lookup that stopped for a human answer.

        ``session`` is what ``track`` handed over; ``response`` is what the user typed. This
        may run in another worker process than ``track`` did, so nothing may be kept on the
        adapter between the two. Outcomes are the same as for ``track``; if the provider did
        not accept the answer and asks again, raise a new challenge.

        ``response`` is a secret of the user: use it, do not log or store it.
        """
        raise NotImplementedError(f"adapter {self.key!r} raised a challenge but does not implement resume()")
