"""Finds the shipment history table on a courier's tracking page. No network access here.

Used by the adapters for sites whose result page could not be examined when they were
written (see docs/indian-couriers.md). Instead of a fixed layout it looks for what such
pages have in common: an HTML table whose header names a date column and a status column.
A page without one is reported as schema drift, never guessed at.

The page is untrusted input: it is parsed as data only; nothing in it is executed, followed
or interpreted as an instruction.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from courier_adapter_sdk import SchemaDrift, ShipmentNotFound, TrackingEvent

from .india import local_time, place, status_code

_MAX_ROWS = 500
_DATE = re.compile(r"\bdate\b|\bdated\b|\bwhen\b")
_TIME = re.compile(r"^time$")
_STATUS = re.compile(r"status|activity|particulars|description|remarks|event")
_PLACE = re.compile(r"location|place|branch|station|city|office|\bhub\b")
_NOT_FOUND = re.compile(
    r"no (record|data|detail|result|shipment|consignment|tracking)s?( information)?"
    r" (is |are |was |were )?(found|available)"
    r"|invalid (lr|l\.r\.|consignment|docket|tracking|cnote)"
    r"|(lr|consignment|docket) (number |no\.? )?(not found|does not exist|is invalid)"
)


def _cells(row: Tag) -> list[str]:
    return [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]


def _rows(table: Tag) -> list[Tag]:
    # Rows of this table only, not of a table nested inside one of its cells.
    return [row for row in table.find_all("tr") if row.find_parent("table") is table]


def _column(header: list[str], pattern: re.Pattern[str]) -> int | None:
    return next((i for i, name in enumerate(header) if pattern.search(name.lower())), None)


def history_events(html: str) -> list[TrackingEvent]:
    """Events of the page's history table, oldest first.

    Raises ``ShipmentNotFound`` when the page says it knows no such shipment, and
    ``SchemaDrift`` when it has neither a history table nor such a message.
    """
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = _rows(table)
        if len(rows) < 2:
            continue
        header = _cells(rows[0])
        date, status = _column(header, _DATE), _column(header, _STATUS)
        if date is None or status is None or date == status:
            continue
        time, where = _column(header, _TIME), _column(header, _PLACE)
        events = []
        for row in rows[1 : _MAX_ROWS + 1]:
            cells = _cells(row)
            if len(cells) != len(header) or not cells[date] or not cells[status]:
                continue  # a spacer or a row spanning the table
            moment = cells[date] if time is None else f"{cells[date]} {cells[time]}"
            events.append(
                TrackingEvent(
                    raw_status=status_code(cells[status]),
                    raw_description=cells[status][:2000],
                    occurred_at=local_time(moment),
                    location=place(cells[where]) if where is not None else None,
                )
            )
        if events:
            return sorted(events, key=lambda event: event.occurred_at)
    if _NOT_FOUND.search(re.sub(r"\s+", " ", soup.get_text(" ")).lower()):
        raise ShipmentNotFound
    raise SchemaDrift("the page has no shipment history table this adapter recognizes")
