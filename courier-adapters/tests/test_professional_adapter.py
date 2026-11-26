"""Tests of the Professional Couriers adapter: the WebForms form of tpcindia.com.

The saved page is the form of step 1, not a result page, so the tests check that its hidden
fields and the consignment number reach the POST, that a bad number is refused without asking
the site, and that a form without __VIEWSTATE is schema drift.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from adapters.professional import ProfessionalAdapter
from courier_adapter_sdk import SchemaDrift, ShipmentNotFound

from .adapter_support import run_track

_FIXTURES = Path(__file__).parent / "fixtures" / "professional"

_BUTTON = "ctl00$ctl00$ContentPlaceHolderForTop$ContentPlaceHolderQuickLinkForTop$Button1"
_TEXT_BOX = "ctl00$ctl00$ContentPlaceHolderForTop$ContentPlaceHolderQuickLinkForTop$TextBox1"

# A stand-in result page: the real one could not be examined. It is only enough of a history
# table for the reader to return one event, so the whole flow can be exercised.
_RESULT_PAGE = """<html><body>
<table>
<tr><th>Date</th><th>Status</th><th>Location</th></tr>
<tr><td>25-11-2026 10:30</td><td>Delivered</td><td>Salem</td></tr>
</table>
</body></html>"""

# A form without __VIEWSTATE: not the page this adapter knows.
_NO_VIEWSTATE = (
    "<html><body><form><input type='hidden' name='__EVENTVALIDATION' value='x' /></form></body></html>"
)


def _form() -> str:
    return (_FIXTURES / "sample.html").read_text(encoding="utf-8")


async def test_hidden_fields_and_number_are_posted() -> None:
    form = _form()
    posted: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=form)
        posted.update(parse_qs(request.content.decode(), keep_blank_values=True))
        return httpx.Response(200, text=_RESULT_PAGE)

    adapter = ProfessionalAdapter(transport=httpx.MockTransport(handler))
    result = await run_track(adapter, "ABC123456")

    assert posted["__VIEWSTATE"] == ["EXAMPLEVIEWSTATEVALUE0001=="]
    assert posted["__VIEWSTATEGENERATOR"] == ["CA0B0334"]
    assert posted["__EVENTVALIDATION"] == ["EXAMPLEEVENTVALIDATION0001=="]
    assert posted["__LASTFOCUS"] == [""]
    assert posted["__VIEWSTATEENCRYPTED"] == [""]
    assert posted["__EVENTTARGET"] == [_BUTTON]
    assert posted["__EVENTARGUMENT"] == [""]
    assert posted[_TEXT_BOX] == ["ABC123456"]
    assert [event.raw_status for event in result.events] == ["delivered"]


async def test_short_number_is_not_asked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the site must not be asked for a bad number")

    adapter = ProfessionalAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ShipmentNotFound):
        await run_track(adapter, "AB")


async def test_number_with_a_hyphen_is_not_asked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the site must not be asked for a bad number")

    adapter = ProfessionalAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(ShipmentNotFound):
        await run_track(adapter, "ABC-123")


async def test_missing_viewstate_is_schema_drift() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_NO_VIEWSTATE)

    adapter = ProfessionalAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(SchemaDrift):
        await run_track(adapter, "ABC123456")
