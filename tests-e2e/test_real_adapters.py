"""Phase 6 end to end: a scraping adapter, an official-API adapter with a stored credential,
the dummy-provider switch and provider rate limiting."""

from __future__ import annotations

import json
from collections.abc import Iterator
from itertools import pairwise

import httpx
import pytest

from fake_dhl import FakeDhl
from harness import Stack, User, number, wait_until

#: DHL's own login in use (other tests store logins for other couriers).
DHL_LOGIN = (
    "SELECT c.id::text FROM provider_credentials c JOIN providers p ON p.id = c.provider_id "
    "WHERE c.revoked_at IS NULL AND p.code = 'dhl'"
)

DHL_KEY = "e2e-dhl-api-key-0123456789-DO-NOT-LEAK"


# ----------------------------------------------------------------------------- scraping adapter


def test_a_page_of_the_demo_courier_website_is_scraped_and_normalized(stack: Stack, user: User) -> None:
    request_id = user.submit(number("OK", "DEMO"), "demosite")
    view = user.wait_for(request_id)
    job = view["latest_job"]
    assert (job["status"], job["outcome"], job["attempt"]) == ("COMPLETED", "SUCCESS", 1)
    assert view["shipment_status"] == "DELIVERED"
    assert [(e["raw_status"], e["normalized_status"]) for e in user.events(request_id)] == [
        ("Booked", "CREATED"),
        ("Picked up", "PICKED_UP"),
        ("In transit", "IN_TRANSIT"),
        ("Out for delivery", "OUT_FOR_DELIVERY"),
        ("Delivered", "DELIVERED"),
    ]
    (raw,) = stack.query("SELECT content_type, body FROM raw_tracking_responses WHERE job_id = %s", job["id"])
    assert raw["content_type"] == "text/html"
    assert '<table id="history">' in raw["body"], "the page is kept exactly as received"


def test_scraping_outcomes_are_classified(stack: Stack, user: User) -> None:
    with stack.provider("demosite", retry_base_ms=200, max_attempts=2):
        requests = {
            kind: user.submit(number(kind, "DEMO"), "demosite")
            for kind in ("TRANSIT", "NOTFOUND", "NEWLAYOUT", "SCRAMBLED", "BADDATE", "BLOCKED", "BUSY", "LIMIT")
        }
        jobs = {kind: user.wait_for(request_id, timeout_s=30) for kind, request_id in requests.items()}

    def summary(kind: str) -> tuple[str, str | None, int]:
        job = jobs[kind]["latest_job"]
        return job["status"], job["last_error_class"] or job["outcome"], job["attempt"]

    assert summary("TRANSIT") == ("COMPLETED", "SUCCESS", 1)
    assert jobs["TRANSIT"]["shipment_status"] == "IN_TRANSIT"
    assert summary("NOTFOUND") == ("COMPLETED", "NOT_FOUND", 1)
    # The redesigned page was learned by an AI repair (demosite adapter 0.2.1).
    assert summary("NEWLAYOUT") == ("COMPLETED", "SUCCESS", 1)
    # A page no adapter version knows, or an unreadable value, will not fix itself: no retry.
    assert summary("SCRAMBLED") == ("FAILED", "SCHEMA_DRIFT", 1)
    assert summary("BADDATE") == ("FAILED", "PARSE_ERROR", 1)
    assert summary("BLOCKED") == ("FAILED", "BLOCKED", 1)
    # An overloaded or rate-limiting site may recover: retried up to the limit.
    assert summary("BUSY") == ("FAILED", "HTTP_5XX", 2)
    assert summary("LIMIT") == ("FAILED", "RATE_LIMITED", 2)


def test_the_site_asks_for_a_pause_and_the_engine_waits_that_long(stack: Stack, user: User) -> None:
    with stack.provider("demosite", retry_base_ms=100, max_attempts=2):
        job = user.wait_for(user.submit(number("LIMIT", "DEMO"), "demosite"), timeout_s=30)["latest_job"]
    first, second = stack.query(
        "SELECT dispatched_at, closed_at FROM job_attempts WHERE job_id = %s ORDER BY attempt", job["id"]
    )
    waited = (second["dispatched_at"] - first["closed_at"]).total_seconds()
    assert waited >= 1.9, "Retry-After: 2 from the site outweighs the 100 ms back-off"


def test_a_site_that_is_down_is_a_retryable_network_error(stack: Stack, user: User) -> None:
    with stack.provider("demosite", retry_base_ms=200, max_attempts=2):
        stack.site.stop()
        try:
            job = user.wait_for(user.submit(number("OK", "DEMO"), "demosite"), timeout_s=30)["latest_job"]
        finally:
            stack.start_site()
    assert (job["status"], job["last_error_class"], job["attempt"]) == ("FAILED", "NETWORK_CONNECT", 2)


