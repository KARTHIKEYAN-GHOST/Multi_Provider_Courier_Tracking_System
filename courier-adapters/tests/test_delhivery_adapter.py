"""Delhivery adapter: parsing API answers, and the HTTP exchange against a mocked Delhivery.

The answers are built from the field names in Delhivery's published API description; no
answer of the real service was available.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from adapters.delhivery import DelhiveryAdapter
from adapters.delhivery.parser import parse_packages
from courier_adapter_sdk import (
    AuthInvalid,
    ParseError,
    ProviderServerError,
    RateLimited,
    SchemaDrift,
    ShipmentNotFound,
)
from courier_adapter_sdk.credentials import CredentialDenied

from .adapter_support import run_track

WAYBILL = "72510016030"
TOKEN = "delhivery-token-123"
# The raw statuses the Engine has a mapping for (migration 0013).
MAPPED = {
    "UD:Manifested",
    "UD:Not Picked",
    "UD:In Transit",
    "UD:Pending",
    "UD:Dispatched",
    "DL:Delivered",
    "RT:In Transit",
    "RT:Pending",
    "RT:Dispatched",
    "DL:RTO",
    "DL:DTO",
    "CN:Canceled",
    "LT:LOST",
}


def scan(kind: str, label: str, when: str, where: str, note: str) -> dict[str, Any]:
    return {
        "ScanDetail": {
            "ScanDateTime": when,
            "ScanType": kind,
            "Scan": label,
            "StatusDateTime": when,
            "ScannedLocation": where,
            "Instructions": note,
            "StatusCode": "X-UCI",
        }
    }


def answer(*scans: dict[str, Any], waybill: str = WAYBILL) -> dict[str, Any]:
    return {
        "ShipmentData": [
            {
                "Shipment": {
                    "AWB": waybill,
                    "Origin": "Tirupur_Hub (Tamil Nadu)",
                    "Destination": "Chennai",
                    "Status": {
                        "Status": "Manifested",
                        "StatusType": "UD",
                        "StatusDateTime": "2026-11-24T09:00:00",
                        "StatusLocation": "Tirupur_Hub (Tamil Nadu)",
                        "Instructions": "Consignment Manifested",
                    },
                    "Scans": list(scans),
                }
            }
        ]
    }


DELIVERED = answer(
    scan("DL", "Delivered", "2026-11-26T10:30:00", "Chennai_Central (Tamil Nadu)", "Delivered to consignee"),
    scan("UD", "Manifested", "2026-11-24T09:00:00", "Tirupur_Hub (Tamil Nadu)", "Consignment Manifested"),
    scan("UD", "In Transit", "2026-11-24T21:15:00.328000", "Tirupur_Hub (Tamil Nadu)", "Shipment picked up"),
    scan("UD", "Dispatched", "2026-11-26T08:05:00", "Chennai_Central (Tamil Nadu)", "Out for delivery"),
)


# ----------------------------------------------------------------------------- parser


def test_scans_are_parsed_oldest_first_with_the_direction_in_the_status() -> None:
    events = parse_packages(DELIVERED, WAYBILL)
    assert [e.raw_status for e in events] == [
        "UD:Manifested",
        "UD:In Transit",
        "UD:Dispatched",
        "DL:Delivered",
    ]
    assert {e.raw_status for e in events} <= MAPPED
    assert events[-1].raw_description == "Delivered to consignee"
    assert events[-1].occurred_at == datetime(2026, 11, 26, 5, 0, tzinfo=UTC), "IST converted to UTC"
    location = events[-1].location
    assert location is not None
    assert (location.city, location.state, location.country) == ("Chennai_Central", "Tamil Nadu", "IN")


def test_a_return_in_transit_is_told_apart_from_a_forward_one() -> None:
    data = answer(scan("RT", "In Transit", "2026-11-27T10:00:00", "Chennai", "Returning to origin"))
    assert [e.raw_status for e in parse_packages(data, WAYBILL)] == ["RT:In Transit"]


def test_a_shipment_without_scans_yields_its_current_status() -> None:
    (event,) = parse_packages(answer(), WAYBILL)
    assert (event.raw_status, event.raw_description) == ("UD:Manifested", "Consignment Manifested")


def test_the_shipment_matching_the_waybill_is_chosen_when_several_are_returned() -> None:
    data = answer(scan("UD", "Pending", "2026-11-25T10:00:00", "Salem", "Reached"), waybill="OTHER1")
    data["ShipmentData"].append(DELIVERED["ShipmentData"][0])
    assert len(parse_packages(data, WAYBILL)) == 4
    assert [e.raw_status for e in parse_packages(data, "other1")] == ["UD:Pending"]


@pytest.mark.parametrize(
    "data",
    [
        {"ShipmentData": []},
        {"Error": "No such waybill or Order Id found", "Success": False},
        {"ShipmentData": [], "Error": "Data does not exist for provided Waybill(s)"},
    ],
)
def test_an_answer_without_a_shipment_means_not_found(data: Any) -> None:
    with pytest.raises(ShipmentNotFound):
        parse_packages(data, WAYBILL)


def test_an_error_about_the_token_is_not_mistaken_for_an_unknown_waybill() -> None:
    with pytest.raises(AuthInvalid) as raised:
        parse_packages({"Error": "Invalid token zzz-secret-zzz"}, WAYBILL)
    assert "zzz" not in str(raised.value)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"ShipmentData": "none"},
        {"ShipmentData": [{"Shipment": {"AWB": WAYBILL}}]},
        {"ShipmentData": [{"Shipment": {"AWB": WAYBILL, "Scans": ["delivered"]}}]},
        {"ShipmentData": [{"Shipment": {"AWB": WAYBILL, "Scans": [{"ScanDetail": {"ScanType": "UD"}}]}}]},
        {"ShipmentData": [{"Shipment": {"AWB": WAYBILL, "Scans": [{"ScanDetail": {"Scan": "Pending"}}]}}]},
    ],
    ids=["list", "empty", "bad-data", "no-status", "scan-not-object", "no-label", "no-date"],
)
def test_an_answer_with_a_different_structure_is_schema_drift(data: Any) -> None:
    with pytest.raises(SchemaDrift):
        parse_packages(data, WAYBILL)


# ----------------------------------------------------------------------------- adapter


async def track(handler: Any, secret: dict[str, str] | None = None) -> Any:
    adapter = DelhiveryAdapter(transport=httpx.MockTransport(handler))
    return await run_track(adapter, WAYBILL, secret or {"token": TOKEN})


async def test_the_request_goes_to_delhivery_with_the_token_in_the_header_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DELIVERED)

    result = await track(handler)
    (request,) = seen
    assert request.method == "GET"
    assert request.url.host == "track.delhivery.com"
    assert request.url.path == "/api/v1/packages/json/"
    assert dict(request.url.params) == {"waybill": WAYBILL}
    assert request.headers["authorization"] == f"Token {TOKEN}"
    assert TOKEN not in str(request.url), "the token is never part of the URL, which servers and proxies log"

    assert result.events[-1].raw_status == "DL:Delivered"
    assert result.raw_response.body == DELIVERED
    assert TOKEN not in json.dumps(result.raw_response.body)


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, ShipmentNotFound), (401, AuthInvalid), (429, RateLimited), (503, ProviderServerError)],
)
async def test_delhivery_error_statuses_are_classified(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await track(lambda request: httpx.Response(status, text="You couldn't be authenticated"))


async def test_an_answer_that_is_not_json_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        await track(lambda request: httpx.Response(200, text="<html>maintenance</html>"))


async def test_a_credential_without_a_token_field_is_refused_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Delhivery must not be contacted")

    with pytest.raises(CredentialDenied):
        await track(handler, {"api_key": "wrong-field"})
