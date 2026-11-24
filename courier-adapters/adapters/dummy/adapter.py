"""Dummy adapter: a courier that exists only to exercise the system.

It calls nothing external. The prefix of the tracking number selects a behaviour, so every
path through the Engine and the worker — success, failure, retry, timeout, crash, hang,
duplicate — can be triggered on demand. Prefixes are matched case-insensitively.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from courier_adapter_sdk import (
    Adapter,
    Blocked,
    JobContext,
    Location,
    NetworkTimeout,
    ProviderServerError,
    RawResponse,
    ShipmentNotFound,
    TrackingEvent,
    TrackingResult,
    TrackRequest,
)
from courier_adapter_sdk.testing import SimulateCrash, SimulateDuplicateResult, SimulateHang

_DELIVERED = ("PKG_CREATED", "PKG_IN_TRANSIT", "PKG_DELIVERED")
_IN_TRANSIT = ("PKG_CREATED", "PKG_IN_TRANSIT")
# PKG_HELD_AT_CUSTOMS is deliberately missing from the Engine's status mapping.
_ODD = ("PKG_CREATED", "PKG_HELD_AT_CUSTOMS")
_DESCRIPTIONS = {
    "PKG_CREATED": "Shipment information received",
    "PKG_IN_TRANSIT": "Departed from facility",
    "PKG_DELIVERED": "Delivered",
    "PKG_HELD_AT_CUSTOMS": "Held at customs",
}
_CITIES = ("Chennai", "Bengaluru", "Coimbatore")


def _history(tracking_number: str, statuses: tuple[str, ...]) -> TrackingResult:
    """A fixed history for a tracking number.

    Timestamps are derived from the number itself, so looking the same shipment up twice
    returns identical events, as a real provider would.
    """
    seed = int.from_bytes(hashlib.sha256(tracking_number.encode()).digest()[:4])
    first = datetime(2026, 1, 1, 8, 0, tzinfo=UTC) + timedelta(days=seed % 300, minutes=seed % 600)
    events = [
        TrackingEvent(
            raw_status=status,
            raw_description=_DESCRIPTIONS[status],
            occurred_at=first + timedelta(hours=18 * index),
            location=Location(city=_CITIES[index % len(_CITIES)], country="IN"),
        )
        for index, status in enumerate(statuses)
    ]
    body = {"tracking_number": tracking_number, "history": list(statuses)}
    return TrackingResult(RawResponse(content_type="application/json", body=body), events)


class DummyAdapter(Adapter):
    key: ClassVar[str] = "dummy"
    version: ClassVar[str] = "0.1.0"

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        number = request.tracking_number.upper()

        def has(prefix: str) -> bool:
            return number.startswith(f"DUMMY-{prefix}-")

        if has("OK"):
            return _history(number, _DELIVERED)
        if has("TRANSIT"):
            return _history(number, _IN_TRANSIT)
        if has("ODD"):
            return _history(number, _ODD)
        if has("NOTFOUND"):
            raise ShipmentNotFound(number)
        if has("FAIL"):
            raise ProviderServerError("simulated provider outage")
        if has("FATAL"):
            raise Blocked("simulated permanent refusal")
        if has("FLAKY"):
            if ctx.attempt == 1:
                raise NetworkTimeout("simulated timeout on the first attempt")
            return _history(number, _DELIVERED)
        if has("SLOW"):
            await asyncio.sleep(3600)  # far beyond any timeout; the SDK cancels it
            return _history(number, _DELIVERED)
        if has("DELAY"):
            # DUMMY-DELAY-<milliseconds>-<anything>
            parts = number.split("-")
            milliseconds = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            await asyncio.sleep(min(milliseconds, 600_000) / 1000)
            return _history(number, _DELIVERED)
        if has("HANG"):
            raise SimulateHang
        if has("CRASHONCE"):
            if ctx.delivery_count == 0:
                raise SimulateCrash
            return _history(number, _DELIVERED)
        if has("CRASH"):
            raise SimulateCrash
        if has("DUP"):
            raise SimulateDuplicateResult(_history(number, _DELIVERED))
        # Anything else behaves like a number the courier has never heard of.
        raise ShipmentNotFound(number)
