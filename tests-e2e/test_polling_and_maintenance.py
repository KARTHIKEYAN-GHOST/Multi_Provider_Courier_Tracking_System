"""Phase 7 end to end: automatic re-polling, status-mapping management, archiving."""

from __future__ import annotations

import json
import time
from itertools import pairwise

from harness import Stack, User, number, wait_until


def test_a_moving_parcel_is_polled_until_it_is_delivered_and_then_left_alone(stack: Stack, user: User) -> None:
    """The demo site shows one more step of a DEMO-PROGRESS parcel on every visit."""
    with stack.provider("demosite", poll_interval_ms=300):
        request_id = user.submit(number("PROGRESS", "DEMO"), "demosite")
        first = user.wait_for(request_id)
        assert first["shipment_status"] in ("CREATED", "PICKED_UP"), "not delivered by the first lookup"

        view = wait_until(
            lambda: (v := user.request(request_id))["shipment_status"] == "DELIVERED" and v,
            30,
            "the parcel to be delivered",
        )
        assert view["next_check_at"] is None, "a delivered shipment is not scheduled again"

        jobs = user.http.get(f"/tracking-requests/{request_id}/jobs").json()["items"]
        triggers = [job["trigger"] for job in reversed(jobs)]
        assert triggers == ["initial", "poll", "poll", "poll", "poll"], "five visits for five steps"
        assert all((job["status"], job["outcome"], job["attempt"]) == ("COMPLETED", "SUCCESS", 1) for job in jobs)
        assert [e["normalized_status"] for e in user.events(request_id)] == [
            "CREATED",
            "PICKED_UP",
            "IN_TRANSIT",
            "OUT_FOR_DELIVERY",
            "DELIVERED",
        ], "each event stored once although every page repeats the earlier ones"

        time.sleep(1.5)  # several poll intervals
        assert len(user.http.get(f"/tracking-requests/{request_id}/jobs").json()["items"]) == 5


def test_polling_backs_off_while_nothing_changes_and_stops_when_nobody_is_subscribed(stack: Stack, user: User) -> None:
    with stack.provider("demosite", poll_interval_ms=200, poll_max_interval_ms=800):
        request_id = user.submit(number("TRANSIT", "DEMO"), "demosite")  # stays in transit forever

        def polls() -> list[dict[str, object]]:
            return stack.query(
                "SELECT j.created_at FROM tracking_jobs j JOIN tracking_requests r ON r.shipment_id = j.shipment_id"
                " WHERE r.id = %s AND j.trigger = 'poll' ORDER BY j.created_at",
                request_id,
            )

        wait_until(lambda: len(polls()) >= 5, 30, "five polls")
        times = [row["created_at"] for row in polls()][:5]
        gaps = [(b - a).total_seconds() for a, b in pairwise(times)]
        assert gaps[0] < gaps[-1], f"the interval grows while nothing changes: {gaps}"
        assert gaps[-1] >= 0.7, f"up to the maximum of 800 ms: {gaps}"
        assert user.request(request_id)["next_check_at"] is not None

        # The only subscriber leaves: polling ends at the next due time.
        assert user.http.delete(f"/tracking-requests/{request_id}").status_code == 204
        time.sleep(1.5)
        settled = len(polls())
        time.sleep(1.5)
        assert len(polls()) == settled


