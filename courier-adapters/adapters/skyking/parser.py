"""Turns the SkyKing tracking service's answer into events. No network access here.

Written from the script of the tracking page, which shows the answer as a table: the answer
is a list, empty when nothing is known ("No Tracking Information Available"), and every
entry has ``DateTime``, ``Status`` and ``Location``. No real answer was available, so the
date is accepted in any of the usual Indian notations (see ``adapters/india.py``).

The answer is untrusted input and is treated as data only.
"""

from __future__ import annotations

from typing import Any

from courier_adapter_sdk import SchemaDrift, ShipmentNotFound, TrackingEvent

from ..india import local_time, place, status_code

_MAX_EVENTS = 500


def _event(item: Any) -> TrackingEvent:
    if not isinstance(item, dict):
        raise SchemaDrift("a history entry is not an object")
    status = item.get("Status")
    if not isinstance(status, str) or not status.strip():
        raise SchemaDrift("a history entry has no Status")
    status = status.strip()
    return TrackingEvent(
        raw_status=status_code(status),
        raw_description=status[:2000],
        occurred_at=local_time(item.get("DateTime")),
        location=place(item.get("Location")),
    )


def parse_history(data: Any) -> list[TrackingEvent]:
    """Events of the consignment, oldest first."""
    if not isinstance(data, list):
        raise SchemaDrift("the answer is not a list")
    if not data:
        raise ShipmentNotFound
    events = [_event(item) for item in data[:_MAX_EVENTS]]
    return sorted(events, key=lambda event: event.occurred_at)
