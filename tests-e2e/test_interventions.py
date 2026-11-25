"""Phase 8 end to end: a lookup that needs a person.

The demo courier website puts some shipments behind a one-time code (``DEMO-OTP-``) or a
CAPTCHA (``DEMO-CAPTCHA-``). The worker stops, the Engine asks the user, the user answers
through the API (or the human interface), and the same attempt continues.

These tests need a Redis for the workers' sessions (``make up-deps``); without one they are
skipped.
"""

from __future__ import annotations

import base64
import hashlib
import re

import httpx
import pytest

from harness import Stack, User, number, wait_until

OTP, CAPTCHA = "WAITING_FOR_OTP", "WAITING_FOR_CAPTCHA"


@pytest.fixture(autouse=True)
def _needs_redis(stack: Stack) -> None:
    if stack.redis_url is None:
        pytest.skip("no Redis reachable: start it with `make up-deps`")


def site_answer(purpose: str, tracking_number: str, digits: int) -> str:
    """What the demo site expects (same rule as demo_site/challenges.py)."""
    value = int.from_bytes(hashlib.sha256(f"{purpose}:{tracking_number.upper()}".encode()).digest()[:8])
    return str(value % 10**digits).zfill(digits)


def otp(tracking_number: str) -> str:
    return site_answer("otp", tracking_number, 6)


def wrong(answer: str) -> str:
    return str((int(answer) + 1) % 10 ** len(answer)).zfill(len(answer))


def pending(user: User) -> list[dict]:
    return list(user.http.get("/interventions").json()["items"])


def challenge_for(user: User, request_id: str) -> dict:
    """The open challenge that belongs to a tracking request."""

    def probe() -> dict | None:
        return next((i for i in pending(user) if i["tracking_request_id"] == request_id), None)

    return wait_until(probe, 20, f"a challenge for {request_id}")


def answer(user: User, intervention_id: str, response: str) -> httpx.Response:
    return user.http.post(f"/interventions/{intervention_id}/response", json={"response": response})


def interventions(stack: Stack, job_id: str) -> list[dict]:
    return stack.query(
        "SELECT sequence, kind, status, attempt, ciphertext IS NULL AS wiped, image IS NULL AS no_image "
        "FROM human_interventions WHERE job_id = %s ORDER BY sequence",
        job_id,
    )


