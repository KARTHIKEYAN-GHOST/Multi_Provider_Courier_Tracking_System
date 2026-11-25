"""Balaji Roadlines adapter: the "LR Tracking" page of Sri Balaji Transport Lines (Regd.).

Several unrelated transporters trade as "Balaji Roadlines". This adapter is for the one
with a Tirupur branch and an online LR tracking page, sribalajitransportlines.com (mail
domain sbrl.in). If another company is meant, this adapter does not apply to it.

The company publishes no API. Its page takes an LR number in the address
(``/Website/tracking?lrno=...``) and answers with a page.

This is automated use of a website (tracking method ``scrape``): it needs the company's
permission, recorded in the Engine with ``admin set-scraping-authorized`` before any job is
dispatched.

PROVISIONAL. The result page could not be examined without looking a consignment up, which
was not done. A result page is read with the general history-table reader in
``adapters/tables.py``; a saved result page is needed to confirm or correct the reading.

The site's certificate had expired when this was written, so the default address is plain
HTTP: an LR number travels unencrypted, as it does for a visitor's browser.
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

DEFAULT_BASE_URL = "http://sribalajitransportlines.com"
_NUMBER = re.compile(r"^[A-Za-z0-9/-]{3,30}$")


class BalajiAdapter(Adapter):
    key: ClassVar[str] = "balaji"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = (base_url or os.environ.get("BALAJI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        if not _NUMBER.match(request.tracking_number):
            raise ShipmentNotFound  # not an LR number; the site is not asked
        client = ctx.http(transport=self._transport)
        response = await client.get(
            f"{self.base_url}/Website/tracking", params={"lrno": request.tracking_number}
        )
        if response.status_code == 404:
            raise ShipmentNotFound
        check_status(response)
        html = response.text
        events = history_events(html)
        # The page is kept exactly as received, for debugging a parser that stops working.
        return TrackingResult(RawResponse(content_type="text/html", body=html), events)
