"""Phase 8 end to end: a lookup that needs a person.

The demo courier website puts some shipments behind a one-time code (``DEMO-OTP-``) or a
CAPTCHA (``DEMO-CAPTCHA-``). The worker stops, the Engine opens a challenge, an admin answers
it through the API (or the human interface), and the same attempt continues. Organization
users track shipments but never see challenges.

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


def pending(stack: Stack) -> list[dict]:
    """The admins' queue of open challenges."""
    return list(stack.answerer().http.get("/interventions").json()["items"])


def open_for(stack: Stack, tracking_number: str, seen: set[str] | frozenset[str] = frozenset()) -> dict | None:
    return next((i for i in pending(stack) if i["tracking_number"] == tracking_number and i["id"] not in seen), None)


def challenge_for(stack: Stack, tracking_number: str) -> dict:
    """The open challenge of a shipment, as the admins see it."""
    return wait_until(lambda: open_for(stack, tracking_number), 20, f"a challenge for {tracking_number}")


def answer(stack: Stack, intervention_id: str, response: str, by: User | None = None) -> httpx.Response:
    """An admin (or ``by``) answers a challenge."""
    who = by or stack.answerer()
    return who.http.post(f"/interventions/{intervention_id}/response", json={"response": response})


def interventions(stack: Stack, job_id: str) -> list[dict]:
    return stack.query(
        "SELECT sequence, kind, status, attempt, ciphertext IS NULL AS wiped, image IS NULL AS no_image "
        "FROM human_interventions WHERE job_id = %s ORDER BY sequence",
        job_id,
    )


STAMPS_AND_IDS = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}"
)


def session_ref(stack: Stack, intervention_id: str) -> str:
    (row,) = stack.query("SELECT session_ref::text AS ref FROM human_interventions WHERE id = %s", intervention_id)
    return str(row["ref"])


