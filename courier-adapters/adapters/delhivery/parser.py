"""Turns a Delhivery tracking API answer into events. No network access here.

Written from Delhivery's published API description (``GET /api/v1/packages/json/``): the
answer has a ``ShipmentData`` list; each entry has a ``Shipment`` with ``AWB``, a current
``Status`` and a ``Scans`` list; each scan is a ``ScanDetail`` with ``ScanDateTime``,
``ScanType`` (UD forward, RT return, DL final, ...), ``Scan`` ("In Transit", "Dispatched",
...), ``ScannedLocation`` and ``Instructions``. Times carry no offset and are IST.

What the answer for an unknown waybill looks like is not in that description. An answer
with an ``Error`` text and no shipment is taken as "not found" unless the text speaks of
the token.

The answer is untrusted input and is treated as data only.
"""

from __future__ import annotations

import re
from typing import Any

from courier_adapter_sdk import AuthInvalid, Location, SchemaDrift, ShipmentNotFound, TrackingEvent

from ..india import local_time

_MAX_EVENTS = 500
_PLACE = re.compile(r"^(?P<city>[^()]+?)\s*\((?P<state>[^()]+)\)\s*$")


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _location(value: Any) -> Location | None:
    """``Bengaluru_East (Karnataka)`` is a facility name followed by its state."""
    text = _text(value)
    if text is None:
        return None
    found = _PLACE.match(text)
    if found is None:
        return Location(city=text[:200], country="IN", raw=text[:500])
    return Location(city=found["city"][:200], state=found["state"][:200], country="IN", raw=text[:500])


def _event(kind: Any, label: Any, when: Any, where: Any, note: Any) -> TrackingEvent:
    kind, label = _text(kind), _text(label)
    if label is None:
        raise SchemaDrift("a scan has no status")
    # "In Transit" is good news on the way out (UD) and a return on the way back (RT), so
    # the direction is part of the raw status.
    return TrackingEvent(
        raw_status=(f"{kind}:{label}" if kind else label)[:200],
        raw_description=(_text(note) or label)[:2000],
        occurred_at=local_time(when),
        location=_location(where),
    )


def _scan(item: Any) -> TrackingEvent:
    detail = item.get("ScanDetail") if isinstance(item, dict) else None
    if not isinstance(detail, dict):
        raise SchemaDrift("a scan has no ScanDetail")
    return _event(
        detail.get("ScanType"),
        detail.get("Scan"),
        detail.get("ScanDateTime") or detail.get("StatusDateTime"),
        detail.get("ScannedLocation"),
        detail.get("Instructions"),
    )


def parse_packages(data: Any, waybill: str) -> list[TrackingEvent]:
    """Events of the shipment with this waybill, oldest first."""
    if not isinstance(data, dict):
        raise SchemaDrift("the answer is not an object")
    entries = data.get("ShipmentData")
    shipments = (
        [e["Shipment"] for e in entries or [] if isinstance(e, dict) and isinstance(e.get("Shipment"), dict)]
        if isinstance(entries, list)
        else []
    )
    if not shipments:
        error = _text(data.get("Error"))
        if error is None and entries != []:
            raise SchemaDrift("the answer has neither shipments nor an error")
        if error is not None and re.search(r"token|authenticat|authoriz|permission", error.lower()):
            raise AuthInvalid("Delhivery did not accept the token")
        raise ShipmentNotFound
    wanted = waybill.upper()
    shipment = next((s for s in shipments if str(s.get("AWB", "")).upper() == wanted), shipments[0])

    scans = shipment.get("Scans")
    if isinstance(scans, list) and scans:
        events = [_scan(item) for item in scans[:_MAX_EVENTS]]
    else:
        # A shipment that was only manifested has a current status but no scans yet.
        status = shipment.get("Status")
        if not isinstance(status, dict):
            raise SchemaDrift("the shipment has neither scans nor a status")
        events = [
            _event(
                status.get("StatusType"),
                status.get("Status"),
                status.get("StatusDateTime"),
                status.get("StatusLocation"),
                status.get("Instructions"),
            )
        ]
    return sorted(events, key=lambda event: event.occurred_at)
