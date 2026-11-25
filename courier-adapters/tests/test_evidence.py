"""An answer the adapter could not read travels to the Engine with the failure."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import httpx
import pytest

from courier_adapter_sdk import (
    Adapter,
    JobContext,
    ParseError,
    ProviderServerError,
    RawResponse,
    SchemaDrift,
    ShipmentNotFound,
    TrackingResult,
    TrackRequest,
)
from courier_adapter_sdk.context import MAX_EVIDENCE_CHARS
from courier_adapter_sdk.worker import Worker

from .adapter_support import job_ctx
from .conftest import FakeDelivery, FakePublisher, job_message, make_settings, schema_errors

PAGE = "<html><body><div class='new-layout'>Shipment 42 is on its way</div></body></html>"


class Site(Adapter):
    """Fetches a page, keeps it as evidence, then fails or succeeds as told."""

    key: ClassVar[str] = "dummy"
    version: ClassVar[str] = "0.1.0"

    def __init__(self, failure: Exception | None, keep: bool = True) -> None:
        self.failure, self.keep = failure, keep

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        client = ctx.http(transport=httpx.MockTransport(lambda r: httpx.Response(200, html=PAGE)))
        response = await client.get("http://courier.example/track")
        if self.keep:
            ctx.keep_evidence(response)
        if self.failure is not None:
            raise self.failure
        return TrackingResult(RawResponse(content_type="text/html", body=response.text), [])


async def result_of(adapter: Adapter) -> dict[str, Any]:
    publisher = FakePublisher()
    worker = Worker(adapter, make_settings(), publisher)
    await worker.submit(FakeDelivery(job_message("X")))
    await publisher.wait_for_results(1)
    result: dict[str, Any] = publisher.results[0]
    assert schema_errors(result) == []
    payload: dict[str, Any] = result["payload"]
    return payload


@pytest.mark.parametrize("failure", [SchemaDrift("no history table"), ParseError("unknown date")])
async def test_an_unreadable_answer_is_sent_with_the_failure(failure: Exception) -> None:
    payload = await result_of(Site(failure))
    assert payload["outcome"] == "FAILURE"
    assert payload["raw_response"] == {"content_type": "text/html", "body": PAGE}
    assert "events" not in payload or payload["events"] in (None, [])


@pytest.mark.parametrize(
    "failure", [ProviderServerError("HTTP 500"), RuntimeError("bug"), ShipmentNotFound()]
)
async def test_other_outcomes_carry_no_evidence(failure: Exception) -> None:
    payload = await result_of(Site(failure))
    assert payload["outcome"] in ("FAILURE", "NOT_FOUND")
    assert payload.get("raw_response") is None


async def test_an_adapter_that_kept_nothing_fails_as_before() -> None:
    payload = await result_of(Site(SchemaDrift("x"), keep=False))
    assert (payload["outcome"], payload.get("raw_response")) == ("FAILURE", None)


async def test_a_success_reports_its_own_raw_response_not_the_evidence() -> None:
    payload = await result_of(Site(None))
    assert (payload["outcome"], payload["raw_response"]["body"]) == ("SUCCESS", PAGE)


def test_json_is_kept_as_json_and_a_huge_answer_is_cut() -> None:
    ctx = job_ctx()
    ctx.keep_evidence(httpx.Response(200, json={"shipments": "none"}))
    assert ctx.evidence == RawResponse(content_type="application/json", body={"shipments": "none"})
    ctx.keep_evidence(httpx.Response(200, text="x" * (MAX_EVIDENCE_CHARS + 50)))
    assert ctx.evidence is not None
    assert (ctx.evidence.content_type, len(ctx.evidence.body)) == ("text/plain", MAX_EVIDENCE_CHARS)
    ctx.keep_evidence(httpx.Response(200, headers={"content-type": "application/json"}, text="{not json"))
    assert ctx.evidence.body == "{not json"
    assert len(json.dumps(ctx.evidence.body)) < 100
