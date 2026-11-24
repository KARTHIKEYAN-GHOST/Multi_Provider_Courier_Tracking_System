"""HTTP helpers: how provider responses and network failures are classified."""

from __future__ import annotations

from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest

from courier_adapter_sdk import errors
from courier_adapter_sdk.context import JobContext
from courier_adapter_sdk.contracts import ErrorClass, new_id, utc_now
from courier_adapter_sdk.http import USER_AGENT, check_status, new_client, retry_after_ms
from courier_adapter_sdk.logging import get_logger


def response(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers=headers, request=httpx.Request("GET", "https://courier.example/t"))


@pytest.mark.parametrize(
    ("status", "error", "retryable"),
    [
        (429, errors.RateLimited, True),
        (500, errors.ProviderServerError, True),
        (503, errors.ProviderServerError, True),
        (401, errors.AuthInvalid, False),
        (403, errors.Blocked, False),
        (302, errors.Blocked, False),
    ],
)
def test_abnormal_statuses_become_adapter_errors(
    status: int, error: type[errors.AdapterError], retryable: bool
) -> None:
    with pytest.raises(error) as raised:
        check_status(response(status))
    assert raised.value.retryable is retryable


@pytest.mark.parametrize("status", [200, 201, 204, 404])
def test_normal_statuses_and_404_are_left_to_the_adapter(status: int) -> None:
    check_status(response(status))


def test_the_providers_retry_after_is_passed_on() -> None:
    with pytest.raises(errors.RateLimited) as raised:
        check_status(response(429, **{"Retry-After": "30"}))
    assert raised.value.retry_after_ms == 30_000
    assert retry_after_ms(response(429)) is None
    assert retry_after_ms(response(429, **{"Retry-After": "soon"})) is None
    assert retry_after_ms(response(429, **{"Retry-After": "999999999"})) == 86_400_000, "capped at a day"
    in_a_minute = format_datetime(utc_now() + timedelta(seconds=60), usegmt=True)
    wait = retry_after_ms(response(429, **{"Retry-After": in_a_minute}))
    assert wait is not None
    assert 50_000 < wait <= 60_000


def test_an_oversized_answer_is_refused() -> None:
    big = httpx.Response(200, content=b"x" * 2_000_001, request=httpx.Request("GET", "https://c.example"))
    with pytest.raises(errors.ProviderServerError, match="too large"):
        check_status(big)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout("t"), ErrorClass.NETWORK_TIMEOUT),
        (httpx.ConnectTimeout("t"), ErrorClass.NETWORK_TIMEOUT),
        (httpx.ConnectError("refused"), ErrorClass.NETWORK_CONNECT),
        (httpx.RemoteProtocolError("closed"), ErrorClass.NETWORK_CONNECT),
    ],
)
def test_network_failures_are_classified_without_the_adapter_catching_them(
    error: Exception, expected: ErrorClass
) -> None:
    info = errors.describe(error)
    assert (info.error_class, info.retryable) == (expected, True)


def test_the_error_message_does_not_repeat_the_exception_text() -> None:
    info = errors.describe(httpx.ConnectError("https://api.example/track?apikey=SECRET123 refused"))
    assert "SECRET123" not in info.message
    assert "ConnectError" in info.message


async def test_clients_have_safe_defaults_and_are_closed_with_the_job() -> None:
    ctx = JobContext(new_id(), 1, new_id(), 0, new_id(), get_logger())
    client = ctx.http(
        transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"Location": "/login"}))
    )
    assert client.headers["user-agent"] == USER_AGENT
    assert client.timeout.connect == 5.0
    reply = await client.get("https://courier.example/track")
    assert reply.status_code == 302, "redirects are shown to the adapter, not followed"
    assert not client.is_closed
    await ctx._close(1.0)
    assert client.is_closed

    plain = new_client(headers={"X-Extra": "1"})
    assert (plain.headers["user-agent"], plain.headers["x-extra"]) == (USER_AGENT, "1")
    await plain.aclose()
