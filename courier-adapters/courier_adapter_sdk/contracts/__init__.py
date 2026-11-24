"""Versioned message contracts shared with the Rust Engine."""

from .envelope import SCHEMA_VERSION, Envelope, new_id, utc_now
from .job import JOB_STARTED, JOB_TRACK, JobStarted, JobStartedEnvelope, JobTrack, JobTrackEnvelope
from .result import (
    JOB_RESULT,
    ErrorClass,
    ErrorInfo,
    JobResult,
    JobResultEnvelope,
    Location,
    Metrics,
    Outcome,
    RawResponse,
    TrackingEvent,
)

# Contract limit on the size of one message (limits.max_message_bytes in topology.yaml).
MAX_MESSAGE_BYTES = 262_144

__all__ = [
    "JOB_RESULT",
    "JOB_STARTED",
    "JOB_TRACK",
    "MAX_MESSAGE_BYTES",
    "SCHEMA_VERSION",
    "Envelope",
    "ErrorClass",
    "ErrorInfo",
    "JobResult",
    "JobResultEnvelope",
    "JobStarted",
    "JobStartedEnvelope",
    "JobTrack",
    "JobTrackEnvelope",
    "Location",
    "Metrics",
    "Outcome",
    "RawResponse",
    "TrackingEvent",
    "new_id",
    "utc_now",
]
