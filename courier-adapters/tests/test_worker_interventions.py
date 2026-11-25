"""The worker's part of the human-intervention workflow: pause, hand over, resume."""

from __future__ import annotations

import base64
import json
from typing import Any, ClassVar

import pytest

from courier_adapter_sdk import (
    Adapter,
    JobContext,
    RawResponse,
    SavedSessionLost,
    Secret,
    TrackingEvent,
    TrackingResult,
    TrackRequest,
    captcha_required,
    otp_required,
)
from courier_adapter_sdk.contracts import MAX_IMAGE_BYTES, new_id, utc_now
from courier_adapter_sdk.credentials import CredentialDenied
from courier_adapter_sdk.sessions import MemorySessionStore
from courier_adapter_sdk.worker import SESSION_MARGIN_S, Worker

from .conftest import FakeDelivery, FakePublisher, job_message, make_settings, schema_errors

ANSWER = "424242"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def result() -> TrackingResult:
    event = TrackingEvent(raw_status="In transit", occurred_at=utc_now())
    return TrackingResult(RawResponse(content_type="text/html", body="<p>ok</p>"), [event])


class GatedAdapter(Adapter):
    """Asks for a code; accepts ``ANSWER``; asks again for anything else."""

    key: ClassVar[str] = "dummy"
    version: ClassVar[str] = "1.0.0"

    def __init__(self, kind: str = "otp") -> None:
        self.kind = kind
        self.resumed: list[tuple[dict[str, Any], str]] = []

    def challenge(self, step: int) -> Exception:
        session = {"cookie": "s3cr3t-cookie", "step": step}
        if self.kind == "captcha":
            return captcha_required("Type the characters.", session, image=PNG, content_type="image/png")
        return otp_required("Enter the code.", session, digits=6, expires_in_s=120)

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        raise self.challenge(1)

    async def resume(
        self, request: TrackRequest, ctx: JobContext, session: dict[str, Any], response: str
    ) -> TrackingResult:
        self.resumed.append((session, response))
        if response != ANSWER:
            raise self.challenge(session["step"] + 1)
        return result()


class Answers:
    """Stands in for the Engine's internal API."""

    def __init__(self, answer: str | Exception = ANSWER) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str, int]] = []

    async def resolve(self, credential_ref: str, job_id: str, attempt: int) -> Secret:
        raise AssertionError("not used")

    async def intervention_response(self, intervention_id: str, job_id: str, attempt: int) -> str:
        self.asked.append((intervention_id, job_id, attempt))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Rig:
    def __init__(self, adapter: Adapter | None = None, *, sessions: Any = "memory", answers: Any = None):
        self.adapter = adapter or GatedAdapter()
        self.publisher = FakePublisher()
        self.sessions = MemorySessionStore() if sessions == "memory" else sessions
        self.answers = answers or Answers()
        self.worker = Worker(
            self.adapter, make_settings(), self.publisher, resolver=self.answers, sessions=self.sessions
        )

    async def deliver(self, message: dict[str, Any], delivery_count: int = 0) -> FakeDelivery:
        delivery = FakeDelivery(message, delivery_count)
        await self.worker.submit(delivery)
        await self.worker.drain(5)
        return delivery

    @property
    def challenges(self) -> list[dict[str, Any]]:
        return [m["payload"] for m in self.publisher.of("job.intervention_required")]

    @property
    def results(self) -> list[dict[str, Any]]:
        return [m["payload"] for m in self.publisher.results]


def resume_message(
    track: dict[str, Any], session_ref: str, intervention_id: str | None = None
) -> dict[str, Any]:
    """The ``job.resume`` the Engine sends once the user has answered: the same job and attempt."""
    payload = {**track["payload"], "intervention_id": intervention_id or new_id(), "session_ref": session_ref}
    return {**track, "message_id": new_id(), "type": "job.resume", "payload": payload}


async def test_a_challenge_pauses_the_job_and_tells_the_engine_without_the_session() -> None:
    rig, track = Rig(), job_message()
    delivery = await rig.deliver(track)

    assert delivery.acked, "the worker is free for other jobs while the person answers"
    assert rig.results == []
    (challenge,) = rig.challenges
    assert challenge["kind"] == "otp"
    assert challenge["prompt"] == "Enter the code."
    assert challenge["response_format"] == {"min_length": 6, "max_length": 6, "charset": "digits"}
    assert challenge["expires_in_s"] == 120
    assert challenge["job_id"] == track["payload"]["job_id"]
    assert challenge["attempt"] == 1
    (message,) = rig.publisher.of("job.intervention_required")
    assert schema_errors(message) == []
    assert message["correlation_id"] == track["correlation_id"]

    # The session went to the store under the reference; the message has only the reference.
    assert await rig.sessions.load(challenge["session_ref"]) == {"cookie": "s3cr3t-cookie", "step": 1}
    assert b"s3cr3t-cookie" not in b"".join(rig.publisher.bodies)
    assert rig.worker.running == 0


