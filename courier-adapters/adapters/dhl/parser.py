"""Turns a DHL "Shipment Tracking - Unified" API answer into events. No network access here.

Written from DHL's published API description (``GET /track/shipments``): the answer has a
``shipments`` list; each shipment has ``id``, a current ``status`` and an ``events`` list,
and every status/event has ``timestamp``, ``statusCode`` (pre-transit, transit, delivered,
failure, unknown), ``status``, ``description`` and ``location.address``.

The answer is untrusted input and is treated as data only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from courier_adapter_sdk import Location, ParseError, SchemaDrift, ShipmentNotFound, TrackingEvent

_MAX_EVENTS = 500


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise SchemaDrift("an event has no timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ParseError("an event has a timestamp in an unknown format") from None
    # DHL sends some timestamps without an offset (local time of the event's location, which
    # the answer does not name). They are taken as UTC, so such an event can be off by the
    # location's UTC offset.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _location(value: Any) -> Location | None:
    address = value.get("address") if isinstance(value, dict) else None
    if not isinstance(address, dict):
        return None
    city, country, postal = (address.get(k) for k in ("addressLocality", "countryCode", "postalCode"))
    if not any(isinstance(v, str) and v for v in (city, country, postal)):
        return None
    return Location(
        city=city[:200] if isinstance(city, str) and city else None,
        country=country[:200] if isinstance(country, str) and country else None,
        postal_code=postal[:50] if isinstance(postal, str) and postal else None,
    )


def _event(item: Any) -> TrackingEvent:
    if not isinstance(item, dict):
        raise SchemaDrift("an event is not an object")
    code, status, description = item.get("statusCode"), item.get("status"), item.get("description")
    if not isinstance(code, str) or not code:
        if not isinstance(status, str) or not status:
            raise SchemaDrift("an event has neither statusCode nor status")
        code = "unknown"
    text = next((t for t in (description, status) if isinstance(t, str) and t), None)
    return TrackingEvent(
        raw_status=code[:200],
        raw_description=text[:2000] if text else None,
        occurred_at=_timestamp(item.get("timestamp")),
        location=_location(item.get("location")),
    )


def parse_shipments(data: Any, tracking_number: str) -> list[TrackingEvent]:
    """Events of the shipment with this tracking number, oldest first."""
    if not isinstance(data, dict) or not isinstance(data.get("shipments"), list):
        raise SchemaDrift("the answer has no shipments list")
    shipments = [s for s in data["shipments"] if isinstance(s, dict)]
    if not shipments:
        raise ShipmentNotFound
    wanted = tracking_number.upper()
    shipment = next((s for s in shipments if str(s.get("id", "")).upper() == wanted), shipments[0])

    items = shipment.get("events")
    if not isinstance(items, list) or not items:
        # A shipment that was only announced has a current status but no history yet.
        if not isinstance(shipment.get("status"), dict):
            raise SchemaDrift("the shipment has neither events nor a status")
        items = [shipment["status"]]
    events = [_event(item) for item in items[:_MAX_EVENTS]]
    return sorted(events, key=lambda event: event.occurred_at)
