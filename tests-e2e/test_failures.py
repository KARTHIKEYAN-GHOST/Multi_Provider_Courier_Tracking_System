"""Acceptance criteria 27-29: failures reported by the adapter, and retries."""

from __future__ import annotations

from harness import Stack, User, number


def attempts(stack: Stack, job_id: str) -> list[dict[str, object]]:
    return stack.query(
        "SELECT attempt, outcome::text, error_class::text, closed_by::text, dispatched_at, closed_at, execution_count"
        " FROM job_attempts WHERE job_id = %s ORDER BY attempt",
        job_id,
    )


def test_a_transient_failure_is_retried_and_then_succeeds(stack: Stack, user: User) -> None:
    request_id = user.submit(number("FLAKY"))
    view = user.wait_for(request_id)
    job = view["latest_job"]
    assert (job["status"], job["attempt"], job["outcome"]) == ("COMPLETED", 2, "SUCCESS")
    assert view["shipment_status"] == "DELIVERED"
    first, second = attempts(stack, job["id"])
    assert (first["outcome"], first["error_class"], first["closed_by"]) == (
        "FAILURE",
        "NETWORK_TIMEOUT",
        "result",
    )
    assert (second["outcome"], second["error_class"]) == ("SUCCESS", None)
    assert len(stack.query("SELECT 1 FROM raw_tracking_responses WHERE job_id = %s", job["id"])) == 1


def test_a_job_that_always_fails_stops_after_max_attempts_with_growing_delays(stack: Stack, user: User) -> None:
    with stack.provider(retry_base_ms=400):
        job = user.wait_for(user.submit(number("FAIL")))["latest_job"]
        assert (job["status"], job["attempt"], job["last_error_class"]) == ("FAILED", 3, "HTTP_5XX")
        rows = attempts(stack, job["id"])
    assert [r["attempt"] for r in rows] == [1, 2, 3]
    assert {r["error_class"] for r in rows} == {"HTTP_5XX"}
    first_gap = (rows[1]["dispatched_at"] - rows[0]["closed_at"]).total_seconds()  # type: ignore[operator]
    second_gap = (rows[2]["dispatched_at"] - rows[1]["closed_at"]).total_seconds()  # type: ignore[operator]
    assert 0.3 <= first_gap < second_gap, (first_gap, second_gap)


def test_a_permanent_failure_is_not_retried(stack: Stack, user: User) -> None:
    job = user.wait_for(user.submit(number("FATAL")))["latest_job"]
    assert (job["status"], job["attempt"], job["last_error_class"]) == ("FAILED", 1, "BLOCKED")
    assert len(attempts(stack, job["id"])) == 1
