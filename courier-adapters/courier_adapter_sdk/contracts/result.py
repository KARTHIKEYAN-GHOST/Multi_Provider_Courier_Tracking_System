"""``job.result`` (worker → Engine): the outcome of one physical execution."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from .envelope import Contract, Envelope, UtcTimestamp
from .job import ExecutionIdentity

JOB_RESULT = "job.result"


class Outcome(StrEnum):
    SUCCESS = "SUCCESS"
    NOT_FOUND = "NOT_FOUND"
    FAILURE = "FAILURE"


class ErrorClass(StrEnum):
    """Error classes a worker may report.

    ``DELIVERY_LIMIT_EXCEEDED``, ``QUEUE_WAIT_EXCEEDED`` and ``WORKER_LOST`` are deliberately
    absent: only the Engine assigns them.
    """

    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    NETWORK_CONNECT = "NETWORK_CONNECT"
    HTTP_5XX = "HTTP_5XX"
    RATE_LIMITED = "RATE_LIMITED"
    BLOCKED = "BLOCKED"
    AUTH_INVALID = "AUTH_INVALID"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    PARSE_ERROR = "PARSE_ERROR"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    ADAPTER_TIMEOUT = "ADAPTER_TIMEOUT"
    INTERNAL = "INTERNAL"
    UNKNOWN = "UNKNOWN"


class Location(Contract):
    city: Annotated[str, StringConstraints(max_length=200)] | None = None
    state: Annotated[str, StringConstraints(max_length=200)] | None = None
    country: Annotated[str, StringConstraints(max_length=200)] | None = None
    postal_code: Annotated[str, StringConstraints(max_length=50)] | None = None
    raw: Annotated[str, StringConstraints(max_length=500)] | None = None


class TrackingEvent(Contract):
    """One provider event. ``raw_status`` is the provider's own wording; the Engine normalizes it."""

    raw_status: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    raw_description: Annotated[str, StringConstraints(max_length=2000)] | None = None
    occurred_at: UtcTimestamp
    location: Location | None = None


class RawResponse(Contract):
    """The provider response exactly as received, kept by the Engine for debugging."""

    content_type: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    body: Any


class ErrorInfo(Contract):
    # The JSON field is "class", which is a Python keyword.
    error_class: ErrorClass = Field(alias="class")
    message: Annotated[str, StringConstraints(max_length=2000)]
    retryable: bool
    retry_after_ms: Annotated[int, Field(ge=0)] | None = None

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class Metrics(Contract):
    duration_ms: Annotated[int, Field(ge=0)] | None = None


class JobResult(ExecutionIdentity):
    finished_at: UtcTimestamp
    outcome: Outcome
    raw_response: RawResponse | None = None
    events: list[TrackingEvent] | None = Field(default=None, max_length=1000)
    error: ErrorInfo | None = None
    metrics: Metrics | None = None

    @model_validator(mode="after")
    def _outcome_rules(self) -> Self:
        if self.outcome is Outcome.SUCCESS and (self.raw_response is None or self.events is None):
            raise ValueError("SUCCESS requires raw_response and events")
        if self.outcome is not Outcome.FAILURE and self.error is not None:
            raise ValueError("only FAILURE may carry an error")
        if self.outcome is Outcome.FAILURE and self.error is None:
            raise ValueError("FAILURE requires an error")
        return self


class JobResultEnvelope(Envelope[JobResult]):
    type: Literal["job.result"]

    def to_bytes(self) -> bytes:
        # Absent optional sections are omitted (the schema forbids e.g. "error": null on SUCCESS);
        # causation_id is the one field that is sent as an explicit null.
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        data.setdefault("causation_id", None)
        return json.dumps(data, separators=(",", ":")).encode()
