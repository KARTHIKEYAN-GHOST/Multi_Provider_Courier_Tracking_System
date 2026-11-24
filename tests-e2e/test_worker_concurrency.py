"""Acceptance criterion 25: slow jobs do not hold up quick ones."""

from __future__ import annotations

from harness import Stack, User, number


def test_twenty_quick_jobs_complete_while_three_slow_jobs_are_still_running(
    stack: Stack, user: User, quiet: None
) -> None:
    with stack.provider(timeout_ms=6000, retry_base_ms=200, max_attempts=2):
        slow = [user.submit(number("SLOW")) for _ in range(3)]
        quick = [user.submit(number("OK")) for _ in range(20)]

        for request_id in quick:
            assert user.wait_for(request_id, timeout_s=5)["latest_job"]["status"] == "COMPLETED"
        for request_id in slow:
            job = user.job(request_id)
            assert (job["status"], job["attempt"]) == ("PROCESSING", 1), "still inside its first execution"

        for request_id in slow:
            job = user.wait_for(request_id, "FAILED", timeout_s=30)["latest_job"]
            assert (job["attempt"], job["last_error_class"]) == (2, "ADAPTER_TIMEOUT")
