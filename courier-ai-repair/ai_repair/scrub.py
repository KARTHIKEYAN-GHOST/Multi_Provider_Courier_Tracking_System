"""Removes what identifies a person from a provider answer before it leaves the system.

A stored answer goes to the AI model (an outside service) and, as a test fixture, into the
Git repository. This removes what can be recognized mechanically: the tracking number,
e-mail addresses and phone numbers. It cannot recognize names and street addresses; see
docs/phase-9.md.
"""

from __future__ import annotations

import json
import re
from typing import Any

TRACKING_PLACEHOLDER = "TRACKING0001"
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
# Indian mobile numbers, with or without the country code.
_PHONE = re.compile(r"(?<![\dA-Za-z])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?![\dA-Za-z])")


def scrub_text(text: str, tracking_number: str | None = None) -> str:
    if tracking_number and len(tracking_number) >= 4:
        text = re.sub(re.escape(tracking_number), TRACKING_PLACEHOLDER, text, flags=re.IGNORECASE)
    text = _EMAIL.sub("person@example.com", text)
    return _PHONE.sub("9000000000", text)


def scrub(body: Any, tracking_number: str | None = None) -> Any:
    """The same answer without the tracking number, e-mail addresses and phone numbers."""
    if isinstance(body, str):
        return scrub_text(body, tracking_number)
    # Replacements contain nothing that needs escaping, so the result is still JSON.
    return json.loads(scrub_text(json.dumps(body, ensure_ascii=False), tracking_number))
