"""ABT and Balaji adapters: the request each sends, and how an answer page is treated.

Both read a result page with the general history-table reader (tests/test_india_helpers.py).
The result pages here are made up: neither site's real result page could be examined.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from adapters.abt import AbtAdapter
from adapters.balaji import BalajiAdapter
from courier_adapter_sdk import (
    Adapter,
    Blocked,
    ProviderServerError,
    RateLimited,
    SchemaDrift,
    ShipmentNotFound,
)

from .adapter_support import run_track

LR = "TPR/12345"
RESULT = """
<html><body><h3>LR Status</h3>
<table>
  <tr><th>Date</th><th>Station</th><th>Status</th></tr>
  <tr><td>25-11-2026 14:05</td><td>Tirupur</td><td>Booked</td></tr>
  <tr><td>26-11-2026 06:00</td><td>Salem</td><td>Arrived at Salem</td></tr>
  <tr><td>27-11-2026 11:20</td><td>Chennai</td><td>Delivered</td></tr>
</table></body></html>
"""
MAINTENANCE = (
    "<html><body><h1>Temporarily Under Maintenance</h1><p>Please call our toll-free number.</p></body></html>"
)

ADAPTERS: list[type[Adapter]] = [AbtAdapter, BalajiAdapter]


async def track(kind: Any, handler: Any, number: str = LR) -> Any:
    return await run_track(kind(transport=httpx.MockTransport(handler)), number)


async def test_abt_submits_the_tracking_form_with_the_lr_number_and_never_a_phone_number() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=RESULT)

    result = await track(AbtAdapter, handler)
    (request,) = seen
    assert (request.method, request.url.host, request.url.path) == (
        "POST",
        "abtlogistics.com",
        "/track_submit.php",
    )
    assert parse_qs(request.content.decode()) == {"track_type": ["lr"], "mobile_lr": [LR]}
    assert [e.raw_status for e in result.events] == ["booked", "in-transit", "delivered"]
    assert (result.raw_response.content_type, result.raw_response.body) == ("text/html", RESULT)


async def test_balaji_asks_the_lr_tracking_page() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=RESULT)

    result = await track(BalajiAdapter, handler)
    (request,) = seen
    assert (request.method, request.url.host, request.url.path) == (
        "GET",
        "sribalajitransportlines.com",
        "/Website/tracking",
    )
    assert dict(request.url.params) == {"lrno": LR}
    assert result.events[-1].raw_status == "delivered"
    assert result.raw_response.body == RESULT


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text=MAINTENANCE),
        httpx.Response(302, headers={"location": "maintenance.html"}),
    ],
    ids=["page", "redirect"],
)
async def test_abt_tracking_switched_off_is_a_temporary_failure_not_a_missing_shipment(
    response: httpx.Response,
) -> None:
    with pytest.raises(ProviderServerError) as raised:
        await track(AbtAdapter, lambda request: response)
    assert raised.value.retryable


@pytest.mark.parametrize("kind", ADAPTERS)
@pytest.mark.parametrize("number", ["12 34", "9876543210; x", "<b>1</b>", "x" * 31, "ab"])
async def test_something_that_is_not_an_lr_number_is_not_sent_to_the_site(kind: Any, number: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the site must not be contacted")

    with pytest.raises(ShipmentNotFound):
        await track(kind, handler, number)


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_a_page_saying_no_record_found_is_not_found(kind: Any) -> None:
    with pytest.raises(ShipmentNotFound):
        await track(kind, lambda request: httpx.Response(200, text="<p>No Record Found</p>"))


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_a_result_page_in_an_unknown_layout_is_schema_drift_not_a_guess(kind: Any) -> None:
    page = "<html><body><div class='card'>LR 12345 is on the way</div></body></html>"
    with pytest.raises(SchemaDrift):
        await track(kind, lambda request: httpx.Response(200, text=page))


@pytest.mark.parametrize("kind", ADAPTERS)
@pytest.mark.parametrize(
    ("status", "error"), [(403, Blocked), (429, RateLimited), (500, ProviderServerError), (301, Blocked)]
)
async def test_a_refusal_is_classified(kind: Any, status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await track(kind, lambda request: httpx.Response(status, text="no"))
