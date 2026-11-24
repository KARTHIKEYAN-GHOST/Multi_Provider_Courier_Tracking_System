"""Acceptance criteria 30-32, 33c, 36: crashed, hung and restarted processes."""

from __future__ import annotations

from harness import Stack, User, number, wait_until


def executions(stack: Stack, job_id: str) -> list[dict[str, object]]:
    return stack.query(
        "SELECT attempt, delivery_count, disposition::text FROM adapter_execution_logs WHERE job_id = %s"
        " ORDER BY attempt, delivery_count",
        job_id,
    )


def test_a_worker_crash_is_survived_by_redelivery_within_the_same_attempt(
    stack: Stack, user: User, quiet: None
) -> None:
    crashes = len(stack.worker.exit_codes)
    job = user.wait_for(user.submit(number("CRASHONCE")), timeout_s=30)["latest_job"]
    assert (job["status"], job["attempt"], job["execution_count"]) == ("COMPLETED", 1, 2)
    assert [(e["delivery_count"], e["disposition"]) for e in executions(stack, job["id"])] == [
        (0, "incomplete"),
        (1, "accepted"),
    ]
    assert stack.worker.exit_codes[crashes:] == [70], "the worker process really died once"


def test_a_job_that_always_crashes_the_worker_is_dead_lettered_and_fails(stack: Stack, user: User, quiet: None) -> None:
    with stack.provider(retry_base_ms=200, timeout_ms=5000):
        crashes = len(stack.worker.exit_codes)
        poison = user.submit(number("CRASH"))
        # A healthy job that arrives while the worker is already in its crash loop. Its
        # deliveries are lost with every crash too, but it has been through fewer of them.
        wait_until(lambda: len(stack.worker.exit_codes) > crashes, 20, "the first crash")
        healthy = user.submit(number("OK"))

        job = user.wait_for(poison, "FAILED", timeout_s=90)["latest_job"]
        assert job["last_error_class"] == "DELIVERY_LIMIT_EXCEEDED"
        (row,) = stack.query("SELECT dead_letter_count, attempt FROM tracking_jobs WHERE id = %s", job["id"])
        assert (row["dead_letter_count"], row["attempt"]) == (2, 2)
        letters = stack.query(
            "SELECT attempt, applied, reason FROM dead_letters WHERE job_id = %s ORDER BY attempt", job["id"]
        )
        assert [(d["attempt"], d["applied"]) for d in letters] == [(1, True), (2, True)]
        assert stack.take("adapter.dummy.jobs.dlq") == [], "the Engine consumed the dead-letter queue"

        neighbour = user.wait_for(healthy, timeout_s=30)["latest_job"]
        assert neighbour["status"] == "COMPLETED", "an innocent job in the same worker still completes"


def test_a_hung_worker_is_detected_as_worker_lost(stack: Stack, user: User, quiet: None) -> None:
    with stack.provider(timeout_ms=800, result_grace_ms=400, max_attempts=1, queue_wait_max_ms=30_000):
        request_id = user.submit(number("HANG"))
        job_id = user.job(request_id)["id"]
        (queued,) = stack.query("SELECT status::text, execution_deadline_at FROM tracking_jobs WHERE id = %s", job_id)
        if queued["status"] == "QUEUED":
            assert queued["execution_deadline_at"] is None
        processing = wait_until(
            lambda: stack.query(
                "SELECT execution_deadline_at FROM tracking_jobs WHERE id = %s AND status = 'PROCESSING'",
                job_id,
            ),
            5,
            "the job to start",
        )
        assert processing[0]["execution_deadline_at"] is not None
        job = user.wait_for(request_id, "FAILED", timeout_s=15)["latest_job"]
        assert job["last_error_class"] == "WORKER_LOST"
    (attempt,) = stack.query("SELECT closed_by::text, error_class::text FROM job_attempts WHERE job_id = %s", job_id)
    assert (attempt["closed_by"], attempt["error_class"]) == ("reaper", "WORKER_LOST")
    assert [e["disposition"] for e in executions(stack, job_id)] == ["incomplete"]
    # The hung job still holds a worker slot and an unacknowledged message; clear both.
    stack.restart_worker(purge=True)


def test_a_result_produced_while_the_engine_was_down_is_processed_after_restart(
    stack: Stack, user: User, quiet: None
) -> None:
    with stack.provider(timeout_ms=5000, result_grace_ms=20_000):
        request_id = user.submit(number("DELAY-1500"))
        user.wait_for(request_id, "PROCESSING", timeout_s=5)
        stack.engine.stop()
        # The worker finishes and publishes while nothing is consuming engine.events.
        job_id = stack.query("SELECT j.id::text FROM tracking_jobs j ORDER BY j.created_at DESC LIMIT 1")[0]["id"]
        wait_until(
            lambda: any("job_finished" in line and job_id in line for line in stack.worker.log_text().splitlines()),
            10,
            "the worker to finish the job",
        )
        stack.start_engine()
        job = user.wait_for(request_id, timeout_s=15)["latest_job"]
        assert (job["status"], job["attempt"], job["outcome"]) == ("COMPLETED", 1, "SUCCESS")
