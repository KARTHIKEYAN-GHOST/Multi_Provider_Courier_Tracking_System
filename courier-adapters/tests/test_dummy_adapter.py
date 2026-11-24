"""Every behaviour of the dummy adapter, selected by tracking-number prefix."""

from __future__ import annotations

import asyncio

import pytest

from adapters.dummy import DummyAdapter
from courier_adapter_sdk import (
    Blocked,
    JobContext,
    NetworkTimeout,
    ProviderServerError,
    ShipmentNotFound,
    TrackRequest,
)
from courier_adapter_sdk.contracts import new_id
from courier_adapter_sdk.logging import get_logger
from courier_adapter_sdk.testing import SimulateCrash, SimulateDuplicateResult, SimulateHang


def ctx(attempt: int = 1, delivery_count: int = 0) -> JobContext:
    return JobContext(new_id(), attempt, new_id(), delivery_count, new_id(), get_logger())


async def track(number: str, **kwargs: int) -> list[str]:
    result = await DummyAdapter().track(TrackRequest("dummy", number), ctx(**kwargs))
    return [event.raw_status for event in result.events]


async def test_ok_is_delivered_with_three_events() -> None:
    assert await track("DUMMY-OK-1") == ["PKG_CREATED", "PKG_IN_TRANSIT", "PKG_DELIVERED"]


async def test_transit_stops_in_transit() -> None:
    assert await track("DUMMY-TRANSIT-1") == ["PKG_CREATED", "PKG_IN_TRANSIT"]


async def test_odd_contains_a_status_the_engine_has_no_mapping_for() -> None:
    assert await track("DUMMY-ODD-1") == ["PKG_CREATED", "PKG_HELD_AT_CUSTOMS"]


async def test_prefixes_are_case_insensitive() -> None:
    assert len(await track("dummy-ok-1")) == 3


async def test_the_same_number_always_returns_the_same_history() -> None:
    adapter = DummyAdapter()
    first = await adapter.track(TrackRequest("dummy", "DUMMY-OK-7"), ctx())
    second = await adapter.track(TrackRequest("dummy", "DUMMY-OK-7"), ctx())
    other = await adapter.track(TrackRequest("dummy", "DUMMY-OK-8"), ctx())
    assert first == second
    assert first.events[0].occurred_at != other.events[0].occurred_at
    times = [e.occurred_at for e in first.events]
    assert times == sorted(times)
    assert first.raw_response.body["tracking_number"] == "DUMMY-OK-7"


@pytest.mark.parametrize(
    ("number", "error"),
    [
        ("DUMMY-NOTFOUND-1", ShipmentNotFound),
        ("SOMETHING-ELSE", ShipmentNotFound),
        ("DUMMY-FAIL-1", ProviderServerError),
        ("DUMMY-FATAL-1", Blocked),
        ("DUMMY-HANG-1", SimulateHang),
        ("DUMMY-CRASH-1", SimulateCrash),
        ("DUMMY-DUP-1", SimulateDuplicateResult),
    ],
)
async def test_failure_prefixes(number: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        await track(number)


async def test_flaky_fails_the_first_attempt_only() -> None:
    with pytest.raises(NetworkTimeout):
        await track("DUMMY-FLAKY-1", attempt=1)
    assert len(await track("DUMMY-FLAKY-1", attempt=2)) == 3


async def test_crashonce_crashes_on_the_first_delivery_only() -> None:
    with pytest.raises(SimulateCrash):
        await track("DUMMY-CRASHONCE-1", delivery_count=0)
    assert len(await track("DUMMY-CRASHONCE-1", delivery_count=1)) == 3


async def test_delay_waits_the_requested_time() -> None:
    loop = asyncio.get_running_loop()
    began = loop.time()
    assert len(await track("DUMMY-DELAY-80-1")) == 3
    assert 0.07 <= loop.time() - began < 1


async def test_slow_never_finishes_on_its_own() -> None:
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await track("DUMMY-SLOW-1")
