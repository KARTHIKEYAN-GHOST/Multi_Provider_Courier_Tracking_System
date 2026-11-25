"""Helpers shared by the adapters for Indian couriers: local time and status wording.

Indian courier sites and APIs give times as local time (IST) without saying so, and most
describe a status in free text. Both are untrusted input and are treated as data only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from courier_adapter_sdk import Location, ParseError, SchemaDrift

IST = timezone(timedelta(hours=5, minutes=30))

# Day first, as written in India. A value such as 03-04-2026 is therefore 3 April.
_FORMATS = (
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y %I:%M:%S %p",
    "%d-%m-%Y %I:%M %p",
    "%d-%m-%Y",
    "%d-%m-%y %H:%M",
    "%d-%m-%y",
    "%d %b %Y %H:%M:%S",
    "%d %b %Y %H:%M",
    "%d %b %Y %I:%M %p",
    "%d %b %Y",
    "%d %B %Y %H:%M",
    "%d %B %Y",
)


def local_time(value: Any) -> datetime:
    """A provider's date and time, understood as IST unless it names an offset, in UTC."""
    if not isinstance(value, str) or not value.strip():
        raise SchemaDrift("an event has no date")
    text = re.sub(r"\s+", " ", value.replace(",", " ")).strip()
    text = re.sub(r"\s*\b(IST|hrs|Hrs)\b\.?$", "", text)
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # 25/11/2026, 25.11.2026 and 25-Nov-2026 are all written with dashes or spaces here.
        plain = re.sub(r"(?<=\d)[/.](?=\d)", "-", text)
        plain = re.sub(r"(?<=\d)-(?=[A-Za-z])|(?<=[A-Za-z])-(?=\d)", " ", plain)
        plain = re.sub(r"(?i)(?<=\d)(am|pm)$", r" \1", plain)
        for layout in _FORMATS:
            try:
                parsed = datetime.strptime(plain, layout)
                break
            except ValueError:
                continue
    if parsed is None:
        # The value is not quoted: it comes from the provider and goes into a message.
        raise ParseError("an event has a date in an unknown format")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(UTC)


# Checked in this order: "un-delivered" and "out for delivery" both contain "deliver".
_VOCABULARY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("returned", re.compile(r"\brto\b|\breturn")),
    (
        "undelivered",
        re.compile(r"\bun ?delivered\b|\bnot delivered\b|delivery (attempt|failed)|\brefused\b"),
    ),
    ("out-for-delivery", re.compile(r"out for delivery|\bofd\b")),
    ("delivered", re.compile(r"\bdelivered\b")),
    ("picked-up", re.compile(r"\bpicked\b|pick ?up done|\bcollected\b")),
    ("booked", re.compile(r"\bbooked\b|\bbooking\b|\bmanifest|\bgenerated\b")),
    (
        "in-transit",
        re.compile(
            r"transit|\bdispatched\b|\bforwarded\b|\barrived\b|\breached\b|\breceived at\b"
            r"|\bdeparted\b|\bloaded\b|\bunloaded\b|\bin ?scan\b|\bout ?scan\b"
        ),
    ),
)


def status_code(text: str) -> str:
    """A fixed code for a status described in free text, or the text itself.

    The Engine maps raw statuses one by one, which free text ("Arrived at Salem hub") would
    make impossible. So wording that is recognized is reduced to one of seven codes. Wording
    that is not recognized is passed on unchanged: it then shows up in the Engine's list of
    unmapped statuses, where a person decides what it means.
    """
    words = re.sub(r"[-_]+", " ", text.lower())
    for code, pattern in _VOCABULARY:
        if pattern.search(words):
            return code
    return text[:200]


def place(value: Any) -> Location | None:
    """A location of which only the provider's own wording is known."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return Location(city=text[:200], country="IN", raw=text[:500])
