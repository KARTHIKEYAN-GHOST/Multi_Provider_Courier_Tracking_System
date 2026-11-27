"""Repairs: the site's newer timeline layout is read into events."""

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


def recorded(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.mark.parametrize(
    ("name", "expected_times"),
    [
        (
            "unreadable_6d0fe480d833.html",
            [
                datetime(2026, 5, 14, 9, 53, tzinfo=UTC),
                datetime(2026, 5, 14, 20, 53, tzinfo=UTC),
                datetime(2026, 5, 15, 7, 53, tzinfo=UTC),
            ],
        ),
        (
            "unreadable_8024984900c5.html",
            [
                datetime(2026, 4, 3, 6, 42, tzinfo=UTC),
                datetime(2026, 4, 3, 17, 42, tzinfo=UTC),
                datetime(2026, 4, 4, 4, 42, tzinfo=UTC),
            ],
        ),
        (
            "unreadable_3e782ad7a33f.html",
            [
                datetime(2026, 5, 24, 5, 3, tzinfo=UTC),
                datetime(2026, 5, 24, 16, 3, tzinfo=UTC),
                datetime(2026, 5, 25, 3, 3, tzinfo=UTC),
            ],
        ),
    ],
)
def test_a_recorded_timeline_page_is_parsed_into_its_events(
    name: str, expected_times: list[datetime]
) -> None:
    events = parse_tracking_page(recorded(name))
    assert [e.raw_status for e in events] == ["Booked", "Picked up", "In transit"]
    assert [e.occurred_at for e in events] == expected_times
    assert all(e.raw_description is None for e in events)
    places = []
    for event in events:
        assert event.location is not None
        places.append((event.location.city, event.location.country, event.location.raw))
    assert places == [
        ("Chennai", "IN", "Chennai, IN"),
        ("Chennai", "IN", "Chennai, IN"),
        ("Bengaluru", "IN", "Bengaluru, IN"),
    ]


async def test_the_adapter_returns_the_events_of_a_recorded_timeline_page() -> None:
    body = recorded("unreadable_6d0fe480d833.html")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/track"
        assert request.url.params["number"] == "TRACKING0001"
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    adapter = DemoSiteAdapter(base_url="http://127.0.0.1:8090", transport=httpx.MockTransport(handler))
    ctx = JobContext(new_id(), 1, new_id(), 0, new_id(), get_logger())
    try:
        result = await adapter.track(TrackRequest("demosite", "TRACKING0001"), ctx)
    finally:
        await ctx._close(1.0)
    assert [e.raw_status for e in result.events] == ["Booked", "Picked up", "In transit"]
    assert result.raw_response.body == body


def _timeline(items: str) -> str:
    return f'<html><body><div class="tracking-v2"><ul class="timeline">{items}</ul></div></body></html>'


def test_a_timeline_date_without_an_offset_is_a_parse_error() -> None:
    html = _timeline(
        '<li class="tl-item"><time>2026-05-14T15:23:00</time><b>Booked</b><i>Chennai, IN</i></li>'
    )
    with pytest.raises(ParseError):
        parse_tracking_page(html)


def test_an_unreadable_timeline_date_is_a_parse_error_that_does_not_quote_the_page() -> None:
    html = _timeline('<li class="tl-item"><time>next week</time><b>Booked</b><i>Chennai, IN</i></li>')
    with pytest.raises(ParseError) as raised:
        parse_tracking_page(html)
    assert "week" not in str(raised.value)


@pytest.mark.parametrize(
    "items",
    [
        "",
        '<li class="tl-item"><time>2026-05-14T15:23:00+05:30</time><i>Chennai, IN</i></li>',
    ],
    ids=["no-items", "missing-status"],
)
def test_a_timeline_without_the_expected_structure_is_schema_drift(items: str) -> None:
    with pytest.raises(SchemaDrift):
        parse_tracking_page(_timeline(items))
