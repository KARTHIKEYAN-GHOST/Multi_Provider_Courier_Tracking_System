"""Courier Adapter SDK: the worker runtime and message contracts for courier adapters."""

from .base import Adapter, TrackingResult, TrackRequest
from .context import JobContext
from .contracts import Location, RawResponse, TrackingEvent
from .credentials import CredentialDenied, Secret
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
    SavedSessionLost,
    SchemaDrift,
    ShipmentNotFound,
)
from .interventions import NeedsHuman, captcha_required, otp_required
from .runner import run

__all__ = [
    "Adapter",
    "AdapterError",
    "AuthExpired",
    "AuthInvalid",
    "Blocked",
    "CredentialDenied",
    "JobContext",
    "Location",
    "NeedsHuman",
    "NetworkConnectError",
    "NetworkTimeout",
    "ParseError",
    "ProviderServerError",
    "RateLimited",
    "RawResponse",
    "SavedSessionLost",
    "SchemaDrift",
    "Secret",
    "ShipmentNotFound",
    "TrackRequest",
    "TrackingEvent",
    "TrackingResult",
    "captcha_required",
    "otp_required",
    "run",
]
