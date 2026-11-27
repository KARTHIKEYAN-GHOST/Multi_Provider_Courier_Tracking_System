"""Turns a Demo Secure Logistics page into events or a sign-in token. No network access here.

Parsing is separate from fetching so it can be tested (and, later, repaired) against saved
pages. The page is untrusted input: it is parsed as data only; nothing in it is executed,
followed or interpreted as an instruction.

PROVISIONAL: this reading of the tracking page is unconfirmed. It follows one delivered page
and one "not found" page seen while signed in. The scans are an ``ol.scans`` list of
``li.scan`` items with ``span.ts`` (day-first time, IST), ``span.desc`` and ``span.loc``,
newest first.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from courier_adapter_sdk import SchemaDrift, ShipmentNotFound, TrackingEvent

from ..india import local_time, place, status_code

_MAX_SCANS = 500
_NOT_FOUND = re.compile(r"consignment not found")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _text(scan: Tag, name: str) -> str:
    cell = scan.find("span", class_=name)
    return cell.get_text(" ", strip=True) if isinstance(cell, Tag) else ""


def is_login_page(html: str) -> bool:
    """Whether the page is the sign-in form (the site sends it when not signed in)."""
    soup = BeautifulSoup(html, "html.parser")
    return soup.select_one('form[action="/secure/login"]') is not None


def form_token(html: str) -> str:
    """The one-time ``form_token`` of the sign-in form."""
    soup = BeautifulSoup(html, "html.parser")
    field = soup.select_one('form[action="/secure/login"] input[name="form_token"]')
    value = field.get("value") if field is not None else None
    if not isinstance(value, str) or not _TOKEN.match(value):
        raise SchemaDrift("the sign-in page has no form token this adapter recognizes")
    return value


def parse_tracking_page(html: str) -> list[TrackingEvent]:
    """Events of a tracking page, oldest first.

    Raises ``ShipmentNotFound`` when the page says the consignment is unknown, and
    ``SchemaDrift`` when it has neither a list of scans nor that message.
    """
    soup = BeautifulSoup(html, "html.parser")
    scans = soup.select("ol.scans > li.scan")
    if not scans:
        empty = soup.select_one("div.empty")
        if empty is not None and _NOT_FOUND.search(empty.get_text(" ", strip=True).lower()):
            raise ShipmentNotFound
        raise SchemaDrift("the page has no list of scans this adapter recognizes")
    events: list[TrackingEvent] = []
    for scan in scans[:_MAX_SCANS]:
        when, what = _text(scan, "ts"), _text(scan, "desc")
        if not when or not what:
            raise SchemaDrift("a scan has no time or no description")
        events.append(
            TrackingEvent(
                raw_status=status_code(what),
                raw_description=what[:2000],
                occurred_at=local_time(when),
                location=place(_text(scan, "loc")),
            )
        )
    return sorted(events, key=lambda event: event.occurred_at)
