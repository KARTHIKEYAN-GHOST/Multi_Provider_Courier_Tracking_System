"""Who is signed in. Kept in memory only: a restart signs everyone out.

The browser holds a random session ID, never the API key. The key stays in this process and
is sent only to the Engine.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

MAX_SIGN_INS = 1000


@dataclass(frozen=True)
class SignIn:
    api_key: str
    #: Must come back with every form the user submits (cross-site request forgery guard).
    csrf: str
    expires: float


class SignIns:
    def __init__(self, ttl_s: int) -> None:
        self._ttl_s = ttl_s
        self._by_id: dict[str, SignIn] = {}

    def add(self, api_key: str) -> str:
        now = time.monotonic()
        self._by_id = {sid: s for sid, s in self._by_id.items() if s.expires > now}
        if len(self._by_id) >= MAX_SIGN_INS:
            del self._by_id[min(self._by_id, key=lambda sid: self._by_id[sid].expires)]
        session_id = secrets.token_urlsafe(32)
        self._by_id[session_id] = SignIn(api_key, secrets.token_urlsafe(32), now + self._ttl_s)
        return session_id

    def get(self, session_id: str | None) -> SignIn | None:
        found = self._by_id.get(session_id or "")
        if found is None or found.expires <= time.monotonic():
            self._by_id.pop(session_id or "", None)
            return None
        return found

    def remove(self, session_id: str | None) -> None:
        self._by_id.pop(session_id or "", None)
