"""Turns a Safexpress waybill tracking answer into events. No network access here.

Written from how Safexpress's own website reads the answer of its tracking service:
``shipment.result`` is ``"failed"`` for an unknown waybill; otherwise ``shipment.tracking``
is a list whose entries have ``eventdt`` (date and time), ``eventdesc`` (a sentence such as
"Waybill Generated For ...") and ``status``. No complete example answer was available, so
the date is accepted in any of the usual Indian notations (see ``adapters/india.py``).

The answer is untrusted input and is treated as data only.
"""

from __future__ import annotations

from typing import Any

from courier_adapter_sdk import SchemaDrift, ShipmentNotFound, TrackingEvent

from ..india import local_time, place, status_code

_MAX_EVENTS = 500


def _event(item: Any) -> TrackingEvent:
    if not isinstance(item, dict):
        raise SchemaDrift("a tracking entry is not an object")
    sentence, label = (item.get(k) for k in ("eventdesc", "status"))
    sentence = sentence.strip() if isinstance(sentence, str) and sentence.strip() else None
    label = label.strip() if isinstance(label, str) and label.strip() else None
    if sentence is None and label is None:
        raise SchemaDrift("a tracking entry has neither status nor eventdesc")
    return TrackingEvent(
        raw_status=status_code(label or sentence or ""),
        raw_description=(sentence or label or "")[:2000],
        occurred_at=local_time(item.get("eventdt")),
        location=place(item.get("location")),
    )


def parse_tracking(data: Any) -> list[TrackingEvent]:
    """Events of the waybill, oldest first."""
    shipment = data.get("shipment") if isinstance(data, dict) else None
    if not isinstance(shipment, dict):
        raise SchemaDrift("the answer has no shipment")
    if str(shipment.get("result", "")).lower() == "failed":
        raise ShipmentNotFound
    entries = shipment.get("tracking")
    if not isinstance(entries, list):
        raise SchemaDrift("the shipment has no tracking list")
    if not entries:
        raise ShipmentNotFound
    events = [_event(item) for item in entries[:_MAX_EVENTS]]
    return sorted(events, key=lambda event: event.occurred_at)
