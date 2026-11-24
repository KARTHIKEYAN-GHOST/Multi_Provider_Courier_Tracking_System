"""Scraping adapter for the demo courier website that ships with this project.

It is the practice target for HTML scraping: fetch one page, parse it. It contacts only the
configured demo site. Scraping a real courier's website needs that courier's permission,
recorded in the Engine (``admin set-scraping-authorized``) before any job is dispatched.
"""

from __future__ import annotations

import os
from typing import ClassVar

import httpx

from courier_adapter_sdk import Adapter, JobContext, RawResponse, TrackingResult, TrackRequest
from courier_adapter_sdk.http import check_status

from .parser import parse_tracking_page

DEFAULT_BASE_URL = "http://127.0.0.1:8090"


class DemoSiteAdapter(Adapter):
    key: ClassVar[str] = "demosite"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = (base_url or os.environ.get("DEMOSITE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport

    async def fetch(self, tracking_number: str, ctx: JobContext) -> httpx.Response:
        client = ctx.http(transport=self._transport)
        response = await client.get(f"{self.base_url}/track", params={"number": tracking_number})
        check_status(response)
        return response

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        response = await self.fetch(request.tracking_number, ctx)
        html = response.text
        events = parse_tracking_page(html)
        # The page is kept exactly as received, for debugging a parser that stops working.
        return TrackingResult(RawResponse(content_type="text/html", body=html), events)