def test_scraping_is_refused_once_its_authorization_is_withdrawn(stack: Stack, user: User) -> None:
    stack.admin("set-scraping-authorized", "--provider", "demosite", "--authorized", "false")
    try:
        refused = user.track(number("OK", "DEMO"), "demosite")
        assert (refused.status_code, refused.json()["error"]["code"]) == (409, "provider_unavailable")
    finally:
        stack.admin("set-scraping-authorized", "--provider", "demosite", "--authorized", "true")
    assert user.track(number("OK", "DEMO"), "demosite").status_code == 201
    actions = stack.query("SELECT details FROM audit_log WHERE action = 'provider.scraping_authorized' ORDER BY id")
    assert [a["details"]["authorized"] for a in actions[-2:]] == [False, True]


# ----------------------------------------------------------------------------- official API + credential


@pytest.fixture
def dhl(stack: Stack) -> Iterator[FakeDhl]:
    """A fake DHL API, the stored DHL credential, and a DHL worker with a fresh worker token."""
    fake = FakeDhl(DHL_KEY)
    stack.admin("set-credential", "--provider", "dhl", "--label", "e2e", stdin=json.dumps({"api_key": DHL_KEY}))
    token = (
        stack.admin("create-worker-token", "--adapter", "dhl", "--name", "e2e").split("worker_token: ")[1].split()[0]
    )
    stack.start_worker(adapter="dhl", DHL_BASE_URL=fake.url, WORKER_TOKEN=token)
    try:
        yield fake
    finally:
        stack.stop_worker("dhl")
        if stack.query(
            "SELECT 1 FROM provider_credentials c JOIN providers p ON p.id = c.provider_id"
            " WHERE p.code = 'dhl' AND c.revoked_at IS NULL"
        ):
            stack.admin("revoke-credential", "--provider", "dhl")
        fake.close()


def everything_stored_or_sent(stack: Stack) -> str:
    """All text a secret could have leaked into: database, logs and queues."""
    tables = [
        "outbox_messages",
        "raw_tracking_responses",
        "audit_log",
        "adapter_execution_logs",
        "job_attempts",
        "tracking_jobs",
        "tracking_events",
        "dead_letters",
        "provider_credentials",
        "worker_tokens",
    ]
    database = "".join(str(stack.query(f"SELECT t::text AS row FROM {table} t")) for table in tables)
    return database + "".join(stack.log_files().values())


def test_dhl_is_refused_while_no_credential_is_stored(stack: Stack, user: User) -> None:
    refused = user.track("DHLDELIVERED0001", "dhl")
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "provider_unavailable")


def test_a_dhl_shipment_is_tracked_with_the_stored_credential_and_the_key_never_leaks(
    stack: Stack, user: User, dhl: FakeDhl
) -> None:
    tracking_number = number("DELIVERED", "DHL").replace("-", "")
    request_id = user.submit(tracking_number, "dhl")
    view = user.wait_for(request_id)
    job = view["latest_job"]
    assert (job["status"], job["outcome"], job["attempt"]) == ("COMPLETED", "SUCCESS", 1)
    assert view["shipment_status"] == "DELIVERED"
    assert [(e["raw_status"], e["normalized_status"]) for e in user.events(request_id)] == [
        ("pre-transit", "CREATED"),
        ("transit", "IN_TRANSIT"),
        ("delivered", "DELIVERED"),
    ]

    # DHL was called once, with the real key, obtained through the Engine's internal API.
    assert [(n, key) for _, n, key in dhl.requests] == [(tracking_number, DHL_KEY)]
    (resolve,) = stack.query("SELECT outcome, actor_type, attempt FROM audit_log WHERE job_id = %s", job["id"])
    assert (resolve["outcome"], resolve["actor_type"], resolve["attempt"]) == ("allowed", "worker", 1)

    # The message carried a reference only; the key is nowhere in the database, logs or queues.
    (outbox,) = stack.query(
        "SELECT payload FROM outbox_messages WHERE payload -> 'payload' ->> 'job_id' = %s", job["id"]
    )
    (credential,) = stack.query(DHL_LOGIN)
    assert outbox["payload"]["payload"]["credential_ref"] == credential["id"]
    haystack = everything_stored_or_sent(stack)
    assert DHL_KEY not in haystack
    assert stack.master_key not in haystack
    token = stack.workers["dhl"].env["WORKER_TOKEN"]
    assert token not in haystack


def test_an_unknown_dhl_number_is_not_found(stack: Stack, user: User, dhl: FakeDhl) -> None:
    job = user.wait_for(user.submit(number("NOSUCH", "DHL").replace("-", ""), "dhl"))["latest_job"]
    assert (job["status"], job["outcome"]) == ("COMPLETED", "NOT_FOUND")


def test_a_key_dhl_rejects_fails_the_job_at_once_as_auth_invalid(stack: Stack, user: User, dhl: FakeDhl) -> None:
    stack.admin("set-credential", "--provider", "dhl", "--label", "wrong", stdin=json.dumps({"api_key": "wrong-key"}))
    job = user.wait_for(user.submit(number("DELIVERED", "DHL").replace("-", ""), "dhl"))["latest_job"]
    assert (job["status"], job["last_error_class"], job["attempt"]) == ("FAILED", "AUTH_INVALID", 1)
    assert dhl.requests[-1][2] == "wrong-key", "the replaced credential was the one in use"


