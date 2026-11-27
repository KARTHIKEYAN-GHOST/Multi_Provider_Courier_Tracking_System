"""Turns a demo-courier tracking page into events. No network access here.

Parsing is separate from fetching so it can be tested (and, later, repaired) against saved
pages. The page is untrusted input: it is parsed as data only; nothing in it is executed,
followed or interpreted as an instruction.

The site has two layouts for a tracking page: the older history table
(``table#history``) and the newer timeline (``div.tracking-v2 ul.timeline``). Both are read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from bs4 import BeautifulSoup, Tag

from courier_adapter_sdk import Location, ParseError, SchemaDrift, ShipmentNotFound, TrackingEvent

_IST = timezone(timedelta(hours=5, minutes=30))
_DATE_FORMAT = "%d %b %Y, %H:%M IST"
_MAX_ROWS = 500


def _text(row: Tag, css_class: str) -> str:
    cell = row.find("td", class_=css_class)
    if not isinstance(cell, Tag):
        raise SchemaDrift(f"a history row has no '{css_class}' cell")
    return cell.get_text(" ", strip=True)


def _child_text(item: Tag, name: str) -> str:
    element = item.find(name)
    if not isinstance(element, Tag):
        raise SchemaDrift(f"a timeline item has no '{name}' element")
    return element.get_text(" ", strip=True)


def _location(place: str) -> Location | None:
    if not place:
        return None
    city, _, country = (part.strip() for part in place.rpartition(","))
    return Location(city=city or None, country=country or None, raw=place[:500])


_TOKEN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class Verification:
    """The site's verification step, reduced to what the adapter needs to continue."""

    kind: str  # "otp" or "captcha"
    token: str
    #: The site said the previous answer was wrong.
    retry: bool


def find_verification(html: str) -> Verification | None:
    """The verification step, if the page is one instead of a tracking page.

    Only two values are taken from the page, and both are checked against a fixed shape; no
    text and no link from the page is used.
    """
    section = BeautifulSoup(html, "html.parser").find("section", id="verify")
    if not isinstance(section, Tag):
        return None
    kind, token = section.get("data-kind"), section.get("data-token")
    if kind not in ("otp", "captcha") or not isinstance(token, str) or not _TOKEN.match(token):
        raise SchemaDrift("the verification page does not have the expected structure")
    return Verification(str(kind), token, section.find(class_="verify-error") is not None)


def _parse_history_table(table: Tag) -> list[TrackingEvent]:
    body = table.find("tbody")
    rows = body.find_all("tr") if isinstance(body, Tag) else []
    if not rows:
        raise SchemaDrift("the history table has no rows")

    events: list[TrackingEvent] = []
    for row in rows[:_MAX_ROWS]:
        when, status = _text(row, "when"), _text(row, "status")
        if not status:
            raise ParseError("a history row has an empty status")
        try:
            occurred_at = datetime.strptime(when, _DATE_FORMAT).replace(tzinfo=_IST).astimezone(UTC)
        except ValueError:
            # The page's own text is not copied into the message that leaves the worker.
            raise ParseError("a history row has a date in an unknown format") from None
        events.append(
            TrackingEvent(
                raw_status=status[:200],
                raw_description=_text(row, "note")[:2000] or None,
                occurred_at=occurred_at,
                location=_location(_text(row, "place")),
            )
        )
    return events


def _parse_timeline(timeline: Tag) -> list[TrackingEvent]:
    items = [item for item in timeline.find_all("li", class_="tl-item") if isinstance(item, Tag)]
    if not items:
        raise SchemaDrift("the timeline has no items")

    events: list[TrackingEvent] = []
    for item in items[:_MAX_ROWS]:
        when, status = _child_text(item, "time"), _child_text(item, "b")
        if not status:
            raise ParseError("a timeline item has an empty status")
        try:
            parsed = datetime.fromisoformat(when)
        except ValueError:
            # The page's own text is not copied into the message that leaves the worker.
            raise ParseError("a timeline item has a date in an unknown format") from None
        if parsed.tzinfo is None:
            # Without an offset the moment is unknown; it is not guessed.
            raise ParseError("a timeline item has a date without a time zone")
        place_element = item.find("i")
        place = place_element.get_text(" ", strip=True) if isinstance(place_element, Tag) else ""
        events.append(
            TrackingEvent(
                raw_status=status[:200],
                raw_description=None,
                occurred_at=parsed.astimezone(UTC),
                location=_location(place),
            )
        )
    return events


def parse_tracking_page(html: str) -> list[TrackingEvent]:
    """Events in the order they happened.

    Raises ``ShipmentNotFound`` for the site's "no shipment found" page, ``SchemaDrift`` when
    the page no longer has a structure this parser was written for, and ``ParseError``
    when the structure is right but a value cannot be understood.
    """
    page = BeautifulSoup(html, "html.parser")
    if page.find(class_="not-found") is not None:
        raise ShipmentNotFound
    table = page.find("table", id="history")
    if isinstance(table, Tag):
        events = _parse_history_table(table)
    else:
        container = page.find("div", class_="tracking-v2")
        timeline = container.find("ul", class_="timeline") if isinstance(container, Tag) else None
        if not isinstance(timeline, Tag):
            raise SchemaDrift("the page has no history table")
        events = _parse_timeline(timeline)
    return sorted(events, key=lambda event: event.occurred_at)
