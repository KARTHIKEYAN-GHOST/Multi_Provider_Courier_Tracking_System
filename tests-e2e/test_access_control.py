"""Acceptance criteria 6-8 over the real HTTP API."""

from __future__ import annotations

import httpx

from harness import Stack, User, number


def test_requests_without_a_valid_key_are_refused(stack: Stack, user: User) -> None:
    url = f"{stack.base_url}/v1/tracking-requests"
    assert httpx.get(url).status_code == 401
    assert httpx.get(url, headers={"Authorization": "Bearer ctk_000000000000_wrong"}).status_code == 401
    assert httpx.get(url, headers={"Authorization": user.api_key}).status_code == 401
    assert user.http.get("/tracking-requests").status_code == 200
    assert httpx.get(f"{stack.base_url}/healthz").status_code == 200


def test_a_user_cannot_see_or_touch_another_users_request(stack: Stack, user: User) -> None:
    # Another organization's user, a colleague in the same organization, and an admin.
    others = [stack.new_user(organization="E2E other organization"), stack.new_user(), stack.answerer()]
    request_id = user.submit(number("OK"))
    for other in others:
        for path in ("", "/events", "/jobs"):
            assert other.http.get(f"/tracking-requests/{request_id}{path}").status_code == 404
        assert other.http.post(f"/tracking-requests/{request_id}/refresh").status_code == 404
        assert other.http.delete(f"/tracking-requests/{request_id}").status_code == 404
        assert request_id not in [r["id"] for r in other.http.get("/tracking-requests").json()["items"]]
    assert user.request(request_id)["id"] == request_id


def test_two_subscribers_share_the_shipment_and_unsubscribing_affects_only_one(stack: Stack, user: User) -> None:
    other = stack.new_user()
    tracking_number = number("OK")
    mine, theirs = user.submit(tracking_number), other.submit(tracking_number)
    assert mine != theirs
    user.wait_for(mine)
    other.wait_for(theirs)

    def essence(events: list[dict[str, object]]) -> list[tuple[object, object]]:
        return [(e["raw_status"], e["occurred_at"]) for e in events]

    assert essence(user.events(mine)) == essence(other.events(theirs))
    assert len(user.events(mine)) == 3
    assert len(stack.query("SELECT 1 FROM shipments WHERE tracking_number = %s", tracking_number)) == 1

    assert user.http.delete(f"/tracking-requests/{mine}").status_code == 204
    assert user.http.get(f"/tracking-requests/{mine}").status_code == 404
    assert len(other.events(theirs)) == 3
