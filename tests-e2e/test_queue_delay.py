"""Acceptance criteria 33, 33a, 33b, 33d: waiting in a queue is not an adapter timeout."""

from __future__ import annotations

import time

from harness import Stack, User, number


def errors(stack: Stack, job_id: str) -> set[str | None]:
    rows = stack.query("SELECT error_class::text FROM job_attempts WHERE job_id = %s", job_id)
    return {r["error_class"] for r in rows}


def test_jobs_that_waited_for_a_stopped_worker_complete_on_their_first_attempt(
    stack: Stack, user: User, quiet: None
) -> None:
    with stack.provider(timeout_ms=1000, result_grace_ms=500, queue_wait_max_ms=30_000):
        stack.worker.stop()
        requests = [user.submit(number("OK")) for _ in range(5)]
        time.sleep(3)  # longer than timeout_ms + result_grace_ms, shorter than queue_wait_max_ms
        assert {user.job(r)["status"] for r in requests} == {"QUEUED"}
        stack.start_worker()
        for request_id in requests:
            job = user.wait_for(request_id)["latest_job"]
            assert (job["status"], job["attempt"], job["execution_count"]) == ("COMPLETED", 1, 1)
            assert job["last_error_class"] is None
            assert errors(stack, job["id"]) == {None}


def test_jobs_behind_a_busy_worker_are_not_timed_out(stack: Stack, user: User, quiet: None) -> None:
    with stack.provider(timeout_ms=1000, result_grace_ms=500, queue_wait_max_ms=30_000):
        stack.restart_worker(concurrency=1)
        requests = [user.submit(number("DELAY-600")) for _ in range(6)]  # the last waits ~3 s
        for request_id in requests:
            job = user.wait_for(request_id, timeout_s=30)["latest_job"]
            assert (job["status"], job["attempt"]) == ("COMPLETED", 1)
            assert errors(stack, job["id"]) == {None}


def test_the_adapter_timeout_counts_from_execution_start_not_from_dispatch(
    stack: Stack, user: User, quiet: None
) -> None:
    with stack.provider(timeout_ms=1500, queue_wait_max_ms=30_000, max_attempts=1):
        stack.worker.stop()
        request_id = user.submit(number("SLOW"))
        time.sleep(2.5)  # waits in the queue for longer than timeout_ms
        stack.start_worker()
        job = user.wait_for(request_id, "FAILED", timeout_s=15)["latest_job"]
        assert job["last_error_class"] == "ADAPTER_TIMEOUT"
    (log,) = stack.query(
        "SELECT (metrics ->> 'duration_ms')::int AS duration_ms FROM adapter_execution_logs WHERE job_id = %s",
        job["id"],
    )
    assert 1400 <= log["duration_ms"] < 2500, "it ran for its full budget"


def test_a_job_no_worker_picks_up_fails_with_queue_wait_exceeded(stack: Stack, user: User, quiet: None) -> None:
    with stack.provider(queue_wait_max_ms=1000, retry_base_ms=200, max_attempts=2):
        stack.worker.stop()
        request_id = user.submit(number("OK"))
        job = user.wait_for(request_id, "FAILED", timeout_s=20)["latest_job"]
        assert (job["attempt"], job["last_error_class"]) == (2, "QUEUE_WAIT_EXCEEDED")
        assert errors(stack, job["id"]) == {"QUEUE_WAIT_EXCEEDED"}
        time.sleep(6)  # well past expires_at plus the worker's clock tolerance
        stack.start_worker()
        # A later job proves the worker has gone through the two expired messages before it.
        assert user.wait_for(user.submit(number("OK")))["latest_job"]["status"] == "COMPLETED"
    assert stack.query("SELECT 1 FROM adapter_execution_logs WHERE job_id = %s", job["id"]) == [], "never executed"
    assert user.job(request_id)["status"] == "FAILED"
    dropped = [
        line for line in stack.worker.log_text().splitlines() if "job_dropped_expired" in line and job["id"] in line
    ]
    assert len(dropped) == 2, "both attempts' messages were dropped without running"
