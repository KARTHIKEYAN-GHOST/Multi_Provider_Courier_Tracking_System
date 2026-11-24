"""Credential resolution in the SDK: how a job obtains its secret, and what can go wrong."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import httpx
import pytest

from courier_adapter_sdk import Adapter, JobContext, RawResponse, TrackingResult, TrackRequest
from courier_adapter_sdk.contracts import new_id
from courier_adapter_sdk.credentials import (
    CredentialDenied,
    CredentialServiceUnavailable,
    HttpCredentialResolver,
    Secret,
)
from courier_adapter_sdk.logging import configure_logging, get_logger
from courier_adapter_sdk.worker import Worker

from .conftest import FakeDelivery, FakePublisher, job_message, make_settings, schema_errors

TOKEN = "cwt_aaaaaaaaaaaa_" + "b" * 64
API_KEY = "the-real-provider-api-key"


def engine(handler: Any) -> HttpCredentialResolver:
    return HttpCredentialResolver(
        "http://engine.internal:8081/", TOKEN, transport=httpx.MockTransport(handler)
    )


async def test_the_resolver_sends_the_reference_job_and_token_and_returns_the_secret() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"secret": {"api_key": API_KEY}})

    secret = await engine(handler).resolve("ref-1", "job-1", 2)
    assert secret["api_key"] == API_KEY
    (request,) = seen
    assert str(request.url) == "http://engine.internal:8081/internal/v1/credentials/resolve"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {"credential_ref": "ref-1", "job_id": "job-1", "attempt": 2}


@pytest.mark.parametrize(
    ("status", "error", "retryable"),
    [
        (401, CredentialDenied, False),
        (403, CredentialDenied, False),
        (422, CredentialDenied, False),
        (500, CredentialServiceUnavailable, True),
        (503, CredentialServiceUnavailable, True),
    ],
)
async def test_refusals_are_permanent_and_outages_are_retryable(
    status: int, error: type[Exception], retryable: bool
) -> None:
    resolver = engine(lambda request: httpx.Response(status, json={"error": {"code": "resolve_denied"}}))
    with pytest.raises(error) as raised:
        await resolver.resolve("r", "j", 1)
    assert raised.value.retryable is retryable  # type: ignore[attr-defined]
    assert TOKEN not in str(raised.value)


async def test_an_unreachable_engine_is_retryable_and_the_error_names_neither_token_nor_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(CredentialServiceUnavailable) as raised:
        await engine(handler).resolve("r", "j", 1)
    assert "ConnectError" in str(raised.value)
    assert TOKEN not in str(raised.value)
    assert "engine.internal" not in str(raised.value)


async def test_a_malformed_answer_is_not_mistaken_for_a_secret() -> None:
    with pytest.raises(CredentialServiceUnavailable):
        await engine(lambda request: httpx.Response(200, json={"nothing": 1})).resolve("r", "j", 1)


def test_a_secret_never_shows_its_values() -> None:
    secret = Secret({"api_key": API_KEY, "account": "12345"})
    for rendered in (
        repr(secret),
        str(secret),
        f"{secret}",
        f"{secret!r}",
        repr({"s": secret}),
        repr([secret]),
    ):
        assert API_KEY not in rendered
        assert "12345" not in rendered
    assert repr(secret) == "Secret(fields=['account', 'api_key'])"
    assert dict(secret) == {"api_key": API_KEY, "account": "12345"}, "values are available when asked for"
    with pytest.raises(CredentialDenied, match="no field 'password'"):
        secret["password"]


def ctx(credential_ref: str | None, resolver: Any) -> JobContext:
    return JobContext(new_id(), 1, new_id(), 0, new_id(), get_logger(), credential_ref, resolver)


async def test_a_job_without_a_reference_or_a_worker_without_a_resolver_cannot_get_a_secret() -> None:
    with pytest.raises(CredentialDenied, match="no credential reference"):
        await ctx(None, None).credentials()
    with pytest.raises(CredentialDenied, match="not configured"):
        await ctx(new_id(), None).credentials()


# ----------------------------------------------------------------------------- through the worker


class RecordingResolver:
    def __init__(self, outcome: Secret | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, int]] = []

    async def resolve(self, credential_ref: str, job_id: str, attempt: int) -> Secret:
        self.calls.append((credential_ref, job_id, attempt))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class NeedsKey(Adapter):
    key: ClassVar[str] = "dummy"
    version: ClassVar[str] = "0.1.0"

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        secret = await ctx.credentials()
        ctx.log.info("calling_provider", credentials=secret)  # a careless adapter logging the secret object
        return TrackingResult(
            RawResponse(content_type="text/plain", body=f"used {len(secret['api_key'])} chars"), []
        )


async def run_job(
    resolver: Any, capfd: pytest.CaptureFixture[str], credential_ref: str | None
) -> tuple[Any, str, Any]:
    configure_logging("json")  # after the capture fixture is active, so the log lands in it
    publisher = FakePublisher()
    worker = Worker(NeedsKey(), make_settings(), publisher, resolver=resolver)
    message = job_message("X", attempt=3)
    if credential_ref:
        message["payload"]["credential_ref"] = credential_ref
    delivery = FakeDelivery(message)
    await worker.submit(delivery)
    await publisher.wait_for_results(1)
    return publisher, capfd.readouterr().err, message


async def test_the_worker_resolves_with_the_jobs_own_identity_and_the_secret_leaves_no_trace(
    capfd: pytest.CaptureFixture[str],
) -> None:
    reference = new_id()
    resolver = RecordingResolver(Secret({"api_key": API_KEY}))
    publisher, logs, message = await run_job(resolver, capfd, reference)

    assert resolver.calls == [(reference, message["payload"]["job_id"], 3)]
    result = publisher.results[0]
    assert result["payload"]["outcome"] == "SUCCESS"
    assert schema_errors(result) == []
    assert API_KEY not in json.dumps(publisher.sent), "not in any message"
    assert API_KEY not in logs, "not in the log, even when the adapter logs the secret object"
    assert "Secret(fields=['api_key'])" in logs


@pytest.mark.parametrize(
    ("failure", "error_class", "retryable"),
    [
        (
            CredentialDenied("the Engine refused the credential: not permitted for this job"),
            "AUTH_INVALID",
            False,
        ),
        (CredentialServiceUnavailable("the Engine's internal API is unreachable"), "INTERNAL", True),
    ],
)
async def test_a_failed_resolve_becomes_a_classified_job_failure(
    capfd: pytest.CaptureFixture[str], failure: Exception, error_class: str, retryable: bool
) -> None:
    publisher, _, _ = await run_job(RecordingResolver(failure), capfd, new_id())
    error = publisher.results[0]["payload"]["error"]
    assert (error["class"], error["retryable"]) == (error_class, retryable)


async def test_a_job_whose_provider_has_no_credential_fails_without_asking_the_engine(
    capfd: pytest.CaptureFixture[str],
) -> None:
    resolver = RecordingResolver(Secret({"api_key": API_KEY}))
    publisher, _, _ = await run_job(resolver, capfd, None)
    assert resolver.calls == []
    assert publisher.results[0]["payload"]["error"]["class"] == "AUTH_INVALID"
