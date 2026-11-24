"""Courier Adapter SDK: the worker runtime and message contracts for courier adapters."""

from .base import Adapter, TrackingResult, TrackRequest
from .context import JobContext
from .contracts import Location, RawResponse, TrackingEvent
from .errors import (
    AdapterError,
    AuthExpired,
    AuthInvalid,
    Blocked,
    NetworkConnectError,
    NetworkTimeout,
    ParseError,
    ProviderServerError,
    RateLimited,
    SchemaDrift,
    ShipmentNotFound,
)
from .runner import run

__all__ = [
    "Adapter",
    "AdapterError",
    "AuthExpired",
    "AuthInvalid",
    "Blocked",
    "JobContext",
    "Location",
    "NetworkConnectError",
    "NetworkTimeout",
    "ParseError",
    "ProviderServerError",
    "RateLimited",
    "RawResponse",
    "SchemaDrift",
    "ShipmentNotFound",
    "TrackRequest",
    "TrackingEvent",
    "TrackingResult",
    "run",
]