def test_a_one_time_code_is_asked_for_answered_and_the_same_attempt_continues(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    waiting = user.wait_for(request_id, OTP)["latest_job"]
    assert waiting["attempt"] == 1

    challenge = challenge_for(user, request_id)
    assert challenge["kind"] == "otp"
    assert challenge["status"] == "pending"
    assert challenge["has_image"] is False
    assert challenge["response_format"] == {"min_length": 6, "max_length": 6, "charset": "digits"}
    assert (challenge["provider_code"], challenge["tracking_number"]) == ("demosite", tracking_number)
    assert len(stack.session_keys()) >= 1, "the worker parked its session in Redis"

    code = otp(tracking_number)
    assert answer(user, challenge["id"], code).status_code == 202

    view = user.wait_for(request_id)
    job = view["latest_job"]
    assert (job["status"], job["outcome"], job["attempt"]) == ("COMPLETED", "SUCCESS", 1)
    assert view["shipment_status"] == "IN_TRANSIT"
    assert [e["raw_status"] for e in user.events(request_id)] == ["Booked", "Picked up", "In transit"]

    # One logical attempt, two physical executions: the one that asked, the one that continued.
    (attempt,) = stack.query("SELECT execution_count FROM job_attempts WHERE job_id = %s", job["id"])
    assert attempt["execution_count"] == 2
    dispositions = stack.query(
        "SELECT disposition::text AS d FROM adapter_execution_logs WHERE job_id = %s ORDER BY started_at", job["id"]
    )
    assert [row["d"] for row in dispositions] == ["suspended", "accepted"]

    # The challenge is closed and the answer is gone from the database.
    assert interventions(stack, job["id"]) == [
        {"sequence": 1, "kind": "otp", "status": "completed", "attempt": 1, "wiped": True, "no_image": True}
    ]
    assert pending(user) == []
    # The answer travelled in no message and no log line.
    messages = stack.query(
        "SELECT payload::text AS body FROM outbox_messages WHERE payload::text LIKE %s", f"%{job['id']}%"
    )
    assert {m["body"].count('"job.resume"') for m in messages} == {0, 1}
    assert all(code not in m["body"] for m in messages)
    for name, text in stack.log_files().items():
        assert code not in text, f"the answer appears in {name}"
    # The worker fetched it over the internal API, and that was recorded.
    audit = stack.query(
        "SELECT action, outcome FROM audit_log WHERE action = 'intervention.resolve' ORDER BY id DESC LIMIT 1"
    )
    assert audit, "resolving an answer is audited"


def test_the_session_in_redis_is_ciphertext_and_removed_after_use(stack: Stack, user: User) -> None:
    wait_until(lambda: not stack.session_keys(), 20, "earlier sessions to be used up")
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(user, request_id)
    (key,) = stack.session_keys()
    assert key.startswith(f"{stack.session_prefix}demosite:")
    (stored,) = stack.session_values()
    assert b"token" not in stored, "the session is encrypted before it leaves the worker"
    assert answer(user, challenge["id"], otp(tracking_number)).status_code == 202
    user.wait_for(request_id)
    wait_until(lambda: not stack.session_keys(), 10, "the used session to be removed")


def test_a_captcha_is_shown_to_the_user_as_a_picture_and_their_answer_continues_the_lookup(
    stack: Stack, user: User
) -> None:
    tracking_number = number("CAPTCHA", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    user.wait_for(request_id, CAPTCHA)
    challenge = challenge_for(user, request_id)
    assert (challenge["kind"], challenge["has_image"]) == ("captcha", True)

    detail = user.http.get(f"/interventions/{challenge['id']}").json()
    assert detail["image"]["content_type"] == "image/png"
    assert base64.b64decode(detail["image"]["data"]).startswith(b"\x89PNG\r\n\x1a\n")

    # The person reads the picture. (The test knows the demo site's rule instead.)
    assert answer(user, challenge["id"], site_answer("captcha", tracking_number, 5)).status_code == 202
    job = user.wait_for(request_id)["latest_job"]
    assert (job["status"], job["outcome"], job["attempt"]) == ("COMPLETED", "SUCCESS", 1)
    (row,) = interventions(stack, job["id"])
    assert (row["status"], row["no_image"]) == ("completed", True), "the picture is deleted with the answer"


def test_an_answer_of_the_wrong_form_is_refused_at_once_and_the_challenge_stays_open(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(user, request_id)
    for malformed in ("12345", "1234567", "12345a", ""):
        assert answer(user, challenge["id"], malformed).status_code == 422
    assert user.job(request_id)["status"] == OTP
    assert answer(user, challenge["id"], otp(tracking_number)).status_code == 202
    assert answer(user, challenge["id"], otp(tracking_number)).status_code == 409, "an answer is given once"
    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_a_wrong_code_leads_to_a_second_challenge_and_the_right_code_then_completes(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    first = challenge_for(user, request_id)
    assert answer(user, first["id"], wrong(otp(tracking_number))).status_code == 202

    def second_challenge() -> dict | None:
        found = [i for i in pending(user) if i["tracking_request_id"] == request_id and i["id"] != first["id"]]
        return found[0] if found else None

    second = wait_until(second_challenge, 20, "the site to ask again")
    assert second["prompt"].startswith("The previous answer was not accepted.")
    assert answer(user, second["id"], otp(tracking_number)).status_code == 202

    job = user.wait_for(request_id)["latest_job"]
    assert (job["status"], job["attempt"]) == ("COMPLETED", 1)
    assert [(i["sequence"], i["status"]) for i in interventions(stack, job["id"])] == [
        (1, "rejected"),
        (2, "completed"),
    ]


def test_a_job_stops_asking_once_the_providers_challenge_limit_is_reached(stack: Stack, user: User) -> None:
    stack.execute("UPDATE providers SET max_challenges = 2 WHERE code = 'demosite'")
    try:
        tracking_number = number("OTP", "DEMO")
        request_id = user.submit(tracking_number, "demosite")
        seen: set[str] = set()
        for _ in range(2):
            challenge = wait_until(
                lambda: next(
                    (i for i in pending(user) if i["tracking_request_id"] == request_id and i["id"] not in seen), None
                ),
                20,
                "a challenge",
            )
            seen.add(challenge["id"])
            assert answer(user, challenge["id"], wrong(otp(tracking_number))).status_code == 202
        job = user.wait_for(request_id)["latest_job"]
    finally:
        stack.execute("UPDATE providers SET max_challenges = 3 WHERE code = 'demosite'")
    assert (job["status"], job["last_error_class"], job["attempt"]) == ("FAILED", "CHALLENGE_LIMIT_EXCEEDED", 1)
    assert [i["status"] for i in interventions(stack, job["id"])] == ["rejected", "rejected"]
    assert pending(user) == []


def test_a_challenge_nobody_answers_expires_and_can_no_longer_be_answered(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(user, request_id)
    job_id = user.job(request_id)["id"]
    # Move time: the deadline passed a moment ago.
    stack.execute(
        "UPDATE tracking_jobs SET intervention_deadline_at = now() - interval '1 second' WHERE id = %s", job_id
    )
    stack.execute("UPDATE human_interventions SET expires_at = now() - interval '1 second' WHERE job_id = %s", job_id)

    job = user.wait_for(request_id, "EXPIRED")["latest_job"]
    assert job["last_error_class"] == "INTERVENTION_EXPIRED"
    assert answer(user, challenge["id"], otp(tracking_number)).status_code == 410
    assert pending(user) == []
    assert [i["status"] for i in interventions(stack, job_id)] == ["expired"]
    # The shipment can be looked up again: that starts a fresh job and a fresh challenge.
    assert user.http.post(f"/tracking-requests/{request_id}/refresh").status_code == 202
    again = challenge_for(user, request_id)
    assert again["id"] != challenge["id"]
    assert answer(user, again["id"], otp(tracking_number)).status_code == 202
    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_a_lost_session_restarts_the_lookup_and_asks_again(stack: Stack, user: User) -> None:
    with stack.provider("demosite", retry_base_ms=100, max_attempts=3):
        tracking_number = number("OTP", "DEMO")
        request_id = user.submit(tracking_number, "demosite")
        first = challenge_for(user, request_id)
        assert stack.drop_sessions() >= 1  # Redis was restarted, or the session timed out
        assert answer(user, first["id"], otp(tracking_number)).status_code == 202

        def asked_again() -> dict | None:
            found = [i for i in pending(user) if i["tracking_request_id"] == request_id and i["id"] != first["id"]]
            return found[0] if found else None

        second = wait_until(asked_again, 30, "the lookup to restart and ask again")
        job = user.job(request_id)
        assert (job["status"], job["attempt"]) == (OTP, 2)
        errors = stack.query(
            "SELECT error_class::text AS c FROM job_attempts WHERE job_id = %s ORDER BY attempt", job["id"]
        )
        assert errors[0]["c"] == "SESSION_LOST"
        assert answer(user, second["id"], otp(tracking_number)).status_code == 202
        assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_a_challenge_belongs_to_the_user_who_asked_and_nobody_else(stack: Stack, user: User) -> None:
    other = stack.new_user()
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(user, request_id)

    assert pending(other) == []
    assert other.http.get(f"/interventions/{challenge['id']}").status_code == 404
    assert answer(other, challenge["id"], otp(tracking_number)).status_code == 404
    assert httpx.get(f"{stack.base_url}/v1/interventions").status_code == 401
    assert user.job(request_id)["status"] == OTP, "the stranger's attempt changed nothing"

    assert answer(user, challenge["id"], otp(tracking_number)).status_code == 202
    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_only_the_worker_of_the_jobs_adapter_can_fetch_an_answer(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(user, request_id)
    job = user.job(request_id)
    body = {"intervention_id": challenge["id"], "job_id": job["id"], "attempt": job["attempt"]}
    url = f"{stack.internal_url}/internal/v1/interventions/resolve"
    foreign = stack.admin("create-worker-token", "--adapter", "dummy").split("worker_token: ")[1].split()[0]

    assert httpx.post(url, json=body).status_code == 401
    assert httpx.post(url, json=body, headers={"Authorization": f"Bearer {user.api_key}"}).status_code == 401
    refused = httpx.post(url, json=body, headers={"Authorization": f"Bearer {foreign}"})
    assert refused.status_code in (403, 404), "a worker of another adapter gets nothing"
    assert httpx.post(f"{stack.base_url}/internal/v1/interventions/resolve", json=body).status_code == 404, (
        "the internal API is not part of the public listener"
    )

    assert answer(user, challenge["id"], otp(tracking_number)).status_code == 202
    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_the_human_interface_lets_a_user_sign_in_and_answer(stack: Stack, user: User) -> None:
    stack.start_interface()
    tracking_number = number("CAPTCHA", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(user, request_id)

    with httpx.Client(base_url=stack.interface_url, follow_redirects=True, timeout=15) as browser:
        assert "Sign in" in browser.get("/").text
        assert browser.post("/login", data={"api_key": "ctk_not_a_real_key"}).status_code == 401
        listing = browser.post("/login", data={"api_key": user.api_key})
        assert f'href="/interventions/{challenge["id"]}"' in listing.text
        assert user.api_key not in "".join(f"{c.name}={c.value}" for c in browser.cookies.jar)

        page = browser.get(f"/interventions/{challenge['id']}")
        assert 'src="data:image/png;base64,' in page.text
        assert "Type the 5 digits shown in the picture." in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert token
        sent = browser.post(
            f"/interventions/{challenge['id']}",
            data={"answer": site_answer("captcha", tracking_number, 5), "csrf": token.group(1)},
        )
        assert "Your answer was sent" in sent.text

    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"
    assert user.api_key not in stack.log_files()["human-interface.log"]
