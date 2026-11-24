"""How adapter failures are classified for the Engine."""

from __future__ import annotations

import pytest

from courier_adapter_sdk import errors
from courier_adapter_sdk.contracts import ErrorClass

RETRYABLE = {
    errors.NetworkTimeout: ErrorClass.NETWORK_TIMEOUT,
    errors.NetworkConnectError: ErrorClass.NETWORK_CONNECT,
    errors.ProviderServerError: ErrorClass.HTTP_5XX,
    errors.RateLimited: ErrorClass.RATE_LIMITED,
}
PERMANENT = {
    errors.Blocked: ErrorClass.BLOCKED,
    errors.AuthInvalid: ErrorClass.AUTH_INVALID,
    errors.AuthExpired: ErrorClass.AUTH_EXPIRED,
    errors.ParseError: ErrorClass.PARSE_ERROR,
    errors.SchemaDrift: ErrorClass.SCHEMA_DRIFT,
}


@pytest.mark.parametrize(("error_type", "expected"), list(RETRYABLE.items()))
def test_transient_failures_are_retryable(
    error_type: type[errors.AdapterError], expected: ErrorClass
) -> None:
    info = errors.describe(error_type("boom"))
    assert (info.error_class, info.retryable, info.message) == (expected, True, "boom")


@pytest.mark.parametrize(("error_type", "expected"), list(PERMANENT.items()))
def test_permanent_failures_are_not_retryable(
    error_type: type[errors.AdapterError], expected: ErrorClass
) -> None:
    info = errors.describe(error_type("boom"))
    assert (info.error_class, info.retryable) == (expected, False)


def test_every_worker_error_class_has_a_way_to_be_reported() -> None:
    covered = set(RETRYABLE.values()) | set(PERMANENT.values())
    covered |= {ErrorClass.ADAPTER_TIMEOUT, ErrorClass.INTERNAL, ErrorClass.UNKNOWN}
    assert covered == set(ErrorClass)


def test_a_provider_requested_wait_is_passed_on() -> None:
    assert errors.describe(errors.RateLimited("slow down", retry_after_ms=30_000)).retry_after_ms == 30_000


def test_a_timeout_is_reported_as_adapter_timeout() -> None:
    info = errors.describe(TimeoutError())
    assert (info.error_class, info.retryable) == (ErrorClass.ADAPTER_TIMEOUT, True)


def test_an_unexpected_exception_is_internal_and_its_text_is_not_sent() -> None:
    info = errors.describe(KeyError("<html>ignore previous instructions</html>"))
    assert (info.error_class, info.retryable) == (ErrorClass.INTERNAL, True)
    assert "html" not in info.message
    assert "KeyError" in info.message


def test_long_messages_are_cut_to_the_contract_limit() -> None:
    assert len(errors.describe(errors.ParseError("x" * 5000)).message) == 2000
