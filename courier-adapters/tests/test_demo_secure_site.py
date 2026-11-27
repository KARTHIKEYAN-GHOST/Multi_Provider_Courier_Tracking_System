"""Demo Secure: the demo courier whose tracking page needs a login (test tooling)."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator

import httpx
import pytest

from demo_site.server import make_server

EMAIL, PASSWORD = "ops@demo-secure.example", "s3cret & <tricky>/pass"


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("DEMO_SITE_SECURE_EMAIL", EMAIL)
    monkeypatch.setenv("DEMO_SITE_SECURE_PASSWORD", PASSWORD)
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def form_token(page: str) -> str:
    found = re.search(r'name="form_token" value="([0-9a-f]+)"', page)
    assert found
    return found.group(1)


def sign_in(client: httpx.Client, email: str = EMAIL, password: str = PASSWORD) -> httpx.Response:
    token = form_token(client.get("/secure/login").text)
    return client.post("/secure/login", data={"email": email, "password": password, "form_token": token})


def test_tracking_without_a_session_leads_to_the_sign_in_page(site: str) -> None:
    with httpx.Client(base_url=site) as client:
        answer = client.get("/secure/track", params={"number": "DEMO-OK-1"})
        assert answer.status_code == 302
        assert answer.headers["location"].startswith("/secure/login?next=")
        page = client.get(answer.headers["location"]).text
        assert 'name="password"' in page


def test_signed_in_the_tracking_page_lists_the_scans_newest_first(site: str) -> None:
    with httpx.Client(base_url=site) as client:
        answer = sign_in(client)
        assert answer.status_code == 303
        assert "demo_secure_session" in client.cookies
        page = client.get("/secure/track", params={"number": "DEMO-OK-7"}).text
    scans = re.findall(r'<span class="desc">([^<]+)</span>', page)
    assert scans == ["Delivered", "Out for delivery", "In transit", "Picked up", "Booked"]
    assert re.search(r'<span class="ts">\d\d-\d\d-2026 \d\d:\d\d</span>', page)
    assert f"Signed in as {EMAIL}" in page
    assert PASSWORD not in page


def test_an_unknown_number_is_a_normal_page(site: str) -> None:
    with httpx.Client(base_url=site) as client:
        sign_in(client)
        page = client.get("/secure/track", params={"number": "DEMO-NOPE-1"})
    assert page.status_code == 200
    assert "Consignment not found" in page.text


@pytest.mark.parametrize(("email", "password"), [(EMAIL, "wrong"), ("other@demo-secure.example", PASSWORD)])
def test_a_wrong_login_gets_the_form_again_and_no_session(site: str, email: str, password: str) -> None:
    with httpx.Client(base_url=site) as client:
        answer = sign_in(client, email, password)
        assert answer.status_code == 200
        assert "Invalid email or password" in answer.text
        assert "demo_secure_session" not in client.cookies
        assert client.get("/secure/track", params={"number": "DEMO-OK-1"}).status_code == 302


def test_a_form_token_works_once(site: str) -> None:
    with httpx.Client(base_url=site) as client:
        token = form_token(client.get("/secure/login").text)
        data = {"email": EMAIL, "password": PASSWORD, "form_token": token}
        assert client.post("/secure/login", data=data).status_code == 303
        assert client.post("/secure/login", data=data).status_code == 400


def test_the_redirect_after_sign_in_stays_on_the_tracking_page(site: str) -> None:
    with httpx.Client(base_url=site) as client:
        token = form_token(client.get("/secure/login").text)
        data = {"email": EMAIL, "password": PASSWORD, "form_token": token, "next": "https://evil.example/"}
        assert client.post("/secure/login", data=data).headers["location"] == "/secure/track"


def test_without_an_account_configured_sign_in_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_SITE_SECURE_PASSWORD", raising=False)
    server = make_server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}") as client:
            assert client.get("/secure/login").status_code == 503
            assert client.get("/secure/track", params={"number": "DEMO-OK-1"}).status_code == 503
    finally:
        server.shutdown()
        server.server_close()
