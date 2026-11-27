"""Demo Secure Logistics adapter: parsing saved pages, and a signed-in lookup over a mock site."""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from adapters.demosecure import DemoSecureAdapter
from adapters.demosecure.parser import form_token, is_login_page, parse_tracking_page
from courier_adapter_sdk import SchemaDrift, ShipmentNotFound

from .adapter_support import run_track

FIXTURES = Path(__file__).parent / "fixtures" / "demosecure"
SECRET = {"email": "someone@example.com", "password": "not-a-real-password"}
TOKEN = "abcdef0123456789abcdef0123456789"
LOGIN_FORM = (
    '<html><body><form method="post" action="/secure/login">'
    f'<input type="hidden" name="form_token" value="{TOKEN}">'
    '<input name="email"><input name="password" type="password"><button>Sign in</button>'
    "</form></body></html>"
)


def page(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text()


# ----------------------------------------------------------------------------- parser (saved pages)


def test_a_delivered_page_is_parsed_into_its_five_events_oldest_first() -> None:
    events = parse_tracking_page(page("result_page"))
    assert [e.raw_status for e in events] == [
        "booked",
        "picked-up",
        "in-transit",
        "out-for-delivery",
        "delivered",
    ]
    assert [e.raw_description for e in events] == [
        "Booked",
        "Picked up",
        "In transit",
        "Out for delivery",
        "Delivered",
    ]
    first, last = events[0], events[-1]
    assert first.occurred_at == datetime(2026, 4, 12, 4, 11, tzinfo=UTC), "09:41 IST is 04:11 UTC"
    assert last.occurred_at == datetime(2026, 4, 14, 0, 11, tzinfo=UTC), "05:41 IST is 00:11 UTC"
    assert first.location is not None
    assert (first.location.city, first.location.country, first.location.raw) == (
        "Chennai",
        "IN",
        "Chennai",
    )
    assert last.location is not None
    assert last.location.city == "Coimbatore"
    assert [e.occurred_at for e in events] == sorted(e.occurred_at for e in events)


def test_the_sites_not_found_page_is_recognized() -> None:
    with pytest.raises(ShipmentNotFound):
        parse_tracking_page(page("notfound_page"))


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html><body>maintenance</body></html>",
        '<ol class="scans"></ol>',
        '<ol class="scans"><li class="scan"><span class="ts">14-04-2026 05:41</span></li></ol>',
    ],
    ids=["empty", "other-page", "no-scans", "missing-description"],
)
def test_pages_without_the_expected_structure_are_schema_drift(html: str) -> None:
    with pytest.raises(SchemaDrift):
        parse_tracking_page(html)


def test_the_sign_in_form_and_its_token_are_recognized() -> None:
    assert is_login_page(LOGIN_FORM)
    assert form_token(LOGIN_FORM) == TOKEN
    assert not is_login_page(page("result_page"))


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>no form</body></html>",
        LOGIN_FORM.replace(TOKEN, "../../admin"),
        LOGIN_FORM.replace(f' value="{TOKEN}"', ""),
    ],
    ids=["no-form", "odd-token", "no-token"],
)
def test_a_sign_in_page_without_a_usable_token_is_schema_drift(html: str) -> None:
    with pytest.raises(SchemaDrift):
        form_token(html)


# ----------------------------------------------------------------------------- adapter (mock site)


def site(
    tracking_html: str, sign_in_html: str = "<html><body>Signed in</body></html>"
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/secure/login" and request.method == "GET":
            return httpx.Response(200, html=LOGIN_FORM)
        if request.url.path == "/secure/login" and request.method == "POST":
            return httpx.Response(200, html=sign_in_html, headers={"set-cookie": "session=s3ss10n; Path=/"})
        if request.url.path == "/secure/track" and request.method == "GET":
            return httpx.Response(200, html=tracking_html)
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


async def test_a_lookup_signs_in_and_reads_the_tracking_page() -> None:
    transport, seen = site(page("result_page"))
    adapter = DemoSecureAdapter(base_url="http://127.0.0.1:18090", transport=transport)
    result = await run_track(adapter, "DEMO-OK-1", SECRET)
    assert [e.raw_status for e in result.events][-1] == "delivered"
    assert len(result.events) == 5
    assert result.raw_response.body == page("result_page")

    sign_in = next(r for r in seen if r.method == "POST")
    form = urllib.parse.parse_qs(sign_in.content.decode())
    assert form["form_token"] == [TOKEN]
    assert form["email"] == [SECRET["email"]]
    assert form["password"] == [SECRET["password"]]
    track = seen[-1]
    assert track.url.path == "/secure/track"
    assert track.url.params["number"] == "DEMO-OK-1"


async def test_an_unknown_consignment_is_not_found() -> None:
    transport, _ = site(page("notfound_page"))
    adapter = DemoSecureAdapter(base_url="http://127.0.0.1:18090", transport=transport)
    with pytest.raises(ShipmentNotFound):
        await run_track(adapter, "DEMO-NOPE-1", SECRET)


async def test_a_number_that_cannot_be_a_consignment_is_not_sent() -> None:
    transport, seen = site(page("result_page"))
    adapter = DemoSecureAdapter(base_url="http://127.0.0.1:18090", transport=transport)
    with pytest.raises(ShipmentNotFound):
        await run_track(adapter, "<script>", SECRET)
    assert seen == []


async def test_a_refused_sign_in_is_schema_drift() -> None:
    transport, _ = site(page("result_page"), sign_in_html=LOGIN_FORM)
    adapter = DemoSecureAdapter(base_url="http://127.0.0.1:18090", transport=transport)
    with pytest.raises(SchemaDrift):
        await run_track(adapter, "DEMO-OK-1", SECRET)


async def test_a_tracking_request_answered_with_the_sign_in_form_is_schema_drift() -> None:
    transport, _ = site(LOGIN_FORM)
    adapter = DemoSecureAdapter(base_url="http://127.0.0.1:18090", transport=transport)
    with pytest.raises(SchemaDrift):
        await run_track(adapter, "DEMO-OK-1", SECRET)
