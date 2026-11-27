"""Scraping adapter for the demo courier website that ships with this project.

It is the practice target for HTML scraping: fetch one page, parse it. It contacts only the
configured demo site. Scraping a real courier's website needs that courier's permission,
recorded in the Engine (``admin set-scraping-authorized``) before any job is dispatched.

Some shipments on the demo site sit behind a verification step: a code texted to the
consignee, or a CAPTCHA. The adapter never guesses or solves either. It stops, hands the
question to a person through the Engine, and continues with their answer in ``resume``.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import httpx

from courier_adapter_sdk import (
    Adapter,
    JobContext,
    NeedsHuman,
    RawResponse,
    SavedSessionLost,
    SchemaDrift,
    TrackingResult,
    TrackRequest,
    captcha_required,
    otp_required,
)
from courier_adapter_sdk.contracts import IMAGE_TYPES, MAX_IMAGE_BYTES
from courier_adapter_sdk.http import check_status

from .parser import Verification, find_verification, parse_tracking_page

DEFAULT_BASE_URL = "http://127.0.0.1:8090"
#: The site's verification step stays open for about this long.
VERIFICATION_LIFETIME_S = 600


class DemoSiteAdapter(Adapter):
    key: ClassVar[str] = "demosite"
    version: ClassVar[str] = "0.2.0"

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
        ctx.keep_evidence(response)
        return await self._answer(response.text, ctx)

    async def resume(
        self, request: TrackRequest, ctx: JobContext, session: dict[str, Any], response: str
    ) -> TrackingResult:
        token = session.get("token")
        if not isinstance(token, str):
            raise SavedSessionLost("the saved session has no verification token")
        client = ctx.http(transport=self._transport)
        page = await client.post(f"{self.base_url}/verify", data={"token": token, "answer": response})
        if page.status_code == 400:
            # The site no longer knows the verification step (it expired there).
            raise SavedSessionLost("the site's verification step has expired")
        check_status(page)
        ctx.keep_evidence(page)
        return await self._answer(page.text, ctx)

    async def _answer(self, html: str, ctx: JobContext) -> TrackingResult:
        """A tracking page becomes a result; a verification page becomes a question."""
        verification = find_verification(html)
        if verification is not None:
            raise await self._challenge(verification, ctx)
        events = parse_tracking_page(html)
        # The page is kept exactly as received, for debugging a parser that stops working.
        return TrackingResult(RawResponse(content_type="text/html", body=html), events)

    async def _challenge(self, verification: Verification, ctx: JobContext) -> NeedsHuman:
        # The prompts are this adapter's own words; nothing the page says is passed on.
        again = "The previous answer was not accepted. " if verification.retry else ""
        session = {"token": verification.token}
        if verification.kind == "otp":
            return otp_required(
                again + "Demo Courier sent a 6-digit code to the phone registered for this shipment. "
                "Enter that code.",
                session,
                digits=6,
                expires_in_s=VERIFICATION_LIFETIME_S,
            )
        # The picture's address is built from the checked token, not taken from the page.
        client = ctx.http(transport=self._transport)
        picture = await client.get(f"{self.base_url}/captcha/{verification.token}.png")
        check_status(picture)
        content_type = picture.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in IMAGE_TYPES or not 0 < len(picture.content) <= MAX_IMAGE_BYTES:
            raise SchemaDrift("the verification picture is not a usable image")
        return captcha_required(
            again + "Type the 5 digits shown in the picture.",
            session,
            image=picture.content,
            content_type=content_type,
            min_length=5,
            max_length=5,
            charset="digits",
            expires_in_s=VERIFICATION_LIFETIME_S,
        )
