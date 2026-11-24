"""DHL adapter: parsing saved API answers, and the HTTP exchange against a mocked DHL."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from adapters.dhl import DhlAdapter
from adapters.dhl.parser import parse_shipments
from courier_adapter_sdk import (
    AuthInvalid,
    JobContext,
    ParseError,
    ProviderServerError,
    RateLimited,
    SchemaDrift,
    ShipmentNotFound,
    TrackRequest,
)
from courier_adapter_sdk.contracts import new_id
from courier_adapter_sdk.credentials import CredentialDenied, Secret
from courier_adapter_sdk.logging import get_logger

FIXTURES = Path(__file__).parent / "fixtures" / "dhl"
NUMBER = "00340434292135100186"
API_KEY = "dhl-key-123"
# The raw statuses the Engine has a mapping for (migration 0010).
MAPPED = {"pre-transit", "transit", "delivered", "failure", "unknown"}


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# ----------------------------------------------------------------------------- parser


def test_a_delivered_shipment_is_parsed_oldest_event_first() -> None:
    events = parse_shipments(load("delivered.json"), NUMBER)
    assert [e.raw_status for e in events] == ["pre-transit", "transit", "delivered"]
    assert {e.raw_status for e in events} <= MAPPED
    assert events[-1].raw_description == "The shipment has been successfully delivered"
    assert events[-1].occurred_at == datetime(2026, 3, 4, 8, 35, tzinfo=UTC), "+05:30 converted to UTC"
    assert events[1].occurred_at == datetime(2026, 3, 2, 22, 40, tzinfo=UTC)
    assert events[0].occurred_at == datetime(2026, 3, 1, 9, 15, tzinfo=UTC), "no offset given: taken as UTC"
    location = events[-1].location
    assert location is not None
    assert (location.city, location.country, location.postal_code) == ("Chennai", "IN", "600001")


def test_a_shipment_without_history_yields_its_current_status() -> None:
    (event,) = parse_shipments(load("announced_no_events.json"), "JD014600006281230704")
    assert (event.raw_status, event.raw_description) == ("pre-transit", "Shipment information received")
    assert event.location is None


def test_the_shipment_matching_the_number_is_chosen_when_several_are_returned() -> None:
    data = load("delivered.json")
    other = {"id": "OTHER", "status": {"timestamp": "2026-01-01T00:00:00Z", "statusCode": "failure"}}
    data["shipments"].insert(0, other)
    assert len(parse_shipments(data, NUMBER.lower())) == 3
    assert [e.raw_status for e in parse_shipments(data, "OTHER")] == ["failure"]


def test_an_empty_shipments_list_means_not_found() -> None:
    with pytest.raises(ShipmentNotFound):
        parse_shipments({"shipments": []}, NUMBER)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"shipments": "none"},
        {"shipments": [{"id": NUMBER}]},
        {"shipments": [{"id": NUMBER, "events": [{"timestamp": "2026-01-01T00:00:00Z"}]}]},
        {"shipments": [{"id": NUMBER, "events": [{"statusCode": "transit"}]}]},
        {"shipments": [{"id": NUMBER, "events": ["delivered"]}]},
    ],
    ids=["list", "empty", "bad-shipments", "no-status", "no-status-code", "no-timestamp", "event-not-object"],
)
def test_an_answer_with_a_different_structure_is_schema_drift(data: Any) -> None:
    with pytest.raises(SchemaDrift):
        parse_shipments(data, NUMBER)


def test_an_unreadable_timestamp_is_a_parse_error_that_does_not_quote_the_value() -> None:
    data = {
        "shipments": [
            {"id": NUMBER, "events": [{"timestamp": "<script>x</script>", "statusCode": "transit"}]}
        ]
    }
    with pytest.raises(ParseError) as raised:
        parse_shipments(data, NUMBER)
    assert "script" not in str(raised.value)


def test_an_event_with_only_free_text_gets_the_unknown_code() -> None:
    data = {
        "shipments": [{"id": NUMBER, "events": [{"timestamp": "2026-01-01T00:00:00Z", "status": "Held"}]}]
    }
    (event,) = parse_shipments(data, NUMBER)
    assert (event.raw_status, event.raw_description) == ("unknown", "Held")


# ----------------------------------------------------------------------------- adapter


class StaticResolver:
    def __init__(self, secret: Secret) -> None:
        self.secret = secret

    async def resolve(self, credential_ref: str, job_id: str, attempt: int) -> Secret:
        return self.secret


def ctx(secret: Secret | None = None) -> JobContext:
    resolver = StaticResolver(secret or Secret({"api_key": API_KEY}))
    return JobContext(new_id(), 1, new_id(), 0, new_id(), get_logger(), new_id(), resolver)


async def track(handler: Any, context: JobContext | None = None) -> Any:
    adapter = DhlAdapter(transport=httpx.MockTransport(handler))
    context = context or ctx()
    try:
        return await adapter.track(TrackRequest("dhl", NUMBER), context)
    finally:
        await context._close(1.0)


async def test_the_request_goes_to_dhl_with_the_key_in_the_header_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load("delivered.json"))

    result = await track(handler)
    (request,) = seen
    assert request.url.host == "api-eu.dhl.com"
    assert request.url.path == "/track/shipments"
    assert dict(request.url.params) == {"trackingNumber": NUMBER}
    assert request.headers["dhl-api-key"] == API_KEY
    assert API_KEY not in str(request.url), "the key is never part of the URL, which servers and proxies log"

    assert [e.raw_status for e in result.events] == ["pre-transit", "transit", "delivered"]
    assert result.raw_response.content_type == "application/json"
    assert result.raw_response.body == load("delivered.json")
    assert API_KEY not in json.dumps(result.raw_response.body)


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, ShipmentNotFound), (401, AuthInvalid), (429, RateLimited), (500, ProviderServerError)],
)
async def test_dhl_error_statuses_are_classified(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await track(lambda request: httpx.Response(status, json=load("not_found.json")))


async def test_an_answer_that_is_not_json_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        await track(lambda request: httpx.Response(200, text="<html>maintenance</html>"))


async def test_a_credential_without_an_api_key_field_is_reported_as_a_credential_problem() -> None:
    with pytest.raises(CredentialDenied, match="api_key"):
        await track(lambda request: httpx.Response(200, json={}), ctx(Secret({"token": "x"})))


def test_the_base_url_can_be_overridden_for_tests_and_other_regions(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DhlAdapter().base_url == "https://api-eu.dhl.com"
    monkeypatch.setenv("DHL_BASE_URL", "http://127.0.0.1:9999/")
    assert DhlAdapter().base_url == "http://127.0.0.1:9999"
    assert DhlAdapter(base_url="https://api.dhl.com").base_url == "https://api.dhl.com"
