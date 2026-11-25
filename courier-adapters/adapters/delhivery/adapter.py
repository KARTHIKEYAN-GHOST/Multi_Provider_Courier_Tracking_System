"""Delhivery adapter: the official "Order Tracking" (pull) API.

Needs the API token Delhivery issues to an account, stored in the Engine as the credential
of provider ``delhivery`` in the form ``{"token": "..."}``. The token reaches this code only
through ``ctx.credentials()``, for the duration of one job.

The token is sent in the ``Authorization`` header, never in the URL. Delhivery's reference
page for this call also describes a ``token`` query parameter; the header is used because
URLs end up in the logs of servers and proxies.
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

from .parser import parse_packages

#: Production. Delhivery's test system is https://staging-express.delhivery.com.
DEFAULT_BASE_URL = "https://track.delhivery.com"


class DelhiveryAdapter(Adapter):
    key: ClassVar[str] = "delhivery"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = (base_url or os.environ.get("DELHIVERY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport

    async def fetch(self, waybill: str, token: str, ctx: JobContext) -> httpx.Response:
        client = ctx.http(transport=self._transport)
        return await client.get(
            f"{self.base_url}/api/v1/packages/json/",
            params={"waybill": waybill},
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
        )

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        secret = await ctx.credentials()
        response = await self.fetch(request.tracking_number, secret["token"], ctx)
        if response.status_code == 404:
            raise ShipmentNotFound
        check_status(response)
        try:
            data: Any = response.json()
        except ValueError:
            raise ParseError("Delhivery's answer is not JSON") from None
        events = parse_packages(data, request.tracking_number)
        return TrackingResult(RawResponse(content_type="application/json", body=data), events)
