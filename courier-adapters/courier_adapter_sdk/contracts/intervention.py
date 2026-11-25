"""``job.intervention_required`` (worker → Engine) and ``job.resume`` (Engine → worker)."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .envelope import Contract, Envelope, UtcTimestamp, UuidStr
from .job import ExecutionIdentity, JobTrack

JOB_INTERVENTION_REQUIRED = "job.intervention_required"
JOB_RESUME = "job.resume"

#: Largest CAPTCHA picture a worker may send (the message limit is 256 KiB, base64 included).
MAX_IMAGE_BYTES = 100_000
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


class ResponseFormat(Contract):
    """What a valid answer looks like, so the Engine can refuse an obviously wrong one."""

    min_length: Annotated[int, Field(ge=1, le=64, strict=True)]
    max_length: Annotated[int, Field(ge=1, le=64, strict=True)]
    charset: Literal["digits", "alphanumeric", "any"]

    @model_validator(mode="after")
    def _ordered(self) -> ResponseFormat:
        if self.min_length > self.max_length:
            raise ValueError("min_length is greater than max_length")
        return self


class ChallengeImage(Contract):
    content_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    #: Base64, standard alphabet.
    data: Annotated[
        str, StringConstraints(min_length=4, max_length=140_000, pattern=r"^[A-Za-z0-9+/]+={0,2}$")
    ]


class InterventionRequired(ExecutionIdentity):
    """An execution stopped because the provider wants something only a person can supply."""

    finished_at: UtcTimestamp
    kind: Literal["otp", "captcha"]
    #: Shown to the user. Written by the adapter, not copied from the provider's page.
    prompt: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    #: Where the worker saved its session. Opaque; echoed back in ``job.resume``.
    session_ref: UuidStr
    expires_in_s: Annotated[int, Field(ge=30, le=3600, strict=True)] | None = None
    response_format: ResponseFormat | None = None
    image: ChallengeImage | None = None


class InterventionRequiredEnvelope(Envelope[InterventionRequired]):
    type: Literal["job.intervention_required"]

    def to_bytes(self) -> bytes:
        # The optional parts of a challenge are left out when unused; the schema has no nulls.
        message = self.model_dump(mode="json")
        message["payload"] = {k: v for k, v in message["payload"].items() if v is not None}
        return json.dumps(message, separators=(",", ":")).encode()


class JobResume(JobTrack):
    """Continue a lookup with a human answer. The answer is fetched from the Engine with
    ``intervention_id``; it is never part of a message."""

    intervention_id: UuidStr
    session_ref: UuidStr


class JobResumeEnvelope(Envelope[JobResume]):
    type: Literal["job.resume"]
