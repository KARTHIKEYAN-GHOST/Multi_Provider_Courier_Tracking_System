"""Sessions: what a worker keeps while a lookup waits for a human answer.

When a provider asks for a one-time code or a CAPTCHA, the adapter hands the SDK whatever it
needs to continue later (cookies, a transaction ID). The worker does not wait: it stores that
session here, tells the Engine, and takes the next job. When the answer arrives, any worker of
the same adapter loads the session and continues.

Sessions are *portable* (plain data, no live browser page). They are held in Redis, which only
adapter workers use — the Engine never sees a session. Because a session can contain cookies
or tokens, it is encrypted before it leaves the process (AES-256-GCM, key
``WORKER_SESSION_KEY``) and bound to its reference, and it expires by itself.

The same store also coordinates *resuming*. RabbitMQ delivers at least once, so the same
``job.resume`` can reach two workers at the same moment. Two things prevent them from both
submitting the user's answer to the provider:

* a **lease** per session: only its holder may continue the session. It is taken atomically,
  carries a random owner token (only the owner can release it) and expires by itself, so a
  worker that died cannot block the lookup for ever;
* a **reported marker**, set once the outcome has been confirmed by the broker: a later
  delivery of the same resume is then simply acknowledged.

Neither makes provider calls exactly-once. A worker can submit the answer and die before it
reports; when its lease expires the resume runs again. See docs/phase-8.md.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from collections.abc import Callable
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

    async def acquire_lease(self, session_ref: str, ttl_s: float) -> str | None:
        """Takes the right to continue this session for ``ttl_s`` seconds.

        Returns a fresh owner token, or ``None`` while somebody else holds the lease. Atomic:
        of any number of simultaneous callers exactly one gets a token."""
        ...

    async def release_lease(self, session_ref: str, owner: str) -> bool:
        """Gives the lease back if ``owner`` still holds it. Never touches a lease that has
        expired and been taken by somebody else, and never raises."""
        ...

    async def mark_reported(self, session_ref: str, ttl_s: int) -> None:
        """Records that the outcome of continuing this session reached the broker."""
        ...

    async def was_reported(self, session_ref: str) -> bool: ...

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


# Compare-and-delete in one step: a lease is removed only by the token that holds it.
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def _unreachable(error: Exception) -> SessionStoreUnavailable:
    # Only the kind of failure: the exception text can contain the connection URL.
    return SessionStoreUnavailable(f"the session store is unreachable ({type(error).__name__})")


class RedisSessionStore:
    """Encrypted sessions in Redis, each with its own expiry, plus the resume leases.

    Sessions live under ``prefix``; leases and reported markers under ``control_prefix``, so
    listing one never shows the other.
    """

    def __init__(
        self,
        url: str,
        key_hex: str,
        *,
        prefix: str = "courier:session:",
        control_prefix: str = "courier:resume:",
    ) -> None:
        self._cipher = SessionCipher(key_hex)
        self._redis = Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        self._prefix = prefix
        self._lease = control_prefix + "lease:"
        self._done = control_prefix + "done:"

    async def acquire_lease(self, session_ref: str, ttl_s: float) -> str | None:
        owner = secrets.token_hex(16)
        try:
            # SET NX PX: created only if absent, and with its expiry, in one command.
            taken = await self._redis.set(
                self._lease + session_ref, owner, nx=True, px=max(1, int(ttl_s * 1000))
            )
        except (RedisError, OSError) as error:
            raise _unreachable(error) from None
        return owner if taken else None

    async def release_lease(self, session_ref: str, owner: str) -> bool:
        try:
            # Typed loosely by the client library; EVAL returns the number of keys deleted.
            removed: object = await self._redis.eval(_RELEASE, 1, self._lease + session_ref, owner)
        except (RedisError, OSError):
            return False  # it expires by itself
        return removed == 1

    async def mark_reported(self, session_ref: str, ttl_s: int) -> None:
        try:
            await self._redis.set(self._done + session_ref, b"1", ex=max(1, ttl_s))
        except (RedisError, OSError) as error:
            raise _unreachable(error) from None

    async def was_reported(self, session_ref: str) -> bool:
        try:
            return bool(await self._redis.exists(self._done + session_ref))
        except (RedisError, OSError) as error:
            raise _unreachable(error) from None

    async def save(self, session_ref: str, data: dict[str, Any], ttl_s: int) -> None:
        sealed = self._cipher.seal(session_ref, data)
        try:
            await self._redis.set(self._prefix + session_ref, sealed, ex=max(1, ttl_s))
        except (RedisError, OSError) as error:
            raise _unreachable(error) from None

    async def load(self, session_ref: str) -> dict[str, Any] | None:
        try:
            stored = await self._redis.get(self._prefix + session_ref)
        except (RedisError, OSError) as error:
            raise _unreachable(error) from None
        return self._cipher.open(session_ref, stored) if isinstance(stored, bytes) else None

    async def delete(self, session_ref: str) -> None:
        with contextlib.suppress(RedisError, OSError):  # it expires by itself anyway
            await self._redis.delete(self._prefix + session_ref)

    async def aclose(self) -> None:
        await self._redis.aclose()


class MemorySessionStore:
    """Sessions in this process only — for tests and single-process experiments. Sessions are
    lost on restart and invisible to other workers, so it is never used by a real worker."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sessions: dict[str, tuple[float, str]] = {}
        self._leases: dict[str, tuple[float, str]] = {}
        self._reported: dict[str, float] = {}

    async def acquire_lease(self, session_ref: str, ttl_s: float) -> str | None:
        held = self._leases.get(session_ref)
        if held is not None and held[0] > self._clock():
            return None
        owner = secrets.token_hex(16)
        self._leases[session_ref] = (self._clock() + ttl_s, owner)
        return owner

    async def release_lease(self, session_ref: str, owner: str) -> bool:
        held = self._leases.get(session_ref)
        if held is None or held[1] != owner or held[0] <= self._clock():
            return False
        del self._leases[session_ref]
        return True

    async def mark_reported(self, session_ref: str, ttl_s: int) -> None:
        self._reported[session_ref] = self._clock() + ttl_s

    async def was_reported(self, session_ref: str) -> bool:
        return self._reported.get(session_ref, 0.0) > self._clock()

    async def save(self, session_ref: str, data: dict[str, Any], ttl_s: int) -> None:
        self._sessions[session_ref] = (self._clock() + ttl_s, json.dumps(data))

    async def load(self, session_ref: str) -> dict[str, Any] | None:
        entry = self._sessions.get(session_ref)
        if entry is None or entry[0] <= self._clock():
            self._sessions.pop(session_ref, None)
            return None
        loaded: dict[str, Any] = json.loads(entry[1])
        return loaded

    async def delete(self, session_ref: str) -> None:
        self._sessions.pop(session_ref, None)

    async def aclose(self) -> None:
        self._sessions.clear()
        self._leases.clear()
        self._reported.clear()

    def __len__(self) -> int:
        return len(self._sessions)