async def test_the_session_outlives_the_challenge_by_a_margin_but_not_the_configured_maximum() -> None:
    saved: list[int] = []

    class Recording(MemorySessionStore):
        async def save(self, session_ref: str, data: dict[str, Any], ttl_s: int) -> None:
            saved.append(ttl_s)
            await super().save(session_ref, data, ttl_s)

    await Rig(sessions=Recording()).deliver(job_message())
    assert saved == [120 + SESSION_MARGIN_S]

    rig = Rig(GatedAdapter("captcha"), sessions=Recording())  # no lifetime given by the adapter
    await rig.deliver(job_message())
    assert saved[-1] == make_settings().worker_session_ttl_s


async def test_a_captcha_picture_travels_as_data() -> None:
    rig = Rig(GatedAdapter("captcha"))
    await rig.deliver(job_message())
    (challenge,) = rig.challenges
    assert challenge["kind"] == "captcha"
    assert challenge["image"]["content_type"] == "image/png"
    assert base64.b64decode(challenge["image"]["data"]) == PNG
    assert schema_errors(rig.publisher.of("job.intervention_required")[0]) == []


def test_a_picture_that_is_not_a_plain_image_is_refused_by_the_sdk() -> None:
    with pytest.raises(ValueError, match="not a PNG"):
        captcha_required("x", {}, image=b"<svg/>", content_type="image/svg+xml")
    with pytest.raises(ValueError, match="bytes"):
        captcha_required("x", {}, image=b"x" * (MAX_IMAGE_BYTES + 1), content_type="image/png")
    with pytest.raises(ValueError, match="bytes"):
        captcha_required("x", {}, image=b"", content_type="image/png")


async def test_resume_continues_with_the_saved_session_and_the_users_answer() -> None:
    rig, track = Rig(), job_message()
    await rig.deliver(track)
    session_ref = rig.challenges[0]["session_ref"]
    intervention_id = new_id()

    delivery = await rig.deliver(resume_message(track, session_ref, intervention_id))

    assert delivery.acked
    assert rig.adapter.resumed == [({"cookie": "s3cr3t-cookie", "step": 1}, ANSWER)]  # type: ignore[attr-defined]
    assert rig.answers.asked == [(intervention_id, track["payload"]["job_id"], 1)]
    (outcome,) = rig.results
    assert outcome["outcome"] == "SUCCESS"
    assert outcome["attempt"] == 1, "the same logical attempt"
    assert schema_errors(rig.publisher.results[0]) == []
    assert await rig.sessions.load(session_ref) is None, "a used session is removed"
    # The answer was used, not recorded: it is in no message.
    assert ANSWER.encode() not in b"".join(rig.publisher.bodies)


async def test_the_answer_never_reaches_the_log(capfd: pytest.CaptureFixture[str]) -> None:
    from courier_adapter_sdk.logging import configure_logging

    configure_logging("json")
    rig, track = Rig(answers=Answers("917356")), job_message()
    await rig.deliver(track)
    await rig.deliver(resume_message(track, rig.challenges[0]["session_ref"]))
    assert len(rig.challenges) == 2, "a wrong answer: the adapter asked again"
    captured = capfd.readouterr()
    assert "job_waiting_for_human" in captured.out + captured.err
    assert "917356" not in captured.out + captured.err
    assert "s3cr3t-cookie" not in captured.out + captured.err


async def test_an_answer_the_provider_rejects_becomes_a_new_challenge_with_a_new_session() -> None:
    rig, track = Rig(answers=Answers("000000")), job_message()
    await rig.deliver(track)
    first = rig.challenges[0]["session_ref"]

    delivery = await rig.deliver(resume_message(track, first))

    assert delivery.acked
    assert rig.results == []
    second = rig.challenges[1]["session_ref"]
    assert second != first
    assert await rig.sessions.load(first) is None
    assert await rig.sessions.load(second) == {"cookie": "s3cr3t-cookie", "step": 2}


