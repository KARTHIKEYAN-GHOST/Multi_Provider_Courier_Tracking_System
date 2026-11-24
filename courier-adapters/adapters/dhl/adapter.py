"""DHL adapter: the official "Shipment Tracking - Unified" API.

Needs an API key, stored in the Engine as the credential of provider ``dhl`` in the form
``{"api_key": "..."}``. The key reaches this code only through ``ctx.credentials()``, for
the duration of one job.
"""

from __future__ import annotations

import os
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

from .parser import parse_shipments

DEFAULT_BASE_URL = "https://api-eu.dhl.com"


class DhlAdapter(Adapter):
    key: ClassVar[str] = "dhl"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = (base_url or os.environ.get("DHL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport

    async def fetch(self, tracking_number: str, api_key: str, ctx: JobContext) -> httpx.Response:
        client = ctx.http(transport=self._transport)
        return await client.get(
            f"{self.base_url}/track/shipments",
            params={"trackingNumber": tracking_number},
            headers={"DHL-API-Key": api_key, "Accept": "application/json"},
        )

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        secret = await ctx.credentials()
        response = await self.fetch(request.tracking_number, secret["api_key"], ctx)
        if response.status_code == 404:
            raise ShipmentNotFound
        check_status(response)
        try:
            data: Any = response.json()
        except ValueError:
            raise ParseError("DHL's answer is not JSON") from None
        events = parse_shipments(data, request.tracking_number)
        return TrackingResult(RawResponse(content_type="application/json", body=data), events)
