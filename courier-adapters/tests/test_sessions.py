"""Sessions kept while a lookup waits for a person: encrypted, bound to their reference, expiring."""

from __future__ import annotations

import os
import secrets

import pytest
from redis.asyncio import Redis

from courier_adapter_sdk.contracts import ErrorClass, new_id
from courier_adapter_sdk.sessions import (
    MAX_SESSION_BYTES,
    MemorySessionStore,
    RedisSessionStore,
    SessionCipher,
    SessionStoreUnavailable,
)

KEY = secrets.token_hex(32)
# Where to look for a Redis: the one named explicitly, else the Compose one (make up-deps),
# else a local Redis on its usual port. The Redis test is skipped if none answers.
REDIS_URLS = (
    [os.environ["TEST_REDIS_URL"]]
    if os.environ.get("TEST_REDIS_URL")
    else ["redis://:dev_redis_password@127.0.0.1:56379/0", "redis://127.0.0.1:6379/0"]
)
SESSION = {"token": "abc", "cookies": {"sid": "s3cr3t-cookie"}}


def test_a_sealed_session_does_not_show_its_content_and_opens_again() -> None:
    cipher, ref = SessionCipher(KEY), new_id()
    sealed = cipher.seal(ref, SESSION)
    assert b"s3cr3t-cookie" not in sealed
    assert b"token" not in sealed
    assert cipher.open(ref, sealed) == SESSION
    assert cipher.seal(ref, SESSION) != sealed, "a fresh nonce every time"


def test_a_session_opens_only_under_its_own_reference_and_key() -> None:
    cipher, ref = SessionCipher(KEY), new_id()
    sealed = cipher.seal(ref, SESSION)
    assert cipher.open(new_id(), sealed) is None, "copied under another reference"
    assert SessionCipher(secrets.token_hex(32)).open(ref, sealed) is None, "another key"
    tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    assert cipher.open(ref, tampered) is None
    assert cipher.open(ref, b"short") is None


@pytest.mark.parametrize("key", ["", "abc", "zz" * 32, "ab" * 16])
def test_a_wrong_key_is_refused_without_repeating_it(key: str) -> None:
    with pytest.raises(ValueError, match="WORKER_SESSION_KEY") as raised:
        SessionCipher(key)
    assert key == "" or key not in str(raised.value)


def test_an_oversized_session_is_refused() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        SessionCipher(KEY).seal(new_id(), {"page": "x" * (MAX_SESSION_BYTES + 1)})


async def test_the_memory_store_keeps_loads_deletes_and_expires() -> None:
    store, ref = MemorySessionStore(), new_id()
    await store.save(ref, SESSION, 60)
    assert await store.load(ref) == SESSION
    await store.delete(ref)
    assert await store.load(ref) is None
    await store.save(ref, SESSION, 0)
    assert await store.load(ref) is None, "expired"
    assert len(store) == 0


async def _redis_or_skip() -> tuple[str, Redis]:
    for url in REDIS_URLS:
        client = Redis.from_url(url, socket_connect_timeout=1)
        try:
            await client.ping()
        except Exception:
            await client.aclose()
            continue
        return url, client
    pytest.skip("no Redis reachable for this test")


async def test_redis_holds_only_ciphertext_with_an_expiry() -> None:
    url, raw = await _redis_or_skip()
    prefix = f"test-{secrets.token_hex(4)}:session:"
    store, ref = RedisSessionStore(url, KEY, prefix=prefix), new_id()
    try:
        await store.save(ref, SESSION, 120)
        stored = await raw.get(prefix + ref)
        assert stored is not None
        assert b"s3cr3t-cookie" not in stored
        assert 0 < await raw.ttl(prefix + ref) <= 120
        assert await store.load(ref) == SESSION

        # Another worker of the same adapter (same key) can continue; a different key cannot.
        other = RedisSessionStore(url, KEY, prefix=prefix)
        assert await other.load(ref) == SESSION
        await other.aclose()
        stranger = RedisSessionStore(url, secrets.token_hex(32), prefix=prefix)
        assert await stranger.load(ref) is None
        await stranger.aclose()

        await store.delete(ref)
        assert await raw.exists(prefix + ref) == 0
        assert await store.load(ref) is None
    finally:
        await raw.delete(prefix + ref)
        await raw.aclose()
        await store.aclose()


async def test_an_unreachable_store_is_a_retryable_failure_that_hides_the_url() -> None:
    store = RedisSessionStore("redis://:hunter2@127.0.0.1:1/0", KEY)
    try:
        with pytest.raises(SessionStoreUnavailable) as raised:
            await store.save(new_id(), SESSION, 60)
        info = raised.value.to_info()
        assert info.error_class is ErrorClass.SESSION_LOST
        assert info.retryable
        assert "hunter2" not in info.message
        with pytest.raises(SessionStoreUnavailable):
            await store.load(new_id())
        await store.delete(new_id())  # best effort: never raises
    finally:
        await store.aclose()