def test_a_one_time_code_is_asked_for_answered_and_the_same_attempt_continues(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    waiting = user.wait_for(request_id, OTP)["latest_job"]
    assert waiting["attempt"] == 1

    challenge = challenge_for(stack, tracking_number)
    assert challenge["kind"] == "otp"
    assert challenge["status"] == "pending"
    assert challenge["has_image"] is False
    assert challenge["response_format"] == {"min_length": 6, "max_length": 6, "charset": "digits"}
    assert (challenge["provider_code"], challenge["tracking_number"]) == ("demosite", tracking_number)
    assert len(stack.session_keys()) >= 1, "the worker parked its session in Redis"

    code = otp(tracking_number)
    assert answer(stack, challenge["id"], code).status_code == 202

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
    assert pending(stack) == []
    # The answer travelled in no message and no log line.
    messages = stack.query(
        "SELECT payload::text AS body FROM outbox_messages WHERE payload::text LIKE %s", f"%{job['id']}%"
    )
    assert {m["body"].count('"job.resume"') for m in messages} == {0, 1}
    assert all(code not in m["body"] for m in messages)
    for name, text in stack.log_files().items():
        # A 6-digit code can turn up by chance inside a timestamp's microseconds or an ID.
        assert code not in STAMPS_AND_IDS.sub("", text), f"the answer appears in {name}"
    # The worker fetched it over the internal API, and that was recorded.
    audit = stack.query(
        "SELECT action, outcome FROM audit_log WHERE action = 'intervention.resolve' ORDER BY id DESC LIMIT 1"
    )
    assert audit, "resolving an answer is audited"


def test_the_session_in_redis_is_ciphertext_and_removed_after_use(stack: Stack, user: User) -> None:
    wait_until(lambda: not stack.session_keys(), 20, "earlier sessions to be used up")
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(stack, tracking_number)
    (key,) = stack.session_keys()
    assert key.startswith(f"{stack.session_prefix}demosite:")
    (stored,) = stack.session_values()
    assert b"token" not in stored, "the session is encrypted before it leaves the worker"
    assert answer(stack, challenge["id"], otp(tracking_number)).status_code == 202
    user.wait_for(request_id)
    wait_until(lambda: not stack.session_keys(), 10, "the used session to be removed")


def test_a_captcha_is_shown_to_the_user_as_a_picture_and_their_answer_continues_the_lookup(
    stack: Stack, user: User
) -> None:
    tracking_number = number("CAPTCHA", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    user.wait_for(request_id, CAPTCHA)
    challenge = challenge_for(stack, tracking_number)
    assert (challenge["kind"], challenge["has_image"]) == ("captcha", True)

    detail = stack.answerer().http.get(f"/interventions/{challenge['id']}").json()
    assert detail["image"]["content_type"] == "image/png"
    assert base64.b64decode(detail["image"]["data"]).startswith(b"\x89PNG\r\n\x1a\n")

    # The person reads the picture. (The test knows the demo site's rule instead.)
    assert answer(stack, challenge["id"], site_answer("captcha", tracking_number, 5)).status_code == 202
    job = user.wait_for(request_id)["latest_job"]
    assert (job["status"], job["outcome"], job["attempt"]) == ("COMPLETED", "SUCCESS", 1)
    (row,) = interventions(stack, job["id"])
    assert (row["status"], row["no_image"]) == ("completed", True), "the picture is deleted with the answer"


def test_an_answer_of_the_wrong_form_is_refused_at_once_and_the_challenge_stays_open(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(stack, tracking_number)
    for malformed in ("12345", "1234567", "12345a", ""):
        assert answer(stack, challenge["id"], malformed).status_code == 422
    assert user.job(request_id)["status"] == OTP
    assert answer(stack, challenge["id"], otp(tracking_number)).status_code == 202
    assert answer(stack, challenge["id"], otp(tracking_number)).status_code == 409, "an answer is given once"
    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_a_wrong_code_leads_to_a_second_challenge_and_the_right_code_then_completes(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    first = challenge_for(stack, tracking_number)
    assert answer(stack, first["id"], wrong(otp(tracking_number))).status_code == 202

    def second_challenge() -> dict | None:
        return open_for(stack, tracking_number, {first["id"]})

    second = wait_until(second_challenge, 20, "the site to ask again")
    assert second["prompt"].startswith("The previous answer was not accepted.")
    assert answer(stack, second["id"], otp(tracking_number)).status_code == 202

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
            challenge = wait_until(lambda: open_for(stack, tracking_number, seen), 20, "a challenge")
            seen.add(challenge["id"])
            assert answer(stack, challenge["id"], wrong(otp(tracking_number))).status_code == 202
        job = user.wait_for(request_id)["latest_job"]
    finally:
        stack.execute("UPDATE providers SET max_challenges = 3 WHERE code = 'demosite'")
    assert (job["status"], job["last_error_class"], job["attempt"]) == ("FAILED", "CHALLENGE_LIMIT_EXCEEDED", 1)
    assert [i["status"] for i in interventions(stack, job["id"])] == ["rejected", "rejected"]
    assert pending(stack) == []


def test_a_challenge_nobody_answers_expires_and_can_no_longer_be_answered(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(stack, tracking_number)
    job_id = user.job(request_id)["id"]
    # Move time: the deadline passed a moment ago.
    stack.execute(
        "UPDATE tracking_jobs SET intervention_deadline_at = now() - interval '1 second' WHERE id = %s", job_id
    )
    stack.execute("UPDATE human_interventions SET expires_at = now() - interval '1 second' WHERE job_id = %s", job_id)

    job = user.wait_for(request_id, "EXPIRED")["latest_job"]
    assert job["last_error_class"] == "INTERVENTION_EXPIRED"
    assert answer(stack, challenge["id"], otp(tracking_number)).status_code == 410
    assert pending(stack) == []
    assert [i["status"] for i in interventions(stack, job_id)] == ["expired"]
    # The shipment can be looked up again: that starts a fresh job and a fresh challenge.
    assert user.http.post(f"/tracking-requests/{request_id}/refresh").status_code == 202
    again = challenge_for(stack, tracking_number)
    assert again["id"] != challenge["id"]
    assert answer(stack, again["id"], otp(tracking_number)).status_code == 202
    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_a_lost_session_restarts_the_lookup_and_asks_again(stack: Stack, user: User) -> None:
    with stack.provider("demosite", retry_base_ms=100, max_attempts=3):
        tracking_number = number("OTP", "DEMO")
        request_id = user.submit(tracking_number, "demosite")
        first = challenge_for(stack, tracking_number)
        assert stack.drop_sessions() >= 1  # Redis was restarted, or the session timed out
        assert answer(stack, first["id"], otp(tracking_number)).status_code == 202

        def asked_again() -> dict | None:
            return open_for(stack, tracking_number, {first["id"]})

        second = wait_until(asked_again, 30, "the lookup to restart and ask again")
        job = user.job(request_id)
        assert (job["status"], job["attempt"]) == (OTP, 2)
        errors = stack.query(
            "SELECT error_class::text AS c FROM job_attempts WHERE job_id = %s ORDER BY attempt", job["id"]
        )
        assert errors[0]["c"] == "SESSION_LOST"
        assert answer(stack, second["id"], otp(tracking_number)).status_code == 202
        assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_only_admins_see_and_answer_challenges_of_every_organization(stack: Stack, user: User) -> None:
    other_org = stack.new_user(organization="E2E second organization")
    tracking_number, theirs = number("OTP", "DEMO"), number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    other_request = other_org.submit(theirs, "demosite")
    challenge = challenge_for(stack, tracking_number)
    second = challenge_for(stack, theirs)

    # Organization users, the one who asked included, are refused.
    for org_user in (user, other_org):
        listing = org_user.http.get("/interventions")
        assert listing.status_code == 403
        assert listing.json()["error"]["code"] == "admins_only"
        assert org_user.http.get(f"/interventions/{challenge['id']}").status_code == 403
        assert answer(stack, challenge["id"], otp(tracking_number), by=org_user).status_code == 403
    assert httpx.get(f"{stack.base_url}/v1/interventions").status_code == 401
    assert user.job(request_id)["status"] == OTP, "the refused attempts changed nothing"

    # Any admin sees both organizations' challenges in one queue and answers them.
    colleague = stack.new_user(organization=None)
    assert {challenge["id"], second["id"]} <= {item["id"] for item in pending(stack)}
    assert answer(stack, challenge["id"], otp(tracking_number), by=colleague).status_code == 202
    assert answer(stack, second["id"], otp(theirs)).status_code == 202
    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"
    assert other_org.wait_for(other_request)["latest_job"]["status"] == "COMPLETED"


def test_only_the_worker_of_the_jobs_adapter_can_fetch_an_answer(stack: Stack, user: User) -> None:
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(stack, tracking_number)
    job = user.job(request_id)
    body = {
        "intervention_id": challenge["id"],
        "session_ref": session_ref(stack, challenge["id"]),
        "job_id": job["id"],
        "attempt": job["attempt"],
    }
    url = f"{stack.internal_url}/internal/v1/interventions/resolve"
    foreign = stack.admin("create-worker-token", "--adapter", "dummy").split("worker_token: ")[1].split()[0]

    assert httpx.post(url, json=body).status_code == 401
    assert httpx.post(url, json=body, headers={"Authorization": f"Bearer {user.api_key}"}).status_code == 401
    refused = httpx.post(url, json=body, headers={"Authorization": f"Bearer {foreign}"})
    assert refused.status_code in (403, 404), "a worker of another adapter gets nothing"
    assert httpx.post(f"{stack.base_url}/internal/v1/interventions/resolve", json=body).status_code == 404, (
        "the internal API is not part of the public listener"
    )

    assert answer(stack, challenge["id"], otp(tracking_number)).status_code == 202
    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_the_human_interface_lets_an_admin_sign_in_and_answer(stack: Stack, user: User) -> None:
    stack.start_interface()
    tracking_number = number("CAPTCHA", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(stack, tracking_number)

    with httpx.Client(base_url=stack.interface_url, follow_redirects=True, timeout=15) as browser:
        assert "Sign in" in browser.get("/").text
        assert browser.post("/login", data={"api_key": "ctk_not_a_real_key"}).status_code == 401
        listing = browser.post("/login", data={"api_key": stack.answerer().api_key})
        assert f'href="/interventions/{challenge["id"]}"' in listing.text
        assert stack.answerer().api_key not in "".join(f"{c.name}={c.value}" for c in browser.cookies.jar)

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
    assert stack.answerer().api_key not in stack.log_files()["human-interface.log"]


# ----------------------------------------------------------------------------- review corrections


def test_the_answer_is_handed_out_only_for_the_session_of_its_challenge(stack: Stack, user: User) -> None:
    """Even the right worker, for the right job and attempt, gets nothing for another session."""
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(stack, tracking_number)
    job = user.job(request_id)
    url = f"{stack.internal_url}/internal/v1/interventions/resolve"
    worker = {"Authorization": f"Bearer {stack.demosite_token}"}
    body = {"intervention_id": challenge["id"], "job_id": job["id"], "attempt": job["attempt"]}
    own = session_ref(stack, challenge["id"])
    code = otp(tracking_number)

    stack.stop_worker("demosite")  # so the test, not the worker, is the one asking
    stack.set_provider("demosite", queue_wait_max_ms=120_000)  # the resume may wait for the restart
    try:
        assert answer(stack, challenge["id"], code).status_code == 202

        other = "01a2111d-b3a8-7cc2-a6b1-c5e86fea3eba"
        denied = httpx.post(url, json={**body, "session_ref": other}, headers=worker)
        assert denied.status_code == 403
        assert code not in denied.text
        unnamed = httpx.post(url, json=body, headers=worker)
        assert 400 <= unnamed.status_code < 500
        assert code not in unnamed.text
        (audit,) = stack.query(
            "SELECT outcome, reason FROM audit_log WHERE action = 'intervention.resolve' AND job_id = %s", job["id"]
        )
        assert (audit["outcome"], audit["reason"]) == ("denied", "session_mismatch")

        granted = httpx.post(url, json={**body, "session_ref": own}, headers=worker)
        assert granted.status_code == 200
        assert granted.json() == {"response": code}
    finally:
        stack.set_provider("demosite", queue_wait_max_ms=8000)
        stack.start_worker(adapter="demosite")
    assert user.wait_for(request_id)["latest_job"]["status"] == "COMPLETED"


def test_copies_of_a_resume_message_submit_the_answer_to_the_site_once(stack: Stack, user: User) -> None:
    """RabbitMQ may deliver job.resume more than once. Here two extra copies are put into the
    queue by hand: one races the original, one arrives after everything is over."""
    tracking_number = number("OTP", "DEMO")
    request_id = user.submit(tracking_number, "demosite")
    challenge = challenge_for(stack, tracking_number)
    job_id = user.job(request_id)["id"]

    stack.stop_worker("demosite")  # let the copies queue up behind the original
    stack.set_provider("demosite", queue_wait_max_ms=120_000)
    try:
        assert answer(stack, challenge["id"], otp(tracking_number)).status_code == 202
        (resume,) = wait_until(
            lambda: stack.query(
                "SELECT payload::text AS body FROM outbox_messages "
                "WHERE message_type = 'job.resume' AND payload::text LIKE %s AND status = 'sent'",
                f"%{job_id}%",
            ),
            10,
            "the job.resume message to be published",
        )
        stack.publish("courier.jobs", "job.resume.v1.demosite", resume["body"])
    finally:
        stack.set_provider("demosite", queue_wait_max_ms=8000)
        stack.start_worker(adapter="demosite")

    view = user.wait_for(request_id)
    assert (view["latest_job"]["status"], view["latest_job"]["attempt"]) == ("COMPLETED", 1)
    stack.publish("courier.jobs", "job.resume.v1.demosite", resume["body"])  # a very late copy
    wait_until(
        lambda: stack.workers["demosite"].log_text().count("resume_already_reported") >= 2,
        15,
        "both copies to be recognized and acknowledged",
    )

    # One execution asked, one continued. A second submission would have been refused by the
    # site (its token works once) and would show up here as another execution.
    executions = stack.query(
        "SELECT disposition::text AS d FROM adapter_execution_logs WHERE job_id = %s ORDER BY started_at", job_id
    )
    assert [row["d"] for row in executions] == ["suspended", "accepted"]
    allowed = stack.query(
        "SELECT 1 FROM audit_log WHERE action = 'intervention.resolve' AND outcome = 'allowed' AND job_id = %s", job_id
    )
    assert len(allowed) == 1, "the answer was fetched once"
    assert user.job(request_id)["status"] == "COMPLETED"
    # Nothing was dead-lettered and no lease is left; only the 'reported' marker remains.
    assert stack.query("SELECT 1 FROM dead_letters WHERE job_id = %s", job_id) == []
    assert not [key for key in stack.resume_keys() if ":lease:" in key]
    assert any(key.endswith(session_ref(stack, challenge["id"])) and ":done:" in key for key in stack.resume_keys())
