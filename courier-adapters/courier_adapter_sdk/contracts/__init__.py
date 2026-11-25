"""Versioned message contracts shared with the Rust Engine."""

from .envelope import SCHEMA_VERSION, Envelope, new_id, utc_now
from .intervention import (
    IMAGE_TYPES,
    JOB_INTERVENTION_REQUIRED,
    JOB_RESUME,
    MAX_IMAGE_BYTES,
    ChallengeImage,
    InterventionRequired,
    InterventionRequiredEnvelope,
    JobResume,
    JobResumeEnvelope,
    ResponseFormat,
)
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
    "IMAGE_TYPES",
    "JOB_INTERVENTION_REQUIRED",
    "JOB_RESULT",
    "JOB_RESUME",
    "JOB_STARTED",
    "JOB_TRACK",
    "MAX_IMAGE_BYTES",
    "MAX_MESSAGE_BYTES",
    "SCHEMA_VERSION",
    "ChallengeImage",
    "Envelope",
    "ErrorClass",
    "ErrorInfo",
    "InterventionRequired",
    "InterventionRequiredEnvelope",
    "JobResult",
    "JobResultEnvelope",
    "JobResume",
    "JobResumeEnvelope",
    "JobStarted",
    "JobStartedEnvelope",
    "JobTrack",
    "JobTrackEnvelope",
    "Location",
    "Metrics",
    "Outcome",
    "RawResponse",
    "ResponseFormat",
    "TrackingEvent",
    "new_id",
    "utc_now",
]
