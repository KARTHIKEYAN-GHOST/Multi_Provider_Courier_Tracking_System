"""SkyKing adapter: the data service behind the tracking page skyking.co/track.

SkyKing publishes no API. Its tracking page fetches the history of a consignment note as
JSON in two steps, which this adapter repeats: it asks for the page's access key, then for
the history, naming itself the way the page does.

This is automated use of a website (tracking method ``scrape``): it needs SkyKing's
permission, recorded in the Engine with ``admin set-scraping-authorized`` before any job is
dispatched. The tracking page shows no CAPTCHA for a lookup (the one on that page guards
the "send me alerts" form, which this adapter never touches).
"""

from __future__ import annotations

import os
import re
from typing import Any, ClassVar

import httpx

from courier_adapter_sdk import (
    Adapter,
    JobContext,
    ParseError,
    RawResponse,
    ShipmentNotFound,
    TrackingResult,
    TrackRequest,
)
from courier_adapter_sdk.http import check_status

from .parser import parse_history

DEFAULT_BASE_URL = "https://live.skyking.co"
#: How the public tracking page identifies itself to the service. Not secrets: they are in
#: the page every visitor receives. Overridable should SkyKing assign its own values.
DEFAULT_CLIENT_KEY = "skm"
DEFAULT_CLIENT_EMAIL = "skm@flyking.co.in"
_NUMBER = re.compile(r"^[A-Za-z0-9]{4,20}$")


class SkykingAdapter(Adapter):
    key: ClassVar[str] = "skyking"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = (base_url or os.environ.get("SKYKING_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.client_key = os.environ.get("SKYKING_CLIENT_KEY") or DEFAULT_CLIENT_KEY
        self.client_email = os.environ.get("SKYKING_CLIENT_EMAIL") or DEFAULT_CLIENT_EMAIL
        self._transport = transport

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        if not _NUMBER.match(request.tracking_number):
            raise ShipmentNotFound  # not a consignment note number; the site is not asked
        client = ctx.http(transport=self._transport)

        key = await client.get(f"{self.base_url}/api/Track/GetAPIKeyValue")
        check_status(key)
        try:
            page_key: Any = key.json()
        except ValueError:
            page_key = None
        if not isinstance(page_key, str) or not page_key:
            raise ParseError("SkyKing's access key answer is not a text")

        response = await client.get(
            f"{self.base_url}/api/Track/ConsignmentMTrack_WebSite_AuthNew",
            params={"cnote": request.tracking_number, "key": self.client_key, "Email": self.client_email},
            headers={"KeyValue": page_key, "Accept": "application/json"},
        )
        check_status(response)
        ctx.keep_evidence(response)  # the history; never the access-key answer
        try:
            data: Any = response.json()
        except ValueError:
            raise ParseError("SkyKing's answer is not JSON") from None
        events = parse_history(data)
        return TrackingResult(RawResponse(content_type="application/json", body=data), events)
