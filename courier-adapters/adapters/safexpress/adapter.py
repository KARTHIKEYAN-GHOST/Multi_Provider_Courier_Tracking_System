"""Safexpress adapter: the waybill tracking API Safexpress gives its account customers.

Safexpress's public website cannot be used by a program: it asks every visitor for a phone
OTP and a CAPTCHA before it shows a waybill. Customers with an account get API access
instead: a client id and secret (OAuth2 client credentials) and an API key. They are
stored in the Engine as the credential of provider ``safexpress`` in the form
``{"client_id": "...", "client_secret": "...", "api_key": "..."}`` and reach this code only
through ``ctx.credentials()``, for the duration of one job.

NOT VERIFIED AGAINST SAFEXPRESS. Safexpress publishes no API description; it sends one
with the credentials. What is certain (it is what Safexpress's own website does) is the
token address, the request body ``{"docNo", "docType"}`` and the outline of the answer.
The tracking address, the scope and the form of the ``Authorization`` header below are
the commonly used ones and must be compared with Safexpress's document before first use.
All three can be changed without touching the code (``SAFEXPRESS_*`` variables).
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import httpx

from courier_adapter_sdk import (
    Adapter,
    AuthInvalid,
    JobContext,
    ParseError,
    RawResponse,
    ShipmentNotFound,
    TrackingResult,
    TrackRequest,
)
from courier_adapter_sdk.http import check_status

from .parser import parse_tracking

DEFAULT_TOKEN_URL = "https://api-auth.safexpress.com/oauth2/token"
DEFAULT_TRACKING_URL = (
    "https://apigateway.safexpress.com/wbtrack/SafexWaybillTracking/webresources/safex_customer/tracking"
)
DEFAULT_SCOPE = "server/waybillapps"


class SafexpressAdapter(Adapter):
    key: ClassVar[str] = "safexpress"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self,
        token_url: str | None = None,
        tracking_url: str | None = None,
        scope: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token_url = token_url or os.environ.get("SAFEXPRESS_TOKEN_URL") or DEFAULT_TOKEN_URL
        self.tracking_url = tracking_url or os.environ.get("SAFEXPRESS_TRACKING_URL") or DEFAULT_TRACKING_URL
        self.scope = scope or os.environ.get("SAFEXPRESS_SCOPE") or DEFAULT_SCOPE
        self._transport = transport

    async def _access_token(self, client: httpx.AsyncClient, client_id: str, client_secret: str) -> str:
        """A short-lived access token. It is used for this one job and kept nowhere."""
        response = await client.post(
            self.token_url,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials", "scope": self.scope},
        )
        if response.status_code == 400:
            # OAuth2 answers 400 (invalid_client, invalid_scope) to credentials it refuses.
            raise AuthInvalid("Safexpress refused the client credentials (HTTP 400)")
        check_status(response)
        try:
            token = response.json().get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            raise ParseError("Safexpress's token answer has no access token")
        return token

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        secret = await ctx.credentials()
        client = ctx.http(transport=self._transport)
        token = await self._access_token(client, secret["client_id"], secret["client_secret"])
        response = await client.post(
            self.tracking_url,
            json={"docNo": request.tracking_number, "docType": "WB"},
            headers={"Authorization": token, "x-api-key": secret["api_key"], "Accept": "application/json"},
        )
        if response.status_code == 404:
            raise ShipmentNotFound
        check_status(response)
        try:
            data: Any = response.json()
        except ValueError:
            raise ParseError("Safexpress's answer is not JSON") from None
        events = parse_tracking(data)
        return TrackingResult(RawResponse(content_type="application/json", body=data), events)
