"""Errors an adapter raises to tell the Engine what went wrong.

The class decides how the Engine reacts: retryable failures are retried with back-off up to
the provider's attempt limit, the rest fail the job at once. An adapter should raise the most
specific class it can; anything else it raises is reported as ``INTERNAL``.
"""

from __future__ import annotations

from .contracts import ErrorClass, ErrorInfo

_MAX_MESSAGE = 2000


class AdapterError(Exception):
    """Base class. ``error_class`` and ``retryable`` are fixed per subclass."""

    error_class: ErrorClass = ErrorClass.UNKNOWN
    retryable: bool = False

    def __init__(self, message: str = "", *, retry_after_ms: int | None = None) -> None:
        super().__init__(message)
        self.message = message or self.__class__.__name__
        self.retry_after_ms = retry_after_ms

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            error_class=self.error_class,
            message=self.message[:_MAX_MESSAGE],
            retryable=self.retryable,
            retry_after_ms=self.retry_after_ms,
        )


class NetworkTimeout(AdapterError):
    """The provider did not answer in time."""

    error_class = ErrorClass.NETWORK_TIMEOUT
    retryable = True


class NetworkConnectError(AdapterError):
    """The provider could not be reached."""

    error_class = ErrorClass.NETWORK_CONNECT
    retryable = True


class ProviderServerError(AdapterError):
    """The provider answered with a 5xx status."""

    error_class = ErrorClass.HTTP_5XX
    retryable = True


class RateLimited(AdapterError):
    """The provider asked us to slow down. Pass ``retry_after_ms`` when it said for how long."""

    error_class = ErrorClass.RATE_LIMITED
    retryable = True


class Blocked(AdapterError):
    """The provider refused us (bot detection, IP ban). Retrying at once will not help."""

    error_class = ErrorClass.BLOCKED
    retryable = False


class AuthInvalid(AdapterError):
    error_class = ErrorClass.AUTH_INVALID
    retryable = False


class AuthExpired(AdapterError):
    error_class = ErrorClass.AUTH_EXPIRED
    retryable = False


class ParseError(AdapterError):
    """The response could not be understood."""

    error_class = ErrorClass.PARSE_ERROR
    retryable = False


class SchemaDrift(AdapterError):
    """The response no longer has the structure the adapter was written for."""

    error_class = ErrorClass.SCHEMA_DRIFT
    retryable = False


class ShipmentNotFound(Exception):
    """The provider does not know the tracking number. This is an answer, not a failure."""


def describe(error: BaseException) -> ErrorInfo:
    """Turns whatever an adapter raised into the error section of a ``job.result``."""
    if isinstance(error, AdapterError):
        return error.to_info()
    if isinstance(error, TimeoutError):
        return ErrorInfo(
            error_class=ErrorClass.ADAPTER_TIMEOUT,
            message="the adapter did not finish within its execution budget",
            retryable=True,
        )
    # An unexpected exception is a bug in the adapter. The message is sent without the
    # exception text, which could contain provider content; the full error goes to the log.
    return ErrorInfo(
        error_class=ErrorClass.INTERNAL,
        message=f"unexpected {type(error).__name__} in adapter",
        retryable=True,
    )
