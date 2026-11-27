"""Demo Secure Logistics adapter: the signed-in tracking page of a business courier website.

The courier publishes no API. Its tracking page (``/secure/track?number=...``) is shown only
to a signed-in account; otherwise the site redirects to ``/secure/login``. The sign-in form
posts ``email``, ``password`` and a one-time ``form_token`` (a hidden field of the form) to
``/secure/login`` and answers with a session cookie. The adapter signs in on every lookup.

Credential: the email and password of an account on the website, with the fields
``email`` and ``password``.

This is automated use of a website (tracking method ``scrape``): it needs the courier's
permission, recorded in the Engine with ``admin set-scraping-authorized`` before any job is
dispatched.

PROVISIONAL: the reading of the answer is unconfirmed. It follows one delivered page and one
"not found" page seen while signed in (see ``parser.py``). Times are India Standard Time.

When this was written, signing in asked for no one-time code and showed no CAPTCHA. The
adapter does not guess what such a step would look like: a page it does not recognize is
reported as schema drift. If the site adds one, it must be handed to a person with the SDK's
``otp_required``/``captcha_required`` and continued in ``resume``, as the demosite adapter
does; it is never solved by code.
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
    SchemaDrift,
    ShipmentNotFound,
    TrackingResult,
    TrackRequest,
)
from courier_adapter_sdk.http import check_status

from .parser import form_token, is_login_page, parse_tracking_page

DEFAULT_BASE_URL = "http://127.0.0.1:18090"
_NUMBER = re.compile(r"^[A-Za-z0-9-]{3,40}$")


class DemoSecureAdapter(Adapter):
    key: ClassVar[str] = "demosecure"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = (base_url or os.environ.get("DEMOSECURE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        if not _NUMBER.match(request.tracking_number):
            raise ShipmentNotFound  # not a consignment number; the site is not asked
        secret = await ctx.credentials()
        client = ctx.http(transport=self._transport)

        login_page = await client.get(f"{self.base_url}/secure/login")
        check_status(login_page)
        token = form_token(login_page.text)
        signed_in = await client.post(
            f"{self.base_url}/secure/login",
            data={
                "form_token": token,
                "next": "/secure/track",
                "email": secret["email"],
                "password": secret["password"],
            },
        )
        if not signed_in.is_redirect:
            check_status(signed_in)
            if is_login_page(signed_in.text):
                raise SchemaDrift("the site answered the sign-in with the sign-in form again")

        response = await client.get(
            f"{self.base_url}/secure/track", params={"number": request.tracking_number}
        )
        if response.is_redirect:
            raise SchemaDrift("the site sent the tracking request back to sign in")
        check_status(response)
        ctx.keep_evidence(response)
        html = response.text
        if is_login_page(html):
            raise SchemaDrift("the site answered the tracking request with the sign-in form")
        events = parse_tracking_page(html)
        # The page is kept exactly as received, for debugging a parser that stops working.
        return TrackingResult(RawResponse(content_type="text/html", body=html), events)
