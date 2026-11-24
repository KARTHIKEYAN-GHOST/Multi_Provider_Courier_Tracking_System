"""Acceptance criteria 17, 18, 21: duplicates and malformed events do no harm."""

from __future__ import annotations

import json

from harness import Stack, User, number, wait_until


def counts(stack: Stack, job_id: str) -> dict[str, int]:
    (row,) = stack.query(
        """SELECT (SELECT count(*) FROM job_attempts WHERE job_id = %(j)s) AS attempts,
                  (SELECT count(*) FROM adapter_execution_logs WHERE job_id = %(j)s) AS executions,
                  (SELECT count(*) FROM raw_tracking_responses WHERE job_id = %(j)s) AS raw_responses,
                  (SELECT count(*) FROM tracking_events e JOIN tracking_jobs j ON j.shipment_id = e.shipment_id
                    WHERE j.id = %(j)s) AS events""".replace("%(j)s", "%s"),
        job_id,
        job_id,
        job_id,
        job_id,
    )
    return {k: int(v) for k, v in row.items()}


def test_the_identical_result_delivered_twice_changes_nothing_the_second_time(stack: Stack, user: User) -> None:
    job = user.wait_for(user.submit(number("DUP")))["latest_job"]
    assert job["status"] == "COMPLETED"
    # Both copies have been consumed once the result's message_id is in the Engine's inbox.
    wait_until(lambda: not stack.take("engine.events", 1), 5, "the event queue to drain")
    assert counts(stack, job["id"]) == {"attempts": 1, "executions": 1, "raw_responses": 1, "events": 3}
    assert job["execution_count"] == 1


def test_the_same_job_message_executed_twice_is_one_attempt_with_two_executions(stack: Stack, user: User) -> None:
    job = user.wait_for(user.submit(number("OK")))["latest_job"]
    (outbox,) = stack.query(
        "SELECT payload FROM outbox_messages WHERE payload -> 'payload' ->> 'job_id' = %s", job["id"]
    )
    # A second copy of the very same job.track message, as a relay crash would produce.
    stack.publish("courier.jobs", "job.track.v1.dummy", json.dumps(outbox["payload"]))

    logs = wait_until(
        lambda: (
            (
                rows := stack.query(
                    "SELECT disposition::text FROM adapter_execution_logs WHERE job_id = %s ORDER BY created_at",
                    job["id"],
                )
            )
            and len(rows) == 2
            and "incomplete" not in {r["disposition"] for r in rows}
            and rows
        ),
        10,
        "the second execution to be recorded",
    )
    assert sorted(r["disposition"] for r in logs) == ["accepted", "duplicate"]
    assert counts(stack, job["id"]) == {"attempts": 1, "executions": 2, "raw_responses": 1, "events": 3}
    (attempt,) = stack.query("SELECT execution_count FROM job_attempts WHERE job_id = %s", job["id"])
    assert attempt["execution_count"] == 2


def test_malformed_events_are_parked_and_do_not_block_later_ones(stack: Stack, user: User) -> None:
    stack.take("engine.events.dlq", 100)
    stack.publish("courier.events", "job.result.v1", "{not json")
    stack.publish(
        "courier.events",
        "job.result.v1",
        json.dumps({"message_id": "x", "type": "job.result", "schema_version": "2.0", "payload": {}}),
    )
    view = user.wait_for(user.submit(number("OK")))
    assert view["latest_job"]["status"] == "COMPLETED", "later messages are still processed"
    parked = wait_until(
        lambda: (found := stack.take("engine.events.dlq", 10)) and found,
        10,
        "the rejected events to be parked",
    )
    parked += stack.take("engine.events.dlq", 10)
    assert len(parked) == 2
    assert {m["payload"] for m in parked} >= {"{not json"}
