"""Repairs: the demo site's timeline layout (div.tracking-v2 ul.timeline) is read."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from adapters.demosite import DemoSiteAdapter
from adapters.demosite.parser import parse_tracking_page
from courier_adapter_sdk import JobContext, ParseError, SchemaDrift, TrackRequest
from courier_adapter_sdk.contracts import new_id
from courier_adapter_sdk.logging import get_logger

FIXTURES = Path(__file__).parent / "fixtures" / "demosite"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.mark.parametrize(
    ("name", "first_utc", "last_utc"),
    [
        (
            "unreadable_ace2d0f43777.html",
            datetime(2026, 1, 6, 9, 15, tzinfo=UTC),
            datetime(2026, 1, 7, 7, 15, tzinfo=UTC),
        ),
        (
            "unreadable_6fc548bde4f4.html",
            datetime(2026, 7, 18, 8, 38, tzinfo=UTC),
            datetime(2026, 7, 19, 6, 38, tzinfo=UTC),
        ),
        (
            "unreadable_87de0b23366b.html",
            datetime(2026, 2, 8, 6, 48, tzinfo=UTC),
            datetime(2026, 2, 9, 4, 48, tzinfo=UTC),
        ),
    ],
)
def test_recorded_timeline_pages_are_parsed(name: str, first_utc: datetime, last_utc: datetime) -> None:
    events = parse_tracking_page(fixture(name))
    assert [e.raw_status for e in events] == ["Booked", "Picked up", "In transit"]
    assert events[0].occurred_at == first_utc
    assert events[-1].occurred_at == last_utc
    assert [e.occurred_at for e in events] == sorted(e.occurred_at for e in events)
    assert all(e.raw_description is None for e in events)
    first, last = events[0].location, events[-1].location
    assert first is not None
    assert (first.city, first.country, first.raw) == ("Chennai", "IN", "Chennai, IN")
    assert last is not None
    assert (last.city, last.country) == ("Bengaluru", "IN")


async def test_the_adapter_reads_a_recorded_timeline_page_over_http() -> None:
    body = fixture("unreadable_ace2d0f43777.html")
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    adapter = DemoSiteAdapter(base_url="http://127.0.0.1:8090", transport=httpx.MockTransport(answer))
    ctx = JobContext(new_id(), 1, new_id(), 0, new_id(), get_logger())
    try:
        result = await adapter.track(TrackRequest("demosite", "TRACKING0001"), ctx)
    finally:
        await ctx._close(1.0)
    assert [e.raw_status for e in result.events] == ["Booked", "Picked up", "In transit"]
    assert result.raw_response.body == body
    assert len(seen) == 1
    assert seen[0].url.path == "/track"
    assert seen[0].url.params["number"] == "TRACKING0001"


def _timeline(item: str) -> str:
    return f'<html><body><div class="tracking-v2"><ul class="timeline">{item}</ul></div></body></html>'


def test_a_timeline_date_that_cannot_be_read_is_a_parse_error() -> None:
    html = _timeline('<li class="tl-item"><time>next week</time><b>Booked</b><i>Chennai, IN</i></li>')
    with pytest.raises(ParseError) as raised:
        parse_tracking_page(html)
    assert "week" not in str(raised.value)


def test_a_timeline_date_without_a_time_zone_is_not_guessed() -> None:
    html = _timeline(
        '<li class="tl-item"><time>2026-01-06T14:45:00</time><b>Booked</b><i>Chennai, IN</i></li>'
    )
    with pytest.raises(ParseError):
        parse_tracking_page(html)


@pytest.mark.parametrize(
    "html",
    [
        _timeline(""),
        _timeline('<li class="tl-item"><b>Booked</b><i>Chennai, IN</i></li>'),
        _timeline('<li class="tl-item"><time>2026-01-06T14:45:00+05:30</time><i>Chennai, IN</i></li>'),
        '<html><body><div class="tracking-v2"></div></body></html>',
    ],
    ids=["no-items", "no-time", "no-status", "no-timeline"],
)
def test_a_timeline_without_the_expected_structure_is_schema_drift(html: str) -> None:
    with pytest.raises(SchemaDrift):
        parse_tracking_page(html)
