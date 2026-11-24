"""Message envelope and the field types shared by every message.

These models mirror the JSON Schemas in ``courier-contracts/schemas/v1``. The schemas are the
source of truth; ``tests/test_contracts.py`` runs the shared fixtures through these models
and validates everything the SDK produces against the schemas.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, StringConstraints

SCHEMA_VERSION = "1.0"

_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$")


def _parse_timestamp(value: Any) -> Any:
    """Accept only the contract's form: UTC with a ``Z`` suffix (never ``+00:00``)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)
    if not isinstance(value, str) or not _TIMESTAMP.match(value):
        raise ValueError("timestamp must be UTC in the form YYYY-MM-DDTHH:MM:SS[.fff]Z")
    # Python accepts at most microseconds; extra digits are truncated, not rounded.
    head, _, fraction = value[:-1].partition(".")
    return datetime.fromisoformat(f"{head}.{fraction[:6] or '0'}+00:00")


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


UtcTimestamp = Annotated[
    datetime,
    BeforeValidator(_parse_timestamp),
    PlainSerializer(_format_timestamp, return_type=str),
]
# Lowercase only, as the schema requires. Kept as text so it round-trips unchanged.
UuidStr = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]
Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,49}$")]
Version = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$")]
TrackingNumber = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")]
Attempt = Annotated[int, Field(ge=1, strict=True)]


def new_id() -> str:
    """A time-ordered UUID (version 7, RFC 9562), lowercase.

    Built by hand because ``uuid.uuid7`` only exists from Python 3.14: 48 bits of Unix time in
    milliseconds followed by random bits from the operating system.
    """
    value = (time.time_ns() // 1_000_000) << 80 | int.from_bytes(os.urandom(10))
    value = (value & ~(0xF << 76)) | (0x7 << 76)  # version 7
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # RFC variant
    return str(uuid.UUID(int=value))


def utc_now() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    """Base for all contract models: unknown fields are ignored (minor-version tolerance)."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class Envelope[P: BaseModel](Contract):
    """Wrapper around every message."""

    message_id: UuidStr
    type: str
    # Only major version 1 is understood; an unknown major version is rejected.
    schema_version: Annotated[str, StringConstraints(pattern=r"^1\.(0|[1-9][0-9]*)$")]
    occurred_at: UtcTimestamp
    correlation_id: UuidStr
    causation_id: UuidStr | None = None
    producer: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    payload: P

    def to_bytes(self) -> bytes:
        return self.model_dump_json(exclude_none=False).encode()
