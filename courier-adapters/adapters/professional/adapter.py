"""The Professional Couriers (TPC) adapter: the tracking form of tpcindia.com.

The Professional Couriers publishes no API. Its home page, https://www.tpcindia.com/, has an
ASP.NET WebForms tracking form. Such a form is guarded by hidden fields (__VIEWSTATE,
__VIEWSTATEGENERATOR, __EVENTVALIDATION and their companions) that a POST must carry back
unchanged. This adapter reads those fields from the page, posts them with the consignment
number, and reads the answer.

This is automated use of a website (tracking method ``scrape``): it needs the company's
permission, recorded in the Engine with ``admin set-scraping-authorized`` before any job is
dispatched.

PROVISIONAL. The result page could not be examined without looking a consignment up, which
was not done. A result page is read with the general history-table reader in
``adapters/tables.py``; a saved result page is needed to confirm or correct the reading.
"""

from __future__ import annotations

import os
import re
from typing import ClassVar

import httpx

from courier_adapter_sdk import (
    Adapter,
    JobContext,
    RawResponse,
    ShipmentNotFound,
    TrackingResult,
    TrackRequest,
)
from courier_adapter_sdk.http import check_status

from ..tables import history_events
from .parser import hidden_fields

DEFAULT_BASE_URL = "https://www.tpcindia.com"
_NUMBER = re.compile(r"^[A-Za-z0-9]{5,30}$")
# The WebForms postback the SEARCH button makes, and the box the number is typed into.
_EVENT_TARGET = "ctl00$ctl00$ContentPlaceHolderForTop$ContentPlaceHolderQuickLinkForTop$Button1"
_TEXT_BOX = "ctl00$ctl00$ContentPlaceHolderForTop$ContentPlaceHolderQuickLinkForTop$TextBox1"


class ProfessionalAdapter(Adapter):
    key: ClassVar[str] = "professional"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = (base_url or os.environ.get("PROFESSIONAL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        if not _NUMBER.match(request.tracking_number):
            raise ShipmentNotFound  # not a consignment number; the site is not asked
        client = ctx.http(transport=self._transport)
        # Step 1: the form page, whose hidden fields guard the postback.
        form = await client.get(self.base_url)
        check_status(form)
        data = hidden_fields(form.text)
        # Step 2: post the hidden fields back with the number and the SEARCH postback.
        data["__EVENTTARGET"] = _EVENT_TARGET
        data["__EVENTARGUMENT"] = ""
        data[_TEXT_BOX] = request.tracking_number
        response = await client.post(self.base_url, data=data, follow_redirects=True)
        check_status(response)
        html = response.text
        ctx.keep_evidence(response)
        events = history_events(html)
        # The page is kept exactly as received, for debugging a parser that stops working.
        return TrackingResult(RawResponse(content_type="text/html", body=html), events)
