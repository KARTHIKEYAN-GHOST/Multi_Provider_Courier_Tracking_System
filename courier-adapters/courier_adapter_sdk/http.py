"""HTTP helpers for adapters that call a courier's API or website."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .contracts import utc_now
from .errors import AuthInvalid, Blocked, ProviderServerError, RateLimited

USER_AGENT = "courier-tracking-adapter/0.1"
#: Connect quickly or fail; allow the provider a while to answer.
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
#: A page or API answer larger than this is not a tracking result.
MAX_RESPONSE_BYTES = 2_000_000


def new_client(**options: Any) -> httpx.AsyncClient:
    """An HTTP client with the SDK's defaults. Prefer ``ctx.http()``, which also closes it."""
    headers = {"User-Agent": USER_AGENT, **options.pop("headers", {})}
    options.setdefault("timeout", DEFAULT_TIMEOUT)
    # Redirects are not followed: a tracking endpoint that redirects (usually to a login or
    # block page) is something the adapter should see and classify.
    options.setdefault("follow_redirects", False)
    return httpx.AsyncClient(headers=headers, **options)


def retry_after_ms(response: httpx.Response) -> int | None:
    """The provider's ``Retry-After`` header in milliseconds (seconds or an HTTP date)."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    if value.isdigit():
        return min(int(value), 86_400) * 1000
    try:
        wait = (parsedate_to_datetime(value) - utc_now()).total_seconds()
    except (TypeError, ValueError):
        return None
    return int(max(0.0, min(wait, 86_400.0)) * 1000)


def check_status(response: httpx.Response) -> None:
    """Raises the right adapter error for a response the provider did not answer normally.

    404 is deliberately not handled here: whether it means "shipment unknown" depends on
    the provider, so the adapter decides.
    """
    status = response.status_code
    if status == 429:
        raise RateLimited("the provider asked us to slow down", retry_after_ms=retry_after_ms(response))
    if status in (401, 407):
        raise AuthInvalid(f"the provider rejected the credential (HTTP {status})")
    if status == 403:
        raise Blocked("the provider refused the request (HTTP 403)")
    if status >= 500:
        raise ProviderServerError(f"the provider answered HTTP {status}")
    if 300 <= status < 400:
        raise Blocked(f"the provider redirected the request (HTTP {status})")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ProviderServerError("the provider's answer is too large to be a tracking result")
