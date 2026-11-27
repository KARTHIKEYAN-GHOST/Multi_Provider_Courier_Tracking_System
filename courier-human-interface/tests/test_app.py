"""The human interface against a stand-in for the Engine's public API."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from human_interface.app import COOKIE, answer_field, create_app, image_src
from human_interface.config import Settings
from human_interface.signins import SignIns

KEY_A, KEY_B = "ctk_aaaaaaaaaaaa_" + "a" * 64, "ctk_bbbbbbbbbbbb_" + "b" * 64
KEY_ORG = "ctk_cccccccccccc_" + "c" * 64
OTP_ID = "01a2111d-b3a8-7cc2-a6b1-c5e86fea3eba"
CAPTCHA_ID = "01a2111d-b3a8-7cc2-a6b1-c5e86fea3ebb"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()


def challenge(intervention_id: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": intervention_id,
        "kind": kind,
        "prompt": "Enter the code.",
        "status": "pending",
        "response_format": {"min_length": 6, "max_length": 6, "charset": "digits"},
        "has_image": kind == "captcha",
        "expires_at": "2026-11-25T10:00:00Z",
        "created_at": "2026-11-25T09:50:00Z",
        "provider_code": "demosite",
        "tracking_number": "DEMO-OTP-1",
        "image": {"content_type": "image/png", "data": PNG} if kind == "captcha" else None,
        **extra,
    }


class FakeEngine:
    """Admin A sees two open challenges; admin B sees none (another moment of the queue).
    KEY_ORG belongs to an organization user, whom the Engine refuses. Other keys are unknown."""

    def __init__(self) -> None:
        self.items = {OTP_ID: challenge(OTP_ID, "otp"), CAPTCHA_ID: challenge(CAPTCHA_ID, "captcha")}
        self.answers: list[tuple[str, str]] = []
        self.answer_status = 202
        self.down = False
        self.revoked = False
        self.demoted = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("refused")
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        if key == KEY_ORG or (self.demoted and key == KEY_A):
            return httpx.Response(403, json={"error": {"code": "admins_only"}})
        if self.revoked or key not in (KEY_A, KEY_B):
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        mine = self.items if key == KEY_A else {}
        path = request.url.path
        if path == "/v1/interventions":
            listed = [{k: v for k, v in item.items() if k != "image"} for item in mine.values()]
            return httpx.Response(200, json={"items": listed})
        intervention_id = path.split("/")[3]
        if intervention_id not in mine:
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        if request.method == "GET":
            return httpx.Response(200, json=mine[intervention_id])
        self.answers.append((intervention_id, json.loads(request.content)["response"]))
        if self.answer_status == 202:
            mine[intervention_id]["status"] = "answered"
        return httpx.Response(self.answer_status, json={})


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def site(engine: FakeEngine) -> TestClient:
    app = create_app(Settings(_env_file=None), httpx.MockTransport(engine))  # type: ignore[call-arg]
    return TestClient(app, follow_redirects=False)


def sign_in(site: TestClient, key: str = KEY_A) -> None:
    response = site.post("/login", data={"api_key": key})
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/interventions"


def csrf(site: TestClient, path: str = f"/interventions/{OTP_ID}") -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', site.get(path).text)
    assert found
    return found.group(1)


def test_nothing_is_shown_without_signing_in(site: TestClient, engine: FakeEngine) -> None:
    for path in ("/interventions", f"/interventions/{OTP_ID}"):
        response = site.get(path)
        assert (response.status_code, response.headers["location"]) == (303, "/login")
    response = site.post(f"/interventions/{OTP_ID}", data={"answer": "123456"})
    assert response.headers["location"] == "/login"
    assert engine.requests == [], "the Engine is not even asked"
    assert site.get("/").headers["location"] == "/interventions"
    assert site.get("/healthz").text == "ok"


def test_a_wrong_api_key_is_refused_and_no_session_is_created(site: TestClient) -> None:
    response = site.post("/login", data={"api_key": "ctk_wrong"})
    assert response.status_code == 401
    assert "not accepted" in response.text
    assert COOKIE not in response.cookies
    assert "ctk_wrong" not in response.text


def test_the_cookie_is_a_random_id_not_the_api_key(site: TestClient) -> None:
    response = site.post("/login", data={"api_key": KEY_A})
    header = response.headers["set-cookie"]
    assert KEY_A not in header
    assert "HttpOnly" in header
    assert "SameSite=strict" in header
    assert "Secure" not in header  # off by default for http://localhost; see the next test


def test_the_cookie_is_marked_secure_when_configured(engine: FakeEngine) -> None:
    settings = Settings(_env_file=None, cookie_secure=True)  # type: ignore[call-arg]
    client = TestClient(create_app(settings, httpx.MockTransport(engine)), follow_redirects=False)
    assert "Secure" in client.post("/login", data={"api_key": KEY_A}).headers["set-cookie"]


def test_the_list_shows_the_users_open_challenges(site: TestClient) -> None:
    sign_in(site)
    page = site.get("/interventions")
    assert page.status_code == 200
    assert f'href="/interventions/{OTP_ID}"' in page.text
    assert f'href="/interventions/{CAPTCHA_ID}"' in page.text
    assert "One-time code" in page.text
    assert "Picture code" in page.text
    assert 'http-equiv="refresh"' in page.text
    assert KEY_A not in page.text


def test_another_user_sees_nothing_and_cannot_open_or_answer(site: TestClient, engine: FakeEngine) -> None:
    sign_in(site, KEY_B)
    assert "Nothing is waiting" in site.get("/interventions").text
    assert site.get(f"/interventions/{OTP_ID}").status_code == 404
    token = re.search(r'name="csrf" value="([^"]+)"', site.get("/interventions").text)
    assert token
    response = site.post(f"/interventions/{OTP_ID}", data={"answer": "123456", "csrf": token.group(1)})
    assert response.status_code == 404
    assert engine.items[OTP_ID]["status"] == "pending"


def test_an_otp_page_asks_for_digits(site: TestClient) -> None:
    sign_in(site)
    page = site.get(f"/interventions/{OTP_ID}").text
    assert "Enter the code." in page
    assert 'inputmode="numeric"' in page
    assert 'maxlength="6"' in page
    assert "<img" not in page


def test_a_captcha_is_shown_as_an_embedded_picture(site: TestClient) -> None:
    sign_in(site)
    page = site.get(f"/interventions/{CAPTCHA_ID}").text
    assert f'<img class="challenge" src="data:image/png;base64,{PNG}"' in page


def test_an_answer_is_forwarded_once_and_never_shown_again(site: TestClient, engine: FakeEngine) -> None:
    sign_in(site)
    response = site.post(f"/interventions/{OTP_ID}", data={"answer": " 424242 ", "csrf": csrf(site)})
    assert (response.status_code, response.headers["location"]) == (303, "/interventions?notice=sent")
    assert engine.answers == [(OTP_ID, "424242")]
    after = site.get("/interventions?notice=sent")
    assert "Your answer was sent" in after.text
    assert "424242" not in after.text
    # Opening it again leads back to the list: it is no longer pending.
    assert (
        site.get(f"/interventions/{OTP_ID}").headers["location"] == "/interventions?notice=already_answered"
    )


@pytest.mark.parametrize(
    ("status", "location"),
    [(409, "/interventions?notice=already_answered"), (410, "/interventions?notice=expired")],
)
def test_engine_refusals_are_explained(
    site: TestClient, engine: FakeEngine, status: int, location: str
) -> None:
    sign_in(site)
    token = csrf(site)
    engine.answer_status = status
    response = site.post(f"/interventions/{OTP_ID}", data={"answer": "424242", "csrf": token})
    assert response.headers["location"] == location
    assert site.get(location).status_code == 200


def test_an_answer_of_the_wrong_form_shows_the_challenge_again(site: TestClient, engine: FakeEngine) -> None:
    sign_in(site)
    token = csrf(site)
    engine.answer_status = 422
    response = site.post(f"/interventions/{OTP_ID}", data={"answer": "12ab", "csrf": token})
    assert response.status_code == 422
    assert "does not have the expected form" in response.text
    assert "12ab" not in response.text, "the answer is not echoed back"


def test_a_form_without_the_pages_token_is_refused(site: TestClient, engine: FakeEngine) -> None:
    sign_in(site)
    for data in ({"answer": "424242"}, {"answer": "424242", "csrf": "guess"}):
        assert site.post(f"/interventions/{OTP_ID}", data=data).status_code == 403
    assert engine.answers == []
    assert site.post("/logout", data={"csrf": "guess"}).status_code == 403


def test_signing_out_ends_the_session(site: TestClient) -> None:
    sign_in(site)
    response = site.post("/logout", data={"csrf": csrf(site)})
    assert response.headers["location"] == "/login?notice=signed_out"
    assert site.get("/interventions").headers["location"] == "/login"


def test_a_revoked_key_signs_the_user_out(site: TestClient, engine: FakeEngine) -> None:
    sign_in(site)
    engine.revoked = True
    assert site.get("/interventions").headers["location"] == "/login?notice=bad_key"
    assert site.get("/interventions").headers["location"] == "/login"


def test_an_organization_user_is_told_that_only_admins_answer(site: TestClient) -> None:
    response = site.post("/login", data={"api_key": KEY_ORG})
    assert response.status_code == 403
    assert "Only admins answer verification requests." in response.text
    assert COOKIE not in response.cookies


def test_an_admin_who_loses_the_role_is_signed_out(site: TestClient, engine: FakeEngine) -> None:
    sign_in(site)
    engine.demoted = True
    assert site.get("/interventions").headers["location"] == "/login?notice=admins_only"
    assert site.get("/interventions").headers["location"] == "/login"


def test_an_unreachable_engine_is_reported_without_details(site: TestClient, engine: FakeEngine) -> None:
    sign_in(site)
    token = csrf(site)
    engine.down = True
    page = site.get("/interventions")
    assert page.status_code == 503
    assert "cannot be reached" in page.text
    assert "ConnectError" not in page.text
    response = site.post(f"/interventions/{OTP_ID}", data={"answer": "424242", "csrf": token})
    assert response.headers["location"] == "/interventions?notice=unavailable"
    assert site.post("/login", data={"api_key": KEY_A}).status_code == 503


def test_text_from_a_challenge_is_escaped(site: TestClient, engine: FakeEngine) -> None:
    engine.items[OTP_ID]["prompt"] = '<script>alert(1)</script><img src=x onerror="steal()">'
    engine.items[OTP_ID]["tracking_number"] = '"><b>x</b>'
    sign_in(site)
    for path in ("/interventions", f"/interventions/{OTP_ID}"):
        page = site.get(path).text
        assert "<script>" not in page
        assert "<b>x</b>" not in page
    assert "&lt;script&gt;" in site.get(f"/interventions/{OTP_ID}").text


@pytest.mark.parametrize(
    "image",
    [
        {"content_type": "image/svg+xml", "data": PNG},
        {"content_type": "text/html", "data": PNG},
        {"content_type": "image/png", "data": 'x" onerror="steal()'},
        {"content_type": "image/png", "data": ""},
        {"content_type": "image/png", "data": "A" * 200_000},
        {"content_type": "image/png"},
        "data:image/png;base64,AAAA",
        None,
    ],
)
def test_only_a_plain_picture_is_ever_embedded(image: Any) -> None:
    assert image_src(image) is None


def test_answer_hints_follow_the_format_and_fall_back_safely() -> None:
    assert answer_field({"min_length": 5, "max_length": 5, "charset": "alphanumeric"}) == {
        "min_length": 5,
        "max_length": 5,
        "inputmode": None,
        "pattern": "[A-Za-z0-9]*",
    }
    for odd in (None, "x", {"min_length": 9, "max_length": 2}, {"min_length": "1", "max_length": 500}):
        assert answer_field(odd)["max_length"] == 64


def test_every_page_forbids_scripts_and_caching(site: TestClient) -> None:
    sign_in(site)
    for path in ("/login", "/interventions", f"/interventions/{CAPTCHA_ID}", "/static/style.css"):
        headers = site.get(path).headers
        assert "default-src 'none'" in headers["content-security-policy"]
        assert "script-src" not in headers["content-security-policy"]
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
    assert "<script" not in site.get(f"/interventions/{CAPTCHA_ID}").text


def test_ids_that_are_not_uuids_never_reach_the_engine(site: TestClient, engine: FakeEngine) -> None:
    sign_in(site)
    token = csrf(site)
    before = len(engine.requests)
    assert site.get("/interventions/..%2F..%2Fadmin").status_code == 404
    assert site.post("/interventions/not-a-uuid", data={"answer": "1", "csrf": token}).status_code == 404
    assert len(engine.requests) == before


def test_sign_ins_expire_and_are_bounded() -> None:
    expired = SignIns(ttl_s=0)
    assert expired.get(expired.add(KEY_A)) is None
    store = SignIns(ttl_s=60)
    first = store.add(KEY_A)
    found = store.get(first)
    assert found is not None
    assert found.api_key == KEY_A
    assert store.get("unknown") is None
    assert store.get(None) is None
    store.remove(first)
    assert store.get(first) is None
