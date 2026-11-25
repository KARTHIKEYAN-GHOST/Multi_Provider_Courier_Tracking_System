"""SkyKing adapter: parsing the tracking service's answer, and the two-step exchange.

The answers are built from the fields the tracking page's script reads; no answer of the
real service was requested or available.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from adapters.skyking import SkykingAdapter
from adapters.skyking.parser import parse_history
from courier_adapter_sdk import (
    Blocked,
    ParseError,
    ProviderServerError,
    RateLimited,
    SchemaDrift,
    ShipmentNotFound,
)

from .adapter_support import run_track

CNOTE = "123456789"
PAGE_KEY = "page-key-from-the-site"
HISTORY = [
    {
        "DateTime": "26-11-2026 10:30",
        "Status": "Delivered",
        "Location": "CHENNAI",
        "RefNo": "D1",
        "Statusflag": 1,
    },
    {
        "DateTime": "26-11-2026 08:00",
        "Status": "Out For Delivery",
        "Location": "CHENNAI",
        "RefNo": "",
        "Statusflag": 0,
    },
    {"DateTime": "25-11-2026 14:05", "Status": "Booked", "Location": "TIRUPUR", "RefNo": "", "Statusflag": 0},
]


def test_history_entries_are_parsed_oldest_first() -> None:
    events = parse_history(HISTORY)
    assert [e.raw_status for e in events] == ["booked", "out-for-delivery", "delivered"]
    assert events[0].occurred_at == datetime(2026, 11, 25, 8, 35, tzinfo=UTC), "IST converted to UTC"
    assert events[0].location is not None
    assert events[0].location.city == "TIRUPUR"


def test_an_undelivered_entry_is_not_read_as_delivered() -> None:
    (event,) = parse_history(
        [{"DateTime": "26-11-2026 18:00", "Status": "Un-Delivered - Door Locked", "Location": "X"}]
    )
    assert (event.raw_status, event.raw_description) == ("undelivered", "Un-Delivered - Door Locked")


def test_an_empty_list_means_not_found() -> None:
    with pytest.raises(ShipmentNotFound):
        parse_history([])


@pytest.mark.parametrize(
    "data",
    [{}, "none", ["Delivered"], [{"DateTime": "26-11-2026 10:30"}], [{"Status": "Delivered"}]],
    ids=["object", "text", "entry-not-object", "no-status", "no-date"],
)
def test_an_answer_with_a_different_structure_is_schema_drift(data: Any) -> None:
    with pytest.raises(SchemaDrift):
        parse_history(data)


# ----------------------------------------------------------------------------- adapter


def site(history: Any = HISTORY, seen: list[httpx.Request] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/api/Track/GetAPIKeyValue":
            return httpx.Response(200, json=PAGE_KEY)
        return httpx.Response(200, json=history)

    return handler


async def track(handler: Any, number: str = CNOTE) -> Any:
    return await run_track(SkykingAdapter(transport=httpx.MockTransport(handler)), number)


async def test_the_lookup_repeats_the_two_requests_of_the_tracking_page() -> None:
    seen: list[httpx.Request] = []
    result = await track(site(seen=seen))
    key_request, lookup = seen
    assert (key_request.method, key_request.url.host) == ("GET", "live.skyking.co")
    assert lookup.url.path == "/api/Track/ConsignmentMTrack_WebSite_AuthNew"
    assert lookup.url.params["cnote"] == CNOTE
    assert set(lookup.url.params) == {"cnote", "key", "Email"}
    assert lookup.headers["keyvalue"] == PAGE_KEY
    assert result.events[-1].raw_status == "delivered"
    assert result.raw_response.body == HISTORY


@pytest.mark.parametrize("number", ["12 34", "1234;DROP", "../etc/passwd", "x" * 21, "ab"])
async def test_something_that_is_not_a_consignment_number_is_not_sent_to_the_site(number: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the site must not be contacted")

    with pytest.raises(ShipmentNotFound):
        await track(handler, number)


async def test_an_unknown_consignment_is_not_found() -> None:
    with pytest.raises(ShipmentNotFound):
        await track(site(history=[]))


@pytest.mark.parametrize(
    ("status", "error"), [(403, Blocked), (429, RateLimited), (500, ProviderServerError), (302, Blocked)]
)
async def test_a_refusal_at_either_step_is_classified(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await track(lambda request: httpx.Response(status, text="no"))

    def second_step(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("GetAPIKeyValue"):
            return httpx.Response(200, json=PAGE_KEY)
        return httpx.Response(status, text="no")

    with pytest.raises(error):
        await track(second_step)


@pytest.mark.parametrize("key_answer", [{"key": "x"}, "", None, 7])
async def test_an_unusable_access_key_is_a_parse_error_and_no_lookup_is_made(key_answer: Any) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=key_answer)

    with pytest.raises(ParseError):
        await track(handler)
    assert calls == ["/api/Track/GetAPIKeyValue"]


async def test_an_html_answer_instead_of_json_is_a_parse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("GetAPIKeyValue"):
            return httpx.Response(200, json=PAGE_KEY)
        return httpx.Response(200, text="<html>Please verify you are human</html>")

    with pytest.raises(ParseError):
        await track(handler)
