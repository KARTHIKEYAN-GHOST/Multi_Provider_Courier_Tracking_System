"""Acceptance criteria 11-13 over HTTP: simultaneous requests never create duplicates."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx

from harness import Stack, User, number


def together[T](calls: list) -> list[httpx.Response]:  # type: ignore[type-arg]
    """Runs the calls at the same moment."""
    barrier = Barrier(len(calls))

    def run(call):  # type: ignore[no-untyped-def]
        barrier.wait()
        return call()

    with ThreadPoolExecutor(len(calls)) as pool:
        return list(pool.map(run, calls))


def rows(stack: Stack, tracking_number: str) -> dict[str, int]:
    (row,) = stack.query(
        """SELECT count(DISTINCT s.id) AS shipments,
                  (SELECT count(*) FROM tracking_requests r JOIN shipments x ON x.id = r.shipment_id
                    WHERE x.tracking_number = %s AND r.status = 'active') AS subscriptions,
                  (SELECT count(*) FROM tracking_jobs j JOIN shipments x ON x.id = j.shipment_id
                    WHERE x.tracking_number = %s) AS jobs,
                  (SELECT count(*) FROM outbox_messages o WHERE o.payload -> 'payload' ->> 'tracking_number' = %s)
                    AS outbox
           FROM shipments s WHERE s.tracking_number = %s""",
        tracking_number,
        tracking_number,
        tracking_number,
        tracking_number,
    )
    return {k: int(v) for k, v in row.items()}


def test_twenty_identical_requests_from_one_user(stack: Stack, user: User) -> None:
    tracking_number = number("DELAY-1500")  # still running while all requests arrive
    responses = together([lambda: user.track(tracking_number)] * 20)
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * 19 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert rows(stack, tracking_number) == {"shipments": 1, "subscriptions": 1, "jobs": 1, "outbox": 1}
    user.wait_for(responses[0].json()["id"])


def test_ten_users_requesting_the_same_shipment_twice_each(stack: Stack) -> None:
    # Users of three organizations: they share one lookup, each sees only their own request.
    users = [stack.new_user(organization=f"E2E shared organization {n % 3}") for n in range(10)]
    tracking_number = number("DELAY-1500")
    responses = together([(lambda u=u: u.track(tracking_number)) for u in users for _ in range(2)])
    assert all(r.status_code in (200, 201) for r in responses)
    assert sum(r.status_code == 201 for r in responses) == 10
    assert rows(stack, tracking_number) == {"shipments": 1, "subscriptions": 10, "jobs": 1, "outbox": 1}
    users[0].wait_for(responses[0].json()["id"])


def test_twenty_simultaneous_refreshes_create_one_job(stack: Stack, user: User) -> None:
    tracking_number = number("DELAY-1500")  # the refresh job is still running while all calls arrive
    request_id = user.submit(tracking_number)
    user.wait_for(request_id)
    responses = together([lambda: user.http.post(f"/tracking-requests/{request_id}/refresh")] * 20)
    assert sorted(r.status_code for r in responses) == [200] * 19 + [202]
    assert len({r.json()["job"]["id"] for r in responses}) == 1, "every caller is pointed at the same job"
    assert rows(stack, tracking_number)["jobs"] == 2
    user.wait_for(request_id)
