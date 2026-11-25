"""Only one execution at a time may continue a session.

RabbitMQ delivers at least once, so two workers can hold the same ``job.resume`` at the same
moment. These tests run two ``Worker`` objects — two worker processes, as far as the lease is
concerned — on one shared session store and check that the user's answer is submitted to the
provider by one of them only, and that nothing is lost when the first one fails or dies.

The lease does not make provider calls exactly-once: see the last tests and docs/phase-8.md.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Any, ClassVar

import pytest
from redis.asyncio import Redis

import courier_adapter_sdk.worker as worker_module
from courier_adapter_sdk import (
    Adapter,
    JobContext,
    RawResponse,
    Secret,
    TrackingEvent,
    TrackingResult,
    TrackRequest,
    captcha_required,
    otp_required,
)
from courier_adapter_sdk.contracts import new_id, utc_now
from courier_adapter_sdk.logging import configure_logging
from courier_adapter_sdk.sessions import (
    MemorySessionStore,
    RedisSessionStore,
    SessionStore,
    SessionStoreUnavailable,
)
from courier_adapter_sdk.worker import Worker

from .conftest import FakeDelivery, FakePublisher, job_message, make_settings, until

ANSWER = "424242"
COOKIE = "s3cr3t-cookie"
KEY = secrets.token_hex(32)
REDIS_URLS = (
    [os.environ["TEST_REDIS_URL"]]
    if os.environ.get("TEST_REDIS_URL")
    else ["redis://:dev_redis_password@127.0.0.1:56379/0", "redis://127.0.0.1:6379/0"]
)


class Provider(Adapter):
    """A courier that accepts ``ANSWER``. Counts how often an answer was submitted to it and
    how many submissions were in progress at the same moment. ``gate`` holds a submission
    open, the way a slow provider would."""

    key: ClassVar[str] = "dummy"
    version: ClassVar[str] = "1.0.0"

    def __init__(self) -> None:
        self.submissions = 0
        self.in_progress = 0
        self.most_at_once = 0
        self.gate = asyncio.Event()
        self.gate.set()
        self.fail_next: Exception | None = None

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        raise otp_required("Enter the code.", {"cookie": COOKIE}, digits=6)

    async def resume(
        self, request: TrackRequest, ctx: JobContext, session: dict[str, Any], response: str
    ) -> TrackingResult:
        assert session == {"cookie": COOKIE}
        self.submissions += 1
        self.in_progress += 1
        self.most_at_once = max(self.most_at_once, self.in_progress)
        try:
            await self.gate.wait()
            if self.fail_next is not None:
                error, self.fail_next = self.fail_next, None
                raise error
            assert response == ANSWER
            event = TrackingEvent(raw_status="In transit", occurred_at=utc_now())
            return TrackingResult(RawResponse(content_type="text/html", body="<p>ok</p>"), [event])
        finally:
            self.in_progress -= 1


class Engine:
    def __init__(self) -> None:
        self.asked = 0

    async def resolve(self, credential_ref: str, job_id: str, attempt: int) -> Secret:
        raise AssertionError("not used")

    async def intervention_response(
        self, intervention_id: str, session_ref: str, job_id: str, attempt: int
    ) -> str:
        self.asked += 1
        return ANSWER


class TokenRecorder(MemorySessionStore):
    """Remembers every owner token it hands out, so tests can check none of them leaks."""

    def __init__(self) -> None:
        super().__init__()
        self.tokens: list[str] = []

    async def acquire_lease(self, session_ref: str, ttl_s: float) -> str | None:
        owner = await super().acquire_lease(session_ref, ttl_s)
        if owner is not None:
            self.tokens.append(owner)
        return owner


class Fleet:
    """Two workers of one adapter on one session store and one broker."""

    def __init__(self, store: SessionStore | None = None, **settings: Any) -> None:
        self.provider = Provider()
        self.engine = Engine()
        self.publisher = FakePublisher()
        self.store: SessionStore = store if store is not None else TokenRecorder()
        options = {"worker_resume_lease_margin_s": 5.0, "worker_resume_lease_poll_s": 0.02, **settings}
        self.workers = [
            Worker(
                self.provider,
                make_settings(worker_id=f"worker-{name}", **options),
                self.publisher,
                resolver=self.engine,
                sessions=self.store,
            )
            for name in ("a", "b")
        ]
        self.track = job_message(timeout_ms=2000)

    async def challenge(self) -> dict[str, Any]:
        """Runs the lookup up to the question and returns the ``job.resume`` that follows the answer."""
        await self.start(0, self.track)
        await self.finish()
        (asked,) = self.publisher.of("job.intervention_required")
        payload = {
            **self.track["payload"],
            "intervention_id": new_id(),
            "session_ref": asked["payload"]["session_ref"],
        }
        return {**self.track, "message_id": new_id(), "type": "job.resume", "payload": payload}

    async def start(self, worker: int, message: dict[str, Any], delivery_count: int = 0) -> FakeDelivery:
        delivery = FakeDelivery(message, delivery_count)
        await self.workers[worker].submit(delivery)
        return delivery

    async def finish(self) -> None:
        for worker in self.workers:
            await worker.drain(10)

    @property
    def results(self) -> list[dict[str, Any]]:
        return [m["payload"] for m in self.publisher.results]


def session_of(resume: dict[str, Any]) -> str:
    return str(resume["payload"]["session_ref"])


# ----------------------------------------------------------------------------- the lease itself


async def redis_store() -> RedisSessionStore:
    for url in REDIS_URLS:
        client = Redis.from_url(url, socket_connect_timeout=1)
        try:
            await client.ping()
        except Exception:
            continue
        finally:
            await client.aclose()
        run = secrets.token_hex(4)
        return RedisSessionStore(
            url, KEY, prefix=f"test-{run}:session:", control_prefix=f"test-{run}:resume:"
        )
    pytest.skip("no Redis reachable for this test")


@pytest.fixture(params=["memory", "redis"])
async def store(request: pytest.FixtureRequest) -> Any:
    instance: SessionStore = MemorySessionStore() if request.param == "memory" else await redis_store()
    yield instance
    await instance.aclose()


async def test_a_lease_has_one_holder_and_only_the_holder_can_release_it(store: SessionStore) -> None:
    ref = new_id()
    owner = await store.acquire_lease(ref, 30)
    assert owner is not None
    assert await store.acquire_lease(ref, 30) is None, "taken"
    assert await store.acquire_lease(new_id(), 30) is not None, "another session is unaffected"

    assert await store.release_lease(ref, "not-the-owner") is False
    assert await store.release_lease(ref, secrets.token_hex(16)) is False
    assert await store.acquire_lease(ref, 30) is None, "a stranger's release changed nothing"

    assert await store.release_lease(ref, owner) is True
    assert await store.release_lease(ref, owner) is False, "released once"
    assert await store.acquire_lease(ref, 30) is not None


async def test_taking_a_lease_is_atomic(store: SessionStore) -> None:
    ref = new_id()
    owners = await asyncio.gather(*(store.acquire_lease(ref, 30) for _ in range(25)))
    assert len([owner for owner in owners if owner is not None]) == 1


async def test_a_lease_expires_and_its_late_owner_cannot_release_the_next_holders(
    store: SessionStore,
) -> None:
    ref = new_id()
    crashed = await store.acquire_lease(ref, 0.15)
    assert crashed is not None
    assert await store.acquire_lease(ref, 30) is None
    await asyncio.sleep(0.3)  # the holder never came back
    successor = await store.acquire_lease(ref, 30)
    assert successor is not None, "the lease of a dead worker does not block for ever"
    assert successor != crashed
    # The first holder wakes up late: its token no longer opens anything.
    assert await store.release_lease(ref, crashed) is False
    assert await store.acquire_lease(ref, 30) is None, "the successor still holds it"
    assert await store.release_lease(ref, successor) is True


async def test_the_reported_marker_is_separate_from_the_lease_and_expires(store: SessionStore) -> None:
    ref = new_id()
    assert await store.was_reported(ref) is False
    await store.mark_reported(ref, 1)
    assert await store.was_reported(ref) is True
    assert await store.was_reported(new_id()) is False
    assert await store.acquire_lease(ref, 30) is not None, "the marker is not a lease"
    await asyncio.sleep(1.2)
    assert await store.was_reported(ref) is False


async def test_lease_and_marker_keys_are_kept_apart_from_sessions_in_redis() -> None:
    instance = await redis_store()
    raw = instance._redis
    ref = new_id()
    try:
        await instance.save(ref, {"cookie": COOKIE}, 60)
        owner = await instance.acquire_lease(ref, 30)
        await instance.mark_reported(ref, 60)
        assert owner is not None
        keys = sorted([key.decode() async for key in raw.scan_iter(match=f"{instance._prefix[:13]}*")])
        assert keys == sorted([instance._prefix + ref, instance._lease + ref, instance._done + ref])
        assert 0 < await raw.pttl(instance._lease + ref) <= 30_000
        assert await raw.get(instance._lease + ref) == owner.encode()
        assert await instance.load(ref) == {"cookie": COOKIE}
    finally:
        await raw.delete(instance._prefix + ref, instance._lease + ref, instance._done + ref)
        await instance.aclose()


async def test_an_unreachable_store_refuses_a_lease_loudly_and_a_release_quietly() -> None:
    instance = RedisSessionStore("redis://:hunter2@127.0.0.1:1/0", KEY)
    try:
        with pytest.raises(SessionStoreUnavailable) as raised:
            await instance.acquire_lease(new_id(), 5)
        assert "hunter2" not in str(raised.value)
        with pytest.raises(SessionStoreUnavailable):
            await instance.was_reported(new_id())
        assert await instance.release_lease(new_id(), "owner") is False
    finally:
        await instance.aclose()


# ----------------------------------------------------------------------------- two workers, one resume


async def test_two_simultaneous_deliveries_submit_the_answer_once() -> None:
    fleet = Fleet()
    resume = await fleet.challenge()
    fleet.provider.gate.clear()  # the provider is slow: the first submission stays open

    first = await fleet.start(0, resume)
    await until(lambda: fleet.provider.in_progress == 1)
    second = await fleet.start(1, resume, delivery_count=1)
    # Give the second worker ample time to do the wrong thing.
    await asyncio.sleep(0.3)
    assert fleet.provider.submissions == 1, "the second worker is waiting, not submitting"
    assert fleet.engine.asked == 1, "it has not even fetched the answer"
    assert not second.settled, "and it has not given its message up"
    assert fleet.workers[1].running == 1

    fleet.provider.gate.set()
    await fleet.finish()

    assert fleet.provider.submissions == 1
    assert fleet.provider.most_at_once == 1
    assert fleet.engine.asked == 1
    assert [r["outcome"] for r in fleet.results] == ["SUCCESS"], "one result, from the one execution"
    assert len(fleet.publisher.of("job.started")) == 2, "the challenge and one resume: the copy never started"
    assert first.acked
    assert second.acked, "the copy is acknowledged once the outcome is with the broker"
    assert await fleet.store.load(session_of(resume)) is None
    assert await fleet.store.acquire_lease(session_of(resume), 1) is not None, "no lease is left behind"


async def test_many_copies_on_both_workers_still_submit_once() -> None:
    fleet = Fleet()
    resume = await fleet.challenge()
    fleet.provider.gate.clear()
    deliveries = [await fleet.start(i % 2, resume, delivery_count=i) for i in range(6)]
    await until(lambda: fleet.provider.in_progress == 1)
    await asyncio.sleep(0.2)
    fleet.provider.gate.set()
    await fleet.finish()
    assert fleet.provider.submissions == 1
    assert fleet.provider.most_at_once == 1
    assert len(fleet.results) == 1
    assert all(delivery.acked for delivery in deliveries)


async def test_a_deferred_delivery_takes_over_when_the_first_execution_could_not_report() -> None:
    fleet = Fleet()
    resume = await fleet.challenge()
    fleet.provider.gate.clear()
    first = await fleet.start(0, resume)
    await until(lambda: fleet.provider.in_progress == 1)
    second = await fleet.start(1, resume, delivery_count=1)
    await asyncio.sleep(0.1)

    # The first worker's broker connection breaks exactly when it wants to report.
    publish, broken = fleet.publisher.publish, [True]

    async def breaks_once(message_type: str, body: bytes, message_id: str, correlation_id: str) -> None:
        if message_type == "job.result" and broken:
            broken.clear()
            raise ConnectionError("broker unreachable")
        await publish(message_type, body, message_id, correlation_id)

    fleet.publisher.publish = breaks_once  # type: ignore[method-assign]
    fleet.provider.gate.set()
    await fleet.finish()
    assert fleet.workers[0].session_lost.is_set()
    assert not first.settled, "unreported: its message returns to the broker"

    # Nothing was lost: the waiting copy ran, with the session that was deliberately kept.
    assert second.acked
    assert [r["outcome"] for r in fleet.results] == ["SUCCESS"]
    assert fleet.results[0]["worker_id"] == "worker-b"
    assert fleet.provider.submissions == 2, "at-least-once: the answer was submitted again"
    assert fleet.provider.most_at_once == 1, "but never by two executions at the same time"
    assert await fleet.store.load(session_of(resume)) is None


async def test_a_failed_execution_releases_the_lease_at_once() -> None:
    fleet = Fleet()
    resume = await fleet.challenge()
    fleet.provider.fail_next = RuntimeError("bug in the adapter")
    delivery = await fleet.start(0, resume)
    await fleet.finish()
    assert delivery.acked
    assert fleet.results[0]["error"]["class"] == "INTERNAL"
    # Reported, so a late copy is acknowledged without touching the provider again.
    copy = await fleet.start(1, resume, delivery_count=1)
    await fleet.finish()
    assert copy.acked
    assert fleet.provider.submissions == 1
    assert len(fleet.results) == 1
    assert await fleet.store.acquire_lease(session_of(resume), 1) is not None


async def test_a_redelivery_after_the_outcome_was_reported_is_acknowledged_without_executing() -> None:
    """The worker published its result and died before acknowledging: RabbitMQ sends the
    resume again. The outcome is already with the broker, so nothing is executed twice."""
    fleet = Fleet()
    resume = await fleet.challenge()
    await fleet.start(0, resume)
    await fleet.finish()
    assert fleet.provider.submissions == 1

    again = await fleet.start(1, resume, delivery_count=1)
    await fleet.finish()
    assert again.acked
    assert fleet.provider.submissions == 1
    assert fleet.engine.asked == 1
    assert len(fleet.results) == 1
    assert len(fleet.publisher.of("job.started")) == 2


# ----------------------------------------------------------------------------- a worker dies


async def test_the_lease_of_a_dead_worker_expires_and_another_worker_recovers() -> None:
    # Lease lifetime here: 0.2 s execution budget + 0.3 s margin.
    fleet = Fleet(worker_resume_lease_margin_s=0.3)
    fleet.track = job_message(timeout_ms=200)
    resume = await fleet.challenge()
    # A worker took the lease and was killed: nobody will ever release it.
    dead_owner = await fleet.store.acquire_lease(session_of(resume), 0.5)
    assert dead_owner is not None

    began = time.monotonic()
    delivery = await fleet.start(1, resume, delivery_count=1)
    await asyncio.sleep(0.2)
    assert fleet.provider.submissions == 0, "while the lease lives, nobody else continues"
    assert not delivery.settled
    await fleet.finish()

    assert time.monotonic() - began >= 0.45, "it waited for the lease to run out"
    assert delivery.acked
    assert fleet.provider.submissions == 1
    assert [r["outcome"] for r in fleet.results] == ["SUCCESS"]
    assert fleet.results[0]["worker_id"] == "worker-b"
    # The dead worker's token is worthless now.
    assert await fleet.store.release_lease(session_of(resume), dead_owner) is False


async def test_a_worker_whose_lease_ran_out_does_not_release_its_successors() -> None:
    fleet = Fleet()
    resume = await fleet.challenge()
    fleet.provider.gate.clear()
    await fleet.start(0, resume)
    await until(lambda: fleet.provider.in_progress == 1)

    # The first worker stalls past its lease; another execution holds the lease now.
    assert isinstance(fleet.store, TokenRecorder)
    fleet.store._leases.pop(session_of(resume))
    successor = await fleet.store.acquire_lease(session_of(resume), 30)
    assert successor is not None

    fleet.provider.gate.set()
    await fleet.finish()
    assert await fleet.store.acquire_lease(session_of(resume), 30) is None, "the successor's lease survived"
    assert await fleet.store.release_lease(session_of(resume), successor) is True


async def test_shutdown_in_the_middle_of_a_resume_leaves_no_lease_and_no_acknowledgement() -> None:
    fleet = Fleet()
    resume = await fleet.challenge()
    fleet.provider.gate.clear()
    delivery = await fleet.start(0, resume)
    await until(lambda: fleet.provider.in_progress == 1)

    await fleet.workers[0].drain(0)  # SIGTERM with no grace, or the broker connection is gone

    assert not delivery.settled, "the message returns to the broker"
    assert fleet.results == []
    assert await fleet.store.load(session_of(resume)) is not None, "the session is kept for the redelivery"
    assert await fleet.store.was_reported(session_of(resume)) is False
    # The redelivery starts immediately: no lease to wait out.
    fleet.provider.gate.set()
    began = time.monotonic()
    again = await fleet.start(1, resume, delivery_count=1)
    await fleet.finish()
    assert time.monotonic() - began < 1.0
    assert again.acked
    assert [r["outcome"] for r in fleet.results] == ["SUCCESS"]


async def test_shutdown_while_waiting_for_the_lease_leaves_the_holder_undisturbed() -> None:
    fleet = Fleet()
    resume = await fleet.challenge()
    fleet.provider.gate.clear()
    first = await fleet.start(0, resume)
    await until(lambda: fleet.provider.in_progress == 1)
    waiting = await fleet.start(1, resume, delivery_count=1)
    await asyncio.sleep(0.1)

    await fleet.workers[1].drain(0)
    assert not waiting.settled
    assert await fleet.store.acquire_lease(session_of(resume), 1) is None, "the holder still holds"

    fleet.provider.gate.set()
    await fleet.finish()
    assert first.acked
    assert fleet.provider.submissions == 1


async def test_a_lease_that_never_frees_sends_the_message_back_instead_of_dropping_it() -> None:
    fleet = Fleet(worker_resume_lease_margin_s=0.1)
    fleet.track = job_message(timeout_ms=100)
    resume = await fleet.challenge()
    assert await fleet.store.acquire_lease(session_of(resume), 60) is not None  # far beyond any wait

    delivery = await fleet.start(1, resume, delivery_count=1)
    await fleet.finish()

    assert not delivery.settled, "neither acknowledged nor rejected: RabbitMQ redelivers it"
    assert fleet.workers[1].session_lost.is_set()
    assert fleet.provider.submissions == 0
    assert fleet.results == []
    assert await fleet.store.load(session_of(resume)) is not None


async def test_without_the_store_nobody_executes_and_the_engine_is_told() -> None:
    class Down(MemorySessionStore):
        down = False

        async def was_reported(self, session_ref: str) -> bool:
            if self.down:
                raise SessionStoreUnavailable("the session store is unreachable (ConnectionError)")
            return await super().was_reported(session_ref)

    store = Down()
    fleet = Fleet(store)
    resume = await fleet.challenge()
    store.down = True
    delivery = await fleet.start(0, resume)
    await fleet.finish()
    assert delivery.acked
    assert fleet.provider.submissions == 0
    assert fleet.engine.asked == 0
    (failure,) = fleet.results
    assert (failure["error"]["class"], failure["error"]["retryable"]) == ("SESSION_LOST", True)


# ----------------------------------------------------------------------------- nothing leaks


async def test_owner_tokens_answers_and_sessions_stay_out_of_messages_and_logs(
    capfd: pytest.CaptureFixture[str],
) -> None:
    configure_logging("json")
    fleet = Fleet()
    resume = await fleet.challenge()
    fleet.provider.gate.clear()
    await fleet.start(0, resume)
    await until(lambda: fleet.provider.in_progress == 1)
    await fleet.start(1, resume, delivery_count=1)
    await asyncio.sleep(0.1)
    fleet.provider.gate.set()
    await fleet.finish()

    assert isinstance(fleet.store, TokenRecorder)
    assert len(fleet.store.tokens) >= 1
    captured = capfd.readouterr()
    logs = captured.out + captured.err
    assert "resume_deferred" in logs
    assert "resume_already_reported" in logs
    messages = b"".join(fleet.publisher.bodies).decode()
    for secret in (*fleet.store.tokens, ANSWER, COOKIE):
        assert secret not in logs
        assert secret not in messages


# ----------------------------------------------------------------------------- oversized challenge


class BigPicture(Adapter):
    key: ClassVar[str] = "dummy"
    version: ClassVar[str] = "1.0.0"

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        picture = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000
        raise captcha_required(
            "Type the characters.", {"cookie": COOKIE}, image=picture, content_type="image/png"
        )


async def oversized(
    monkeypatch: pytest.MonkeyPatch, *, broker_down: bool
) -> tuple[MemorySessionStore, FakeDelivery, FakePublisher, Worker]:
    # Small enough that the challenge (with its picture) does not fit, large enough for the
    # job message and the failure result.
    monkeypatch.setattr(worker_module, "MAX_MESSAGE_BYTES", 3000)
    store, publisher = MemorySessionStore(), FakePublisher()
    if broker_down:
        publisher.fail.add("job.result")
    worker = Worker(BigPicture(), make_settings(), publisher, sessions=store)
    delivery = FakeDelivery(job_message())
    await worker.submit(delivery)
    await worker.drain(5)
    return store, delivery, publisher, worker


async def test_a_challenge_too_large_to_send_fails_the_job_and_its_saved_session_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, delivery, publisher, _ = await oversized(monkeypatch, broker_down=False)
    assert delivery.acked
    assert publisher.of("job.intervention_required") == []
    (failure,) = [m["payload"] for m in publisher.results]
    assert (failure["outcome"], failure["error"]["class"], failure["error"]["retryable"]) == (
        "FAILURE",
        "INTERNAL",
        False,
    )
    assert len(store) == 0, "nobody will be asked, so nobody will resume: the session must not linger"
    assert COOKIE.encode() not in b"".join(publisher.bodies)


async def test_the_session_of_an_oversized_challenge_is_kept_while_the_failure_is_unreported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, delivery, publisher, worker = await oversized(monkeypatch, broker_down=True)
    assert not delivery.settled, "the failure did not reach the broker: the message is redelivered"
    assert worker.session_lost.is_set()
    assert publisher.results == []
    assert len(store) == 1, "kept until the redelivery is handled or the session expires"