def test_a_worker_with_a_revoked_token_gets_no_credential(stack: Stack, user: User, dhl: FakeDhl) -> None:
    prefix = stack.workers["dhl"].env["WORKER_TOKEN"].split("_")[1]
    stack.admin("revoke-worker-token", "--prefix", prefix)
    job = user.wait_for(user.submit(number("DELIVERED", "DHL").replace("-", ""), "dhl"))["latest_job"]
    assert (job["status"], job["last_error_class"], job["attempt"]) == ("FAILED", "AUTH_INVALID", 1)
    assert dhl.requests == [], "DHL was never called"


def test_the_internal_api_refuses_everyone_but_the_right_worker(stack: Stack, user: User, dhl: FakeDhl) -> None:
    with stack.provider("dhl", min_dispatch_interval_ms=0):
        stack.stop_worker("dhl")  # keep the job in flight
        request_id = user.submit(number("DELIVERED", "DHL").replace("-", ""), "dhl")
        job_id = user.job(request_id)["id"]
        (credential,) = stack.query(DHL_LOGIN)
        body = {"credential_ref": credential["id"], "job_id": job_id, "attempt": 1}
        url = f"{stack.internal_url}/internal/v1/credentials/resolve"
        dhl_token = stack.workers["dhl"].env["WORKER_TOKEN"]
        other = stack.admin("create-worker-token", "--adapter", "demosite").split("worker_token: ")[1].split()[0]

        def post(token: str | None, **changes: object) -> httpx.Response:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            return httpx.post(url, json={**body, **changes}, headers=headers, timeout=5)

        assert post(None).status_code == 401
        assert post(user.api_key).status_code == 401, "a user API key opens nothing here"
        assert post(other).status_code == 403, "a worker of another adapter"
        assert post(dhl_token, attempt=2).status_code == 403
        allowed = post(dhl_token)
        assert (allowed.status_code, allowed.json()["secret"]) == (200, {"api_key": DHL_KEY})
        assert allowed.headers["cache-control"] == "no-store"
        # The public API does not have this route at all.
        public = httpx.post(
            f"{stack.base_url}/internal/v1/credentials/resolve",
            json=body,
            headers={"Authorization": f"Bearer {dhl_token}"},
            timeout=5,
        )
        assert public.status_code == 404

        rows = stack.query("SELECT outcome, reason FROM audit_log WHERE job_id = %s ORDER BY id", job_id)
        assert [(r["outcome"], r["reason"]) for r in rows] == [
            ("denied", "job_belongs_to_another_adapter"),
            ("denied", "attempt_not_in_flight"),
            ("allowed", None),
        ]
        stack.start_worker(adapter="dhl")
        user.wait_for(request_id)


# ----------------------------------------------------------------------------- rate limiting


def test_calls_to_a_rate_limited_provider_are_spaced_out(stack: Stack, user: User, dhl: FakeDhl) -> None:
    with stack.provider("dhl", min_dispatch_interval_ms=700, queue_wait_max_ms=20_000):
        requests = [user.submit(number("TRANSIT", "DHL").replace("-", ""), "dhl") for _ in range(4)]
        for request_id in requests:
            job = user.wait_for(request_id, timeout_s=20)["latest_job"]
            assert (job["status"], job["attempt"], job["last_error_class"]) == ("COMPLETED", 1, None)
    times = sorted(at for at, _, _ in dhl.requests)
    gaps = [later - earlier for earlier, later in pairwise(times)]
    assert len(times) == 4
    assert all(gap >= 0.6 for gap in gaps), gaps  # 700 ms slots, minus scheduling tolerance


# ----------------------------------------------------------------------------- dummy-provider switch


def test_with_the_switch_off_the_test_providers_are_not_offered(stack: Stack, user: User) -> None:
    def offered() -> list[str]:
        return sorted(p["code"] for p in user.http.get("/providers").json()["items"])

    # Real couriers stay listed whatever the switch says. (The list grows with new adapters.)
    real = [row["code"] for row in stack.query("SELECT code FROM providers WHERE NOT is_test ORDER BY code")]
    assert "dhl" in real

    assert offered() == sorted([*real, "demosite", "dummy"])
    stack.engine.stop()
    stack.engine.start(ENABLE_DUMMY_PROVIDER="false")
    wait_until(stack._ready, 30, "the Engine to become ready")
    try:
        assert offered() == real
        assert user.track(number("OK")).status_code == 404
    finally:
        stack.engine.stop()
        stack.engine.start(ENABLE_DUMMY_PROVIDER="true")
        wait_until(stack._ready, 30, "the Engine to become ready")
    assert offered() == sorted([*real, "demosite", "dummy"])
    assert user.wait_for(user.submit(number("OK")))["latest_job"]["status"] == "COMPLETED"
