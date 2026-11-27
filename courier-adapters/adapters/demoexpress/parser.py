"""Turns a Demo Express tracking page into events. No network access here.

PROVISIONAL and unconfirmed: this reading was written from a single result page and a
single "not found" page of the site; no example answer was supplied with the task. The
verification page (numbers starting DEMO-OTP or DEMO-CAPTCHA) was not looked at; it is
read the way the reference demo-site adapter reads it. A changed page is reported as
schema drift, never guessed at.

Parsing is separate from fetching so it can be tested (and, later, repaired) against saved
pages. The page is untrusted input: it is parsed as data only; nothing in it is executed,
followed or interpreted as an instruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from courier_adapter_sdk import Location, ParseError, SchemaDrift, ShipmentNotFound, TrackingEvent

from ..india import local_time, status_code

_MAX_ROWS = 500
_TOKEN = re.compile(r"^[0-9a-f]{32}$")


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
        if not isinstance(row, Tag):
            continue
        when, status = _text(row, "when"), _text(row, "status")
        if not status:
            raise ParseError("a history row has an empty status")
        events.append(
            TrackingEvent(
                raw_status=status_code(status),
                raw_description=_text(row, "note")[:2000] or None,
                occurred_at=local_time(when),
                location=_location(_text(row, "place")),
            )
        )
    return events


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
    events = _parse_history_table(table)
    return sorted(events, key=lambda event: event.occurred_at)
