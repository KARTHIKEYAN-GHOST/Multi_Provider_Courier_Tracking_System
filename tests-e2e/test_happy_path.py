"""Acceptance criteria 37-41: lookups that succeed, and how their statuses are normalized."""

from __future__ import annotations

import json

from harness import Stack, User, number


def test_a_delivered_shipment(stack: Stack, user: User) -> None:
    request_id = user.submit(number("OK"))
    view = user.wait_for(request_id, timeout_s=10)

    job = view["latest_job"]
    assert (job["status"], job["outcome"], job["attempt"], job["execution_count"]) == (
        "COMPLETED",
        "SUCCESS",
        1,
        1,
    )
    assert job["last_error_class"] is None
    assert view["shipment_status"] == "DELIVERED"
    events = user.events(request_id)
    assert [(e["raw_status"], e["normalized_status"]) for e in events] == [
        ("PKG_CREATED", "CREATED"),
        ("PKG_IN_TRANSIT", "IN_TRANSIT"),
        ("PKG_DELIVERED", "DELIVERED"),
    ]
    raw = stack.query("SELECT content_type, body FROM raw_tracking_responses WHERE job_id = %s", job["id"])
    assert len(raw) == 1
    assert raw[0]["body"]["history"] == ["PKG_CREATED", "PKG_IN_TRANSIT", "PKG_DELIVERED"]
    (log,) = stack.query("SELECT disposition, worker_id FROM adapter_execution_logs WHERE job_id = %s", job["id"])
    assert (log["disposition"], log["worker_id"]) == ("accepted", "e2e-worker")


def test_a_shipment_in_transit(user: User) -> None:
    view = user.wait_for(user.submit(number("TRANSIT")))
    assert (view["latest_job"]["status"], view["shipment_status"]) == ("COMPLETED", "IN_TRANSIT")


def test_an_unknown_tracking_number_is_an_answer_not_a_failure(user: User) -> None:
    request_id = user.submit(number("NOTFOUND"))
    view = user.wait_for(request_id)
    job = view["latest_job"]
    assert (job["status"], job["outcome"], job["attempt"]) == ("COMPLETED", "NOT_FOUND", 1)
    assert view["shipment_status"] == "UNKNOWN"
    assert user.events(request_id) == []


def test_an_unmapped_provider_status_is_kept_and_normalized_to_unknown(user: User) -> None:
    request_id = user.submit(number("ODD"))
    user.wait_for(request_id)
    statuses = [(e["raw_status"], e["normalized_status"]) for e in user.events(request_id)]
    assert statuses == [("PKG_CREATED", "CREATED"), ("PKG_HELD_AT_CUSTOMS", "UNKNOWN")]


def test_one_correlation_id_links_the_api_the_messages_and_both_logs(stack: Stack, user: User) -> None:
    response = user.track(number("OK"))
    correlation_id = response.headers["x-correlation-id"]
    request_id = response.json()["id"]
    job = user.wait_for(request_id)["latest_job"]

    (row,) = stack.query("SELECT correlation_id::text FROM tracking_jobs WHERE id = %s", job["id"])
    assert row["correlation_id"] == correlation_id
    (outbox,) = stack.query(
        "SELECT payload FROM outbox_messages WHERE payload -> 'payload' ->> 'job_id' = %s", job["id"]
    )
    assert outbox["payload"]["correlation_id"] == correlation_id

    def lines(text: str) -> list[dict[str, object]]:
        parsed = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
        return [line for line in parsed if correlation_id in json.dumps(line)]

    worker_lines = lines(stack.worker.log_text())
    assert {line["message"] for line in worker_lines} >= {"job_started", "job_finished"}
    assert lines(stack.engine.log_text()), "the Engine logged the request under the same ID"
    for secret in (
        user.api_key,
        stack.settings["RABBITMQ_ENGINE_PASSWORD"],
        stack.settings["POSTGRES_PASSWORD"],
    ):
        assert secret not in stack.engine.log_text()
        assert secret not in stack.worker.log_text()
