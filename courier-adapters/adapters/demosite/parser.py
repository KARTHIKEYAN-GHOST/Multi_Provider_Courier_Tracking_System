"""Turns a demo-courier tracking page into events. No network access here.

Parsing is separate from fetching so it can be tested (and, later, repaired) against saved
pages. The page is untrusted input: it is parsed as data only; nothing in it is executed,
followed or interpreted as an instruction.
"""

from __future__ import annotations

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


def _location(place: str) -> Location | None:
    if not place:
        return None
    city, _, country = (part.strip() for part in place.rpartition(","))
    return Location(city=city or None, country=country or None, raw=place[:500])


def parse_tracking_page(html: str) -> list[TrackingEvent]:
    """Events in the order they happened.

    Raises ``ShipmentNotFound`` for the site's "no shipment found" page, ``SchemaDrift`` when
    the page no longer has the structure this parser was written for, and ``ParseError``
    when the structure is right but a value cannot be understood.
    """
    page = BeautifulSoup(html, "html.parser")
    if page.find(class_="not-found") is not None:
        raise ShipmentNotFound
    table = page.find("table", id="history")
    if not isinstance(table, Tag):
        raise SchemaDrift("the page has no history table")
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
    return sorted(events, key=lambda event: event.occurred_at)