def test_an_operator_maps_an_unknown_status_and_the_history_is_corrected(stack: Stack, user: User) -> None:
    """DUMMY-ODD reports the wording PKG_HELD_AT_CUSTOMS, which has no mapping."""
    request_id = user.submit(number("ODD"))
    user.wait_for(request_id)
    odd = next(e for e in user.events(request_id) if e["normalized_status"] == "UNKNOWN")
    raw_status = odd["raw_status"]

    listed = stack.admin("list-unmapped-statuses", "--provider", "dummy")
    assert raw_status in listed
    assert "events:" in listed

    out = stack.admin("set-status-mapping", "--provider", "dummy", "--raw-status", raw_status, "--status", "exception")
    try:
        assert "mapping stored" in out
        mapped = next(e for e in user.events(request_id) if e["raw_status"] == raw_status)
        assert mapped["normalized_status"] == "EXCEPTION"
        assert raw_status not in stack.admin("list-unmapped-statuses", "--provider", "dummy")
        assert raw_status in stack.admin("list-status-mappings", "--provider", "dummy")
        # A shipment tracked from now on gets the mapping at once.
        later = user.submit(number("ODD"))
        user.wait_for(later)
        assert all(e["normalized_status"] != "UNKNOWN" for e in user.events(later))
    finally:
        # Other tests rely on this wording being unmapped.
        stack.admin("remove-status-mapping", "--provider", "dummy", "--raw-status", raw_status)
    restored = next(e for e in user.events(request_id) if e["raw_status"] == raw_status)
    assert restored["normalized_status"] == "UNKNOWN"
    audited = stack.query(
        "SELECT action FROM audit_log WHERE action LIKE 'status_mapping.%%' AND details ->> 'raw_status' = %s"
        " ORDER BY id",
        raw_status,
    )
    assert [row["action"] for row in audited][-2:] == ["status_mapping.set", "status_mapping.remove"]


def test_the_poll_policy_command_reschedules_open_shipments(stack: Stack, user: User) -> None:
    request_id = user.submit(number("TRANSIT"))
    user.wait_for(request_id)
    assert user.request(request_id)["next_check_at"] is None, "the dummy provider is not polled by default"
    try:
        out = stack.admin("set-poll-policy", "--provider", "dummy", "--interval-ms", "600000")
        assert "open shipments rescheduled" in out
        assert user.request(request_id)["next_check_at"] is not None
    finally:
        stack.admin("set-poll-policy", "--provider", "dummy", "--interval-ms", "0")
    assert user.request(request_id)["next_check_at"] is None


def test_a_long_finished_shipment_is_archived_and_its_number_starts_a_new_shipment(stack: Stack, user: User) -> None:
    tracking_number = number("OK")
    old = user.submit(tracking_number)
    assert user.wait_for(old)["shipment_status"] == "DELIVERED"

    # Delivered 91 days ago (the reuse window is 90 days).
    aged = stack.execute(
        "UPDATE shipments SET terminal_at = now() - interval '91 days' WHERE tracking_number = %s", tracking_number
    )
    assert aged == 1
    report = json.loads(stack.admin("run-maintenance"))
    assert report["shipments_archived"] >= 1
    assert report["shipments_purged"] == 0, "events are kept for the retention period after archiving"

    view = user.request(old)
    assert view["archived_at"] is not None
    assert view["shipment_status"] == "DELIVERED"
    assert len(user.events(old)) == 3, "the history is still readable"
    refused = user.http.post(f"/tracking-requests/{old}/refresh")
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "shipment_archived")

    # The same number again: a new shipment, tracked from scratch.
    new = user.submit(tracking_number)
    assert new != old
    fresh = user.wait_for(new)
    assert (fresh["archived_at"], fresh["shipment_status"]) == (None, "DELIVERED")
    generations = stack.query(
        "SELECT archived_at IS NOT NULL AS archived FROM shipments WHERE tracking_number = %s ORDER BY created_at",
        tracking_number,
    )
    assert [row["archived"] for row in generations] == [True, False]

    # Long after archiving, the old generation's events are deleted; the request remains.
    stack.execute(
        "UPDATE shipments SET archived_at = now() - interval '181 days'"
        " WHERE tracking_number = %s AND archived_at IS NOT NULL",
        tracking_number,
    )
    report = json.loads(stack.admin("run-maintenance"))
    assert (report["shipments_purged"], report["events_deleted"], report["raw_responses_deleted"]) == (1, 3, 1)
    assert user.events(old) == []
    assert user.request(old)["shipment_status"] == "DELIVERED"
    assert len(user.events(new)) == 3, "the new generation is untouched"
