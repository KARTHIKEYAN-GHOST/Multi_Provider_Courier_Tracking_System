"""How an adapter says "a person is needed": a one-time code or a CAPTCHA answer.

An adapter raises ``NeedsHuman`` from ``track`` (or from ``resume``, when the answer was not
accepted and the provider asks again). It hands over the *session* — the plain data it needs
to continue: cookies, a transaction ID. The SDK stores it, tells the Engine, and the worker
moves on to other jobs. When the user has answered, ``Adapter.resume`` is called with the
session and the answer, possibly in another worker process.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal

from .contracts import IMAGE_TYPES, MAX_IMAGE_BYTES, ChallengeImage, ResponseFormat


@dataclass(frozen=True)
class Challenge:
    kind: Literal["otp", "captcha"]
    prompt: str
    session: dict[str, Any]
    response_format: ResponseFormat | None = None
    image: ChallengeImage | None = None
    expires_in_s: int | None = None


class NeedsHuman(Exception):
    """Raised by an adapter to pause the lookup until a person has answered."""

    def __init__(self, challenge: Challenge) -> None:
        super().__init__(f"{challenge.kind} required")
        self.challenge = challenge


def _format(digits: int | None, min_length: int, max_length: int, charset: str) -> ResponseFormat:
    if digits is not None:
        return ResponseFormat(min_length=digits, max_length=digits, charset="digits")
    return ResponseFormat.model_validate(
        {"min_length": min_length, "max_length": max_length, "charset": charset}
    )


def otp_required(
    prompt: str,
    session: dict[str, Any],
    *,
    digits: int | None = None,
    min_length: int = 1,
    max_length: int = 64,
    charset: Literal["digits", "alphanumeric", "any"] = "any",
    expires_in_s: int | None = None,
) -> NeedsHuman:
    """The provider sent the user a one-time code. ``prompt`` is the adapter's own wording —
    never text copied from the provider's page."""
    return NeedsHuman(
        Challenge(
            "otp", prompt, session, _format(digits, min_length, max_length, charset), None, expires_in_s
        )
    )


def captcha_required(
    prompt: str,
    session: dict[str, Any],
    *,
    image: bytes,
    content_type: str,
    min_length: int = 1,
    max_length: int = 64,
    charset: Literal["digits", "alphanumeric", "any"] = "any",
    expires_in_s: int | None = None,
) -> NeedsHuman:
    """The provider shows a CAPTCHA. The picture is passed to the user exactly as received;
    it is never solved automatically."""
    if content_type not in IMAGE_TYPES:
        raise ValueError("the CAPTCHA picture is not a PNG, JPEG, GIF or WebP image")
    if not 0 < len(image) <= MAX_IMAGE_BYTES:
        raise ValueError(f"the CAPTCHA picture must be 1 to {MAX_IMAGE_BYTES} bytes")
    picture = ChallengeImage.model_validate(
        {"content_type": content_type, "data": base64.b64encode(image).decode()}
    )
    return NeedsHuman(
        Challenge(
            "captcha", prompt, session, _format(None, min_length, max_length, charset), picture, expires_in_s
        )
    )
