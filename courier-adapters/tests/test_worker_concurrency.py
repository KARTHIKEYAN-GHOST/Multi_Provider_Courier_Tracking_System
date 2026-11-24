"""The worker runtime without a broker (acceptance criteria 22, 23, 24, 26).

Jobs are held on ``asyncio.Event`` gates, so the tests control exactly when each job finishes
and never rely on sleeps for ordering.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, ClassVar, NoReturn

import pytest

from adapters.dummy import DummyAdapter
from courier_adapter_sdk import Adapter, JobContext, RawResponse, TrackingResult, TrackRequest
from courier_adapter_sdk.worker import CRASH_EXIT_CODE, Worker

from .conftest import FakeDelivery, FakePublisher, job_message, make_settings, schema_errors, until


class GateAdapter(Adapter):
    """Each job waits on a gate named after its tracking number, and records what happened."""

    key: ClassVar[str] = "dummy"
    version: ClassVar[str] = "0.1.0"

    def __init__(self) -> None:
        self.gates: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.running = 0
        self.max_running = 0
        self.started: list[str] = []
        self.cleaned: list[str] = []
        self.contexts: list[JobContext] = []

    def open(self, *numbers: str) -> None:
        for number in numbers:
            self.gates[number].set()

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        number = request.tracking_number
        self.started.append(number)
        self.contexts.append(ctx)

        async def cleanup() -> None:
            self.cleaned.append(number)

        ctx.on_cleanup(cleanup)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if number.startswith("BOOM"):
                raise ValueError("secret provider text")
            await self.gates[number].wait()
            return TrackingResult(RawResponse(content_type="text/plain", body=number), [])
        finally:
            self.running -= 1


def make_worker(
    adapter: Adapter | None = None, **settings: Any
) -> tuple[Worker, GateAdapter | Any, FakePublisher]:
    adapter = adapter or GateAdapter()
    publisher = FakePublisher()
    return Worker(adapter, make_settings(**settings), publisher), adapter, publisher


async def submit(worker: Worker, *deliveries: FakeDelivery) -> asyncio.Task[None]:
    """Feeds deliveries the way the consume loop does: one after another, as slots free up."""

    async def feed() -> None:
        for delivery in deliveries:
            await worker.submit(delivery)

    return asyncio.create_task(feed())


# ----------------------------------------------------------------------------- concurrency


async def test_no_more_than_the_configured_number_of_jobs_run_at_once() -> None:
    worker, adapter, publisher = make_worker(worker_concurrency=2)
    deliveries = [FakeDelivery(job_message(f"JOB-{n}")) for n in range(5)]
    feeding = await submit(worker, *deliveries)

    await until(lambda: adapter.running == 2)
    for _ in range(20):  # give a third job every chance to start, if the limit were broken
        await asyncio.sleep(0)
    assert (adapter.running, worker.running, worker.free_slots) == (2, 2, 0)
    assert adapter.started == ["JOB-0", "JOB-1"]

    adapter.open(*(f"JOB-{n}" for n in range(5)))
    await feeding
    await publisher.wait_for_results(5)
    await until(lambda: worker.running == 0)

    assert adapter.max_running == 2
    assert all(d.acked for d in deliveries)
    assert worker.free_slots == 2


async def test_a_blocked_job_does_not_delay_the_others_and_times_out_on_its_own() -> None:
    worker, adapter, publisher = make_worker(worker_concurrency=3)
    stuck = FakeDelivery(job_message("STUCK", timeout_ms=1000))
    quick = [FakeDelivery(job_message("QUICK-1")), FakeDelivery(job_message("QUICK-2"))]
    adapter.open("QUICK-1", "QUICK-2")
    await submit(worker, stuck, *quick)

    await publisher.wait_for_results(2)
    assert {m["payload"]["outcome"] for m in publisher.results} == {"SUCCESS"}, (
        "both quick results came first"
    )
    assert not stuck.settled
    await until(lambda: worker.running == 1)
    assert adapter.running == 1

    await publisher.wait_for_results(3, timeout_s=3)
    timed_out = publisher.results[-1]["payload"]
    assert timed_out["outcome"] == "FAILURE"
    assert timed_out["error"] == {
        "class": "ADAPTER_TIMEOUT",
        "message": "the adapter did not finish within its execution budget",
        "retryable": True,
    }
    assert 900 <= timed_out["metrics"]["duration_ms"] < 2500

    await until(lambda: worker.running == 0)
    assert "STUCK" in adapter.cleaned, "the cancelled job released its resources"
    assert stuck.acked, "a reported failure is acknowledged; the Engine decides about a retry"
    assert (worker.free_slots, adapter.running) == (3, 0)


async def test_the_execution_budget_starts_when_the_job_runs_not_while_it_waits() -> None:
    worker, adapter, publisher = make_worker(worker_concurrency=1)
    first = FakeDelivery(job_message("FIRST"))
    second = FakeDelivery(job_message("SECOND", timeout_ms=300))
    feeding = await submit(worker, first, second)

    await until(lambda: adapter.running == 1)
    await asyncio.sleep(0.5)  # SECOND waits for a slot for longer than its whole timeout
    assert adapter.started == ["FIRST"]
    adapter.open("FIRST", "SECOND")
    await feeding
    await publisher.wait_for_results(2)

    assert [m["payload"]["outcome"] for m in publisher.results] == ["SUCCESS", "SUCCESS"]


# ----------------------------------------------------------------------------- cleanup


async def test_cleanup_runs_after_success_error_timeout_and_cancellation() -> None:
    worker, adapter, publisher = make_worker(worker_concurrency=4)
    adapter.open("GOOD")
    cancelled = FakeDelivery(job_message("CANCELLED"))
    await submit(
        worker,
        FakeDelivery(job_message("GOOD")),
        FakeDelivery(job_message("BOOM")),
        FakeDelivery(job_message("TIMEOUT", timeout_ms=50)),
        cancelled,
    )
    await publisher.wait_for_results(3)
    await until(lambda: worker.running == 1)
    assert sorted(adapter.cleaned) == ["BOOM", "GOOD", "TIMEOUT"]

    await worker.drain(0)  # shutdown with no grace: the remaining job is cancelled
    assert sorted(adapter.cleaned) == ["BOOM", "CANCELLED", "GOOD", "TIMEOUT"]
    assert not cancelled.settled, "its message stays unacknowledged and returns to the queue"
    assert len(publisher.results) == 3
    assert (worker.running, worker.free_slots) == (0, 4)


async def test_cleanup_callbacks_run_in_reverse_order_and_a_failing_one_does_not_stop_the_rest() -> None:
    order: list[str] = []

    class Resourceful(GateAdapter):
        async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
            async def note(name: str) -> None:
                order.append(name)

            async def broken() -> None:
                raise RuntimeError("close failed")

            ctx.on_cleanup(note, "first")
            ctx.on_cleanup(broken)
            ctx.on_cleanup(note, "second")
            return TrackingResult(RawResponse(content_type="text/plain", body=""), [])

    worker, _, publisher = make_worker(Resourceful())
    delivery = FakeDelivery(job_message("X"))
    await worker.submit(delivery)
    await publisher.wait_for_results(1)
    await until(lambda: worker.running == 0)
    assert order == ["second", "first"]
    assert delivery.acked


async def test_a_cleanup_that_hangs_is_abandoned_after_the_cleanup_timeout() -> None:
    class Hanging(GateAdapter):
        async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
            ctx.on_cleanup(asyncio.Event().wait)
            return TrackingResult(RawResponse(content_type="text/plain", body=""), [])

    worker, _, publisher = make_worker(Hanging(), worker_cleanup_timeout_s=0.1)
    await worker.submit(FakeDelivery(job_message("X")))
    await publisher.wait_for_results(1)
    await until(lambda: worker.running == 0, timeout_s=2)
    assert worker.free_slots == 4


# ----------------------------------------------------------------------------- shutdown


async def test_graceful_shutdown_lets_the_running_job_finish_and_report() -> None:
    worker, adapter, publisher = make_worker()
    delivery = FakeDelivery(job_message("IN-FLIGHT"))
    await worker.submit(delivery)
    await until(lambda: adapter.running == 1)

    draining = asyncio.create_task(worker.drain(grace_s=5))
    await asyncio.sleep(0.05)
    assert not draining.done(), "shutdown waits for the job"
    adapter.open("IN-FLIGHT")
    await draining

    assert publisher.results[0]["payload"]["outcome"] == "SUCCESS"
    assert delivery.acked
    assert worker.running == 0


# ----------------------------------------------------------------------------- messages


async def test_started_and_result_are_valid_messages_that_carry_the_jobs_identity() -> None:
    worker, _, publisher = make_worker(DummyAdapter())
    job = job_message("DUMMY-OK-1", attempt=2)
    delivery = FakeDelivery(job, delivery_count=3)
    await worker.submit(delivery)
    await publisher.wait_for_results(1)

    started, result = publisher.sent
    assert (started["type"], result["type"]) == ("job.started", "job.result")
    for message in (started, result):
        assert schema_errors(message) == []
        assert message["correlation_id"] == job["correlation_id"], "the Engine's correlation ID is kept"
        assert message["causation_id"] == job["message_id"]
        assert message["producer"] == "adapter-dummy@0.1.0"
        payload = message["payload"]
        assert (payload["job_id"], payload["attempt"]) == (job["payload"]["job_id"], 2)
        assert (payload["worker_id"], payload["delivery_count"]) == ("test-worker", 3)
        assert (payload["adapter_key"], payload["adapter_version"]) == ("dummy", "0.1.0")
    assert started["payload"]["execution_id"] == result["payload"]["execution_id"]
    assert started["payload"]["started_at"] == result["payload"]["started_at"]
    assert started["message_id"] != result["message_id"]

    payload = result["payload"]
    assert payload["outcome"] == "SUCCESS"
    assert [e["raw_status"] for e in payload["events"]] == ["PKG_CREATED", "PKG_IN_TRANSIT", "PKG_DELIVERED"]
    assert payload["raw_response"]["content_type"] == "application/json"
    assert "error" not in payload
    assert payload["finished_at"] >= payload["started_at"]


async def test_each_delivery_of_the_same_message_is_a_new_execution() -> None:
    worker, _, publisher = make_worker(DummyAdapter())
    job = job_message("DUMMY-OK-1")
    await worker.submit(FakeDelivery(job, delivery_count=0))
    await worker.submit(FakeDelivery(job, delivery_count=1))
    await publisher.wait_for_results(2)
    first, second = (m["payload"] for m in publisher.results)
    assert first["execution_id"] != second["execution_id"]
    assert (first["job_id"], first["attempt"]) == (second["job_id"], second["attempt"])
    assert (first["delivery_count"], second["delivery_count"]) == (0, 1)


@pytest.mark.parametrize(
    ("number", "outcome", "error_class", "retryable"),
    [
        ("DUMMY-NOTFOUND-1", "NOT_FOUND", None, None),
        ("DUMMY-FAIL-1", "FAILURE", "HTTP_5XX", True),
        ("DUMMY-FATAL-1", "FAILURE", "BLOCKED", False),
        ("DUMMY-FLAKY-1", "FAILURE", "NETWORK_TIMEOUT", True),
    ],
)
async def test_adapter_outcomes_are_reported(
    number: str, outcome: str, error_class: str | None, retryable: bool | None
) -> None:
    worker, _, publisher = make_worker(DummyAdapter())
    delivery = FakeDelivery(job_message(number))
    await worker.submit(delivery)
    await publisher.wait_for_results(1)
    result = publisher.results[0]
    assert schema_errors(result) == []
    assert result["payload"]["outcome"] == outcome
    error = result["payload"].get("error")
    assert (error and error["class"], error and error["retryable"]) == (error_class, retryable)
    assert "events" not in result["payload"] or outcome == "SUCCESS"
    await until(lambda: delivery.acked)


async def test_an_unexpected_adapter_exception_is_reported_as_internal_without_its_text() -> None:
    worker, _, publisher = make_worker()
    await worker.submit(FakeDelivery(job_message("BOOM")))
    await publisher.wait_for_results(1)
    error = publisher.results[0]["payload"]["error"]
    assert (error["class"], error["retryable"]) == ("INTERNAL", True)
    assert "secret provider text" not in json.dumps(publisher.sent)


async def test_an_oversized_result_is_replaced_by_a_failure() -> None:
    class Huge(GateAdapter):
        async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
            return TrackingResult(RawResponse(content_type="text/html", body="x" * 300_000), [])

    worker, _, publisher = make_worker(Huge())
    delivery = FakeDelivery(job_message("X"))
    await worker.submit(delivery)
    await publisher.wait_for_results(1)
    result = publisher.results[0]
    assert len(publisher.bodies[-1]) < 262_144
    assert schema_errors(result) == []
    assert (result["payload"]["outcome"], result["payload"]["error"]["retryable"]) == ("FAILURE", False)


# ----------------------------------------------------------------------------- expiry


async def test_a_first_delivery_past_its_queue_deadline_is_dropped_without_running() -> None:
    worker, adapter, publisher = make_worker()
    delivery = FakeDelivery(job_message("LATE", expires_in_s=-60))
    await worker.submit(delivery)
    await until(lambda: worker.running == 0)
    assert delivery.acked
    assert adapter.started == []
    assert publisher.sent == [], "neither job.started nor job.result"


async def test_a_redelivery_is_executed_even_after_the_queue_deadline() -> None:
    worker, adapter, publisher = make_worker()
    adapter.open("LATE")
    await worker.submit(FakeDelivery(job_message("LATE", expires_in_s=-60), delivery_count=1))
    await publisher.wait_for_results(1)
    assert adapter.started == ["LATE"]


async def test_small_clock_differences_do_not_drop_a_job() -> None:
    worker, adapter, publisher = make_worker(worker_expiry_skew_s=5)
    adapter.open("JUST-LATE")
    await worker.submit(FakeDelivery(job_message("JUST-LATE", expires_in_s=-2)))
    await publisher.wait_for_results(1)
    assert adapter.started == ["JUST-LATE"]


# ----------------------------------------------------------------------------- bad input


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        json.dumps(job_message(adapter_key="another")).encode(),
        json.dumps({**job_message(), "schema_version": "2.0"}).encode(),
        json.dumps({**job_message(), "type": "job.result"}).encode(),
        json.dumps(job_message(timeout_ms=0)).encode(),
        b" " * 262_145,
    ],
    ids=["not-json", "other-adapter", "unknown-major-version", "wrong-type", "invalid-field", "oversized"],
)
async def test_a_message_that_can_never_be_processed_is_rejected_to_the_dead_letter_queue(
    body: bytes,
) -> None:
    worker, adapter, publisher = make_worker()
    delivery = FakeDelivery(body)
    await worker.submit(delivery)
    await until(lambda: worker.running == 0)
    assert (delivery.rejected, delivery.acked) == (True, False)
    assert adapter.started == []
    assert publisher.sent == []
    assert worker.free_slots == 4


# ----------------------------------------------------------------------------- broker trouble


async def test_a_result_that_cannot_be_published_is_never_acknowledged() -> None:
    worker, adapter, publisher = make_worker()
    publisher.fail = {"job.result"}
    adapter.open("X")
    delivery = FakeDelivery(job_message("X"))
    await worker.submit(delivery)
    await until(lambda: worker.running == 0)

    assert not delivery.settled, "left unacknowledged, so RabbitMQ redelivers and counts the failure"
    assert worker.session_lost.is_set(), "the session owner is told to reconnect"
    assert adapter.cleaned == ["X"]
    assert not hasattr(delivery, "nack"), "the worker has no way to requeue explicitly"


async def test_a_lost_started_notice_does_not_stop_the_job() -> None:
    worker, adapter, publisher = make_worker()
    publisher.fail = {"job.started"}
    adapter.open("X")
    delivery = FakeDelivery(job_message("X"))
    await worker.submit(delivery)
    await publisher.wait_for_results(1)
    assert publisher.of("job.started") == []
    assert publisher.results[0]["payload"]["outcome"] == "SUCCESS"
    assert "started_at" in publisher.results[0]["payload"], "the result is complete on its own"
    await until(lambda: delivery.acked)
    assert not worker.session_lost.is_set()


# ----------------------------------------------------------------------------- fault injection


class ProcessExited(BaseException):
    """Stands in for the process dying."""


def fake_exit(code: int) -> NoReturn:
    raise ProcessExited(code)


async def test_fault_injection_is_refused_unless_explicitly_enabled() -> None:
    exits: list[int] = []
    worker = Worker(DummyAdapter(), make_settings(), publisher := FakePublisher(), exit_process=exits.append)  # type: ignore[arg-type]
    for number in ("DUMMY-CRASH-1", "DUMMY-HANG-1", "DUMMY-DUP-1"):
        await worker.submit(FakeDelivery(job_message(number)))
    await publisher.wait_for_results(3)
    assert exits == [], "a production worker cannot be told to crash"
    assert {m["payload"]["error"]["class"] for m in publisher.results} == {"INTERNAL"}


async def test_a_simulated_crash_exits_after_started_and_before_any_result() -> None:
    exits: list[int] = []

    def record_exit(code: int) -> NoReturn:
        exits.append(code)
        raise ProcessExited(code)

    publisher = FakePublisher()
    worker = Worker(
        DummyAdapter(), make_settings(worker_allow_fault_injection=True), publisher, exit_process=record_exit
    )
    delivery = FakeDelivery(job_message("DUMMY-CRASH-1"))
    await worker.submit(delivery)
    await until(lambda: exits != [])
    assert exits == [CRASH_EXIT_CODE]
    assert [m["type"] for m in publisher.sent] == ["job.started"]
    assert not delivery.settled


async def test_a_simulated_hang_reports_started_and_then_nothing() -> None:
    publisher = FakePublisher()
    worker = Worker(DummyAdapter(), make_settings(worker_allow_fault_injection=True), publisher)
    delivery = FakeDelivery(job_message("DUMMY-HANG-1", timeout_ms=50))
    await worker.submit(delivery)
    await asyncio.sleep(0.3)  # well past the 50 ms timeout: a hang is not a timeout
    assert [m["type"] for m in publisher.sent] == ["job.started"]
    assert not delivery.settled
    assert worker.running == 1, "the process is alive and the job still occupies its slot"
    await worker.drain(0)
    assert worker.running == 0


async def test_a_simulated_duplicate_publishes_the_identical_result_twice() -> None:
    publisher = FakePublisher()
    worker = Worker(DummyAdapter(), make_settings(worker_allow_fault_injection=True), publisher)
    delivery = FakeDelivery(job_message("DUMMY-DUP-1"))
    await worker.submit(delivery)
    await publisher.wait_for_results(2)
    assert publisher.bodies[-1] == publisher.bodies[-2], "byte-identical, same message_id"
    assert publisher.results[0]["message_id"] == publisher.results[1]["message_id"]
    await until(lambda: delivery.acked)