async def test_a_session_that_is_gone_is_reported_as_session_lost_without_asking_for_the_answer() -> None:
    rig, track = Rig(), job_message()
    delivery = await rig.deliver(resume_message(track, new_id()))
    assert delivery.acked
    (outcome,) = rig.results
    assert outcome["outcome"] == "FAILURE"
    assert outcome["error"]["class"] == "SESSION_LOST"
    assert outcome["error"]["retryable"] is True
    assert rig.answers.asked == [], "no answer is fetched for a lookup that cannot continue"
    assert rig.adapter.resumed == []  # type: ignore[attr-defined]


async def test_an_answer_the_engine_refuses_fails_the_execution() -> None:
    rig, track = Rig(answers=Answers(CredentialDenied("not permitted for this job"))), job_message()
    await rig.deliver(track)
    await rig.deliver(resume_message(track, rig.challenges[0]["session_ref"]))
    (outcome,) = rig.results
    assert outcome["error"]["class"] == "AUTH_INVALID"
    assert outcome["error"]["retryable"] is False


async def test_a_redelivered_resume_can_run_again_because_the_session_is_removed_only_after_reporting() -> (
    None
):
    rig, track = Rig(), job_message()
    await rig.deliver(track)
    session_ref = rig.challenges[0]["session_ref"]
    resume = resume_message(track, session_ref)

    rig.publisher.fail.add("job.result")
    lost = await rig.deliver(resume)
    assert not lost.settled, "the result was not published: the message goes back to the broker"
    assert rig.worker.session_lost.is_set()
    assert await rig.sessions.load(session_ref) is not None

    rig.publisher.fail.clear()
    again = await rig.deliver(resume, delivery_count=1)
    assert again.acked
    assert rig.results[0]["outcome"] == "SUCCESS"
    assert await rig.sessions.load(session_ref) is None


async def test_a_challenge_that_cannot_be_published_is_not_acknowledged() -> None:
    rig = Rig()
    rig.publisher.fail.add("job.intervention_required")
    delivery = await rig.deliver(job_message())
    assert not delivery.settled
    assert rig.worker.session_lost.is_set()


async def test_a_worker_without_a_session_store_reports_a_failure_instead_of_a_challenge() -> None:
    rig = Rig(sessions=None)
    delivery = await rig.deliver(job_message())
    assert delivery.acked
    assert rig.challenges == []
    (outcome,) = rig.results
    assert outcome["error"]["class"] == "INTERNAL"
    # ... and it cannot resume either.
    await rig.deliver(resume_message(job_message(), new_id()))
    assert rig.results[1]["error"]["class"] == "INTERNAL"
    assert rig.answers.asked == []


async def test_an_adapter_that_challenges_but_cannot_resume_is_a_reported_bug() -> None:
    class NoResume(GatedAdapter):
        resume = Adapter.resume

    rig, track = Rig(NoResume()), job_message()
    await rig.deliver(track)
    await rig.deliver(resume_message(track, rig.challenges[0]["session_ref"]))
    assert rig.results[0]["error"]["class"] == "INTERNAL"


async def test_an_adapter_can_report_a_lost_session_itself() -> None:
    class Expired(GatedAdapter):
        async def resume(self, request: Any, ctx: Any, session: Any, response: str) -> TrackingResult:
            raise SavedSessionLost("the site's verification step has expired")

    rig, track = Rig(Expired()), job_message()
    await rig.deliver(track)
    await rig.deliver(resume_message(track, rig.challenges[0]["session_ref"]))
    assert rig.results[0]["error"]["class"] == "SESSION_LOST"


@pytest.mark.parametrize("missing", ["intervention_id", "session_ref"])
async def test_an_incomplete_resume_message_is_dead_lettered(missing: str) -> None:
    rig = Rig()
    message = resume_message(job_message(), new_id())
    del message["payload"][missing]
    delivery = await rig.deliver(message)
    assert delivery.rejected
    assert rig.publisher.sent == []


async def test_the_shared_resume_fixture_is_executed() -> None:
    from .conftest import fixture_files

    (path,) = [p for p in fixture_files("valid", "job.resume") if p.name == "job.resume.basic.json"]
    message = json.loads(path.read_text())
    rig = Rig()
    await rig.sessions.save(message["payload"]["session_ref"], {"step": 1}, 60)
    message["payload"]["adapter_key"] = "dummy"
    delivery = await rig.deliver(message, delivery_count=1)  # a redelivery: never dropped as expired
    assert delivery.acked
    assert rig.results[0]["outcome"] == "SUCCESS"
