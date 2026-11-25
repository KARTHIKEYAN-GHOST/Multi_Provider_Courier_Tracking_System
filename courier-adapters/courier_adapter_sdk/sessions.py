"""Sessions: what a worker keeps while a lookup waits for a human answer.

When a provider asks for a one-time code or a CAPTCHA, the adapter hands the SDK whatever it
needs to continue later (cookies, a transaction ID). The worker does not wait: it stores that
session here, tells the Engine, and takes the next job. When the answer arrives, any worker of
the same adapter loads the session and continues.

Sessions are *portable* (plain data, no live browser page). They are held in Redis, which only
adapter workers use — the Engine never sees a session. Because a session can contain cookies
or tokens, it is encrypted before it leaves the process (AES-256-GCM, key
``WORKER_SESSION_KEY``) and bound to its reference, and it expires by itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .contracts import ErrorClass
from .errors import AdapterError

#: A session is small state, not a page dump.
MAX_SESSION_BYTES = 64 * 1024
_NONCE_BYTES = 12


class SessionStoreUnavailable(AdapterError):
    """The session store cannot be reached. Worth retrying."""

    error_class = ErrorClass.SESSION_LOST
    retryable = True


class SessionStore(Protocol):
    async def save(self, session_ref: str, data: dict[str, Any], ttl_s: int) -> None: ...

    async def load(self, session_ref: str) -> dict[str, Any] | None:
        """The session, or ``None`` if it is gone or cannot be read."""
        ...

    async def delete(self, session_ref: str) -> None: ...

    async def aclose(self) -> None: ...


class SessionCipher:
    """Encrypts a session for storage. The reference is authenticated with it, so a stored
    value copied under another reference does not decrypt."""

    def __init__(self, key_hex: str) -> None:
        try:
            key = bytes.fromhex(key_hex.strip())
        except ValueError:
            raise ValueError("WORKER_SESSION_KEY must be hexadecimal") from None
        if len(key) != 32:
            raise ValueError("WORKER_SESSION_KEY must be 64 hexadecimal characters (256 bits)")
        self._aead = AESGCM(key)

    def seal(self, session_ref: str, data: dict[str, Any]) -> bytes:
        plain = json.dumps(data, separators=(",", ":")).encode()
        if len(plain) > MAX_SESSION_BYTES:
            raise ValueError(f"session of {len(plain)} bytes exceeds the limit of {MAX_SESSION_BYTES}")
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plain, session_ref.encode())

    def open(self, session_ref: str, stored: bytes) -> dict[str, Any] | None:
        try:
            plain = self._aead.decrypt(stored[:_NONCE_BYTES], stored[_NONCE_BYTES:], session_ref.encode())
            data = json.loads(plain)
        except (InvalidTag, ValueError):
            return None
        return data if isinstance(data, dict) else None


class RedisSessionStore:
    """Encrypted sessions in Redis, each with its own expiry."""

    def __init__(self, url: str, key_hex: str, *, prefix: str = "courier:session:") -> None:
        self._cipher = SessionCipher(key_hex)
        self._redis = Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        self._prefix = prefix

    async def save(self, session_ref: str, data: dict[str, Any], ttl_s: int) -> None:
        sealed = self._cipher.seal(session_ref, data)
        try:
            await self._redis.set(self._prefix + session_ref, sealed, ex=max(1, ttl_s))
        except (RedisError, OSError) as error:
            # Only the kind of failure: the exception text can contain the connection URL.
            raise SessionStoreUnavailable(
                f"the session store is unreachable ({type(error).__name__})"
            ) from None

    async def load(self, session_ref: str) -> dict[str, Any] | None:
        try:
            stored = await self._redis.get(self._prefix + session_ref)
        except (RedisError, OSError) as error:
            raise SessionStoreUnavailable(
                f"the session store is unreachable ({type(error).__name__})"
            ) from None
        return self._cipher.open(session_ref, stored) if isinstance(stored, bytes) else None

    async def delete(self, session_ref: str) -> None:
        with contextlib.suppress(RedisError, OSError):  # it expires by itself anyway
            await self._redis.delete(self._prefix + session_ref)

    async def aclose(self) -> None:
        await self._redis.aclose()


class MemorySessionStore:
    """Sessions in this process only — for tests and single-process experiments. Sessions are
    lost on restart and invisible to other workers, so it is never used by a real worker."""

    def __init__(self) -> None:
        self._sessions: dict[str, tuple[float, str]] = {}

    async def save(self, session_ref: str, data: dict[str, Any], ttl_s: int) -> None:
        self._sessions[session_ref] = (time.monotonic() + ttl_s, json.dumps(data))

    async def load(self, session_ref: str) -> dict[str, Any] | None:
        entry = self._sessions.get(session_ref)
        if entry is None or entry[0] <= time.monotonic():
            self._sessions.pop(session_ref, None)
            return None
        loaded: dict[str, Any] = json.loads(entry[1])
        return loaded

    async def delete(self, session_ref: str) -> None:
        self._sessions.pop(session_ref, None)

    async def aclose(self) -> None:
        self._sessions.clear()

    def __len__(self) -> int:
        return len(self._sessions)
