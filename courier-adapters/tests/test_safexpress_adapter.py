"""Safexpress adapter: parsing tracking answers, and the two-step exchange against a mock.

The answers are built from the fields Safexpress's website reads; no answer of the real
customer API was available (see the adapter's module text).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from adapters.safexpress import SafexpressAdapter
from adapters.safexpress.parser import parse_tracking
from courier_adapter_sdk import AuthInvalid, ParseError, ProviderServerError, SchemaDrift, ShipmentNotFound

from .adapter_support import run_track

WAYBILL = "123456789"
SECRET = {"client_id": "client-abc", "client_secret": "secret-xyz-987", "api_key": "gateway-key-555"}
ACCESS = "short-lived-access-token"

DELIVERED = {
    "shipment": {
        "result": "success",
        "waybill": WAYBILL,
        "tracking": [
            {
                "eventdt": "26-11-2026 10:30:00",
                "eventdesc": "Waybill Delivered: Delivered Date",
                "status": "Delivered",
            },
            {
                "eventdt": "26-11-2026 08:00:00",
                "eventdesc": "Out for Delivery from Chennai",
                "status": "Out for Delivery",
            },
            {
                "eventdt": "25-11-2026 23:10:00",
                "eventdesc": "Waybill reached final destination",
                "status": "Arrived at Destination",
            },
            {
                "eventdt": "24-11-2026 20:00:00",
                "eventdesc": "RPS has been closed Successfully",
                "status": "In Transit",
            },
            {
                "eventdt": "24-11-2026 14:05:00",
                "eventdesc": "Waybill Generated For 3 packages",
                "status": "Booked",
            },
        ],
    }
}


def test_tracking_entries_are_parsed_oldest_first() -> None:
    events = parse_tracking(DELIVERED)
    assert [e.raw_status for e in events] == [
        "booked",
        "in-transit",
        "in-transit",
        "out-for-delivery",
        "delivered",
    ]
    assert events[0].raw_description == "Waybill Generated For 3 packages"
    assert events[0].occurred_at == datetime(2026, 11, 24, 8, 35, tzinfo=UTC), "IST converted to UTC"


def test_an_entry_without_a_status_label_is_classified_by_its_sentence() -> None:
    data = {
        "shipment": {
            "result": "success",
            "tracking": [{"eventdt": "2026-11-24 14:05:00", "eventdesc": "Out for Delivery"}],
        }
    }
    (event,) = parse_tracking(data)
    assert event.raw_status == "out-for-delivery"


@pytest.mark.parametrize(
    "data",
    [{"shipment": {"result": "failed"}}, {"shipment": {"result": "success", "tracking": []}}],
)
def test_a_failed_or_empty_answer_means_not_found(data: Any) -> None:
    with pytest.raises(ShipmentNotFound):
        parse_tracking(data)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"shipment": "x"},
        {"shipment": {"result": "success"}},
        {"shipment": {"result": "success", "tracking": ["Delivered"]}},
        {"shipment": {"result": "success", "tracking": [{"eventdt": "24-11-2026"}]}},
        {"shipment": {"result": "success", "tracking": [{"status": "Booked"}]}},
    ],
    ids=["list", "empty", "bad-shipment", "no-tracking", "entry-not-object", "no-status", "no-date"],
)
def test_an_answer_with_a_different_structure_is_schema_drift(data: Any) -> None:
    with pytest.raises(SchemaDrift):
        parse_tracking(data)


# ----------------------------------------------------------------------------- adapter


async def track(handler: Any) -> Any:
    return await run_track(SafexpressAdapter(transport=httpx.MockTransport(handler)), WAYBILL, SECRET)


async def test_a_token_is_fetched_with_the_client_credentials_and_used_for_the_lookup() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api-auth.safexpress.com":
            return httpx.Response(
                200, json={"access_token": ACCESS, "expires_in": 3600, "token_type": "Bearer"}
            )
        return httpx.Response(200, json=DELIVERED)

    result = await track(handler)
    token_request, lookup = seen
    assert (token_request.method, token_request.url.path) == ("POST", "/oauth2/token")
    basic = base64.b64encode(f"{SECRET['client_id']}:{SECRET['client_secret']}".encode()).decode()
    assert token_request.headers["authorization"] == f"Basic {basic}"
    assert token_request.content == b"grant_type=client_credentials&scope=server%2Fwaybillapps"

    assert lookup.method == "POST"
    assert lookup.url.host == "apigateway.safexpress.com"
    assert json.loads(lookup.content) == {"docNo": WAYBILL, "docType": "WB"}
    assert lookup.headers["authorization"] == ACCESS
    assert lookup.headers["x-api-key"] == SECRET["api_key"]
    for request in seen:
        for value in (*SECRET.values(), ACCESS):
            assert value not in str(request.url), "no secret is ever part of a URL"

    assert result.events[-1].raw_status == "delivered"
    body = json.dumps(result.raw_response.body)
    assert all(value not in body for value in (*SECRET.values(), ACCESS))


@pytest.mark.parametrize("status", [400, 401])
async def test_refused_client_credentials_are_an_auth_failure_and_no_lookup_is_made(status: int) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(status, json={"error": "invalid_client"})

    with pytest.raises(AuthInvalid) as raised:
        await track(handler)
    assert calls == ["api-auth.safexpress.com"]
    assert SECRET["client_secret"] not in str(raised.value)


async def test_a_token_answer_without_a_token_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        await track(lambda request: httpx.Response(200, json={"token_type": "Bearer"}))


@pytest.mark.parametrize(
    ("status", "error"), [(404, ShipmentNotFound), (401, AuthInvalid), (500, ProviderServerError)]
)
async def test_lookup_error_statuses_are_classified(status: int, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api-auth.safexpress.com":
            return httpx.Response(200, json={"access_token": ACCESS})
        return httpx.Response(status, json={"message": "no"})

    with pytest.raises(error):
        await track(handler)


async def test_the_addresses_and_scope_can_be_set_to_what_safexpress_documents() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"access_token": ACCESS} if "token" in request.url.path else DELIVERED
        )

    adapter = SafexpressAdapter(
        token_url="https://auth.example/oauth2/token",
        tracking_url="https://gw.example/v2/track",
        scope="server/other",
        transport=httpx.MockTransport(handler),
    )
    await run_track(adapter, WAYBILL, SECRET)
    assert [str(r.url) for r in seen] == ["https://auth.example/oauth2/token", "https://gw.example/v2/track"]
    assert b"scope=server%2Fother" in seen[0].content
