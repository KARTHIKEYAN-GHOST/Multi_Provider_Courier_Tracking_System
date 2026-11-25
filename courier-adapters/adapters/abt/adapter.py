"""ABT Parcel Service adapter: the "Track Shipment" form on abtlogistics.com.

ABT publishes no API. Its website has a form that takes an LR number (or the booking
mobile number, which this adapter never sends) and answers with a page.

This is automated use of a website (tracking method ``scrape``): it needs ABT's permission,
recorded in the Engine with ``admin set-scraping-authorized`` before any job is dispatched.

PROVISIONAL. When this was written (November 2026) ABT had switched its online tracking
off ("Temporarily Under Maintenance"), so the result page could not be examined. The
adapter reports that state as a temporary failure, and reads a result page with the
general history-table reader in ``adapters/tables.py``. Once tracking is back, a saved
result page is needed to confirm or correct the reading.
"""

from __future__ import annotations

import os
import re
from typing import ClassVar

import httpx

from courier_adapter_sdk import (
    Adapter,
    JobContext,
    ProviderServerError,
    RawResponse,
    ShipmentNotFound,
    TrackingResult,
    TrackRequest,
)
from courier_adapter_sdk.http import check_status

from ..tables import history_events

DEFAULT_BASE_URL = "https://abtlogistics.com"
_NUMBER = re.compile(r"^[A-Za-z0-9/-]{3,30}$")
_MAINTENANCE = re.compile(r"under\s+maintenance|temporarily\s+unavailable", re.IGNORECASE)


class AbtAdapter(Adapter):
    key: ClassVar[str] = "abt"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = (base_url or os.environ.get("ABT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        if not _NUMBER.match(request.tracking_number):
            raise ShipmentNotFound  # not an LR number; the site is not asked
        client = ctx.http(transport=self._transport)
        response = await client.post(
            f"{self.base_url}/track_submit.php",
            data={"track_type": "lr", "mobile_lr": request.tracking_number},
        )
        if (
            300 <= response.status_code < 400
            and "maintenance" in response.headers.get("location", "").lower()
        ):
            raise ProviderServerError("ABT's online tracking is switched off for maintenance")
        check_status(response)
        html = response.text
        if _MAINTENANCE.search(html):
            raise ProviderServerError("ABT's online tracking is switched off for maintenance")
        ctx.keep_evidence(response)
        events = history_events(html)
        # The page is kept exactly as received, for debugging a parser that stops working.
        return TrackingResult(RawResponse(content_type="text/html", body=html), events)
