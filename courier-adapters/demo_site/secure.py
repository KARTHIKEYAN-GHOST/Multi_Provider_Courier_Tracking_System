"""Demo Secure: a second demo courier on the same site whose tracking needs a login.

It exists to test, end to end, a courier whose adapter must log in: the agent building that
adapter with the login through placeholders, and the worker using the login stored
(encrypted) in the Engine.

- ``GET /secure/login``: the sign-in form, with a one-time form token.
- ``POST /secure/login``: email, password and the form token. Right: a session cookie and a
  redirect to the tracking page; wrong: the form again with an error.
- ``GET /secure/track?number=``: without a valid session a redirect to the sign-in page;
  with one, the tracking page (its own layout, not the Demo Courier table).

The account is read from DEMO_SITE_SECURE_EMAIL and DEMO_SITE_SECURE_PASSWORD at every sign
in, never from the code: the agent can read this repository, so a password written here
would not test that it never sees the login. Without a password the pages answer 503.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from html import escape
from urllib.parse import quote

from .pages import IST, Page, _history

SESSION_LIFETIME_S = 30 * 60
FORM_LIFETIME_S = 10 * 60
COOKIE = "demo_secure_session"
NOT_FOUND = "Consignment not found. Check the number."
_MAX_OPEN = 10_000

_lock = threading.Lock()
_sessions: dict[str, float] = {}  # session id -> expiry (monotonic)
_forms: dict[str, float] = {}  # one-time form token -> expiry


def _remember(store: dict[str, float], lifetime_s: float) -> str:
    token = secrets.token_hex(16)
    now = time.monotonic()
    with _lock:
        if len(store) > _MAX_OPEN:
            for key in [k for k, until in store.items() if until < now]:
                del store[key]
            if len(store) > _MAX_OPEN:
                store.clear()
        store[token] = now + lifetime_s
    return token


def _valid(store: dict[str, float], token: str, *, once: bool = False) -> bool:
    with _lock:
        until = store.pop(token, None) if once else store.get(token)
    return until is not None and until >= time.monotonic()


def _account() -> tuple[str, str] | None:
    password = os.environ.get("DEMO_SITE_SECURE_PASSWORD", "")
    if not password:
        return None
    return os.environ.get("DEMO_SITE_SECURE_EMAIL", "ops@demo-secure.example"), password


def _layout(title: str, content: str, signed_in: bool = False) -> str:
    account = _account()
    who = (
        f'<span class="account">Signed in as {escape(account[0])}</span>'
        if signed_in and account
        else '<a href="/secure/login">Sign in</a>'
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        f"<title>{escape(title)} | Demo Secure Logistics</title></head>\n<body>\n"
        f'<div class="topbar"><b>Demo Secure Logistics</b> {who}</div>\n'
        f'<div class="content">\n{content}\n</div>\n'
        '<div class="foot">Business customers only. For testing.</div>\n</body>\n</html>\n'
    )


def _not_configured() -> Page:
    return Page(503, _layout("Unavailable", "<p>Sign-in is not available.</p>"))


def login_page(next_path: str = "/secure/track", error: bool = False) -> Page:
    if _account() is None:
        return _not_configured()
    form = _remember(_forms, FORM_LIFETIME_S)
    message = '\n  <p class="login-error">Invalid email or password.</p>' if error else ""
    return Page(
        200,
        _layout(
            "Sign in",
            f'<div class="login">\n  <h2>Business sign in</h2>{message}\n'
            '  <form method="post" action="/secure/login">\n'
            f'    <input type="hidden" name="form_token" value="{form}">\n'
            f'    <input type="hidden" name="next" value="{escape(_safe_next(next_path))}">\n'
            '    <label>Email <input name="email" type="email"></label>\n'
            '    <label>Password <input name="password" type="password"></label>\n'
            "    <button>Sign in</button>\n  </form>\n</div>",
        ),
    )


def _safe_next(path: str) -> str:
    """Only a path on this site's tracking page; anything else goes to the default."""
    return path if path.startswith("/secure/track") and "//" not in path else "/secure/track"


def sign_in(form: dict[str, list[str]]) -> Page:
    account = _account()
    if account is None:
        return _not_configured()

    def field(name: str) -> str:
        return form.get(name, [""])[0]

    next_path = _safe_next(field("next") or "/secure/track")
    if not _valid(_forms, field("form_token"), once=True):
        return Page(
            400, _layout("Expired", '<p class="login-expired">The form has expired. Sign in again.</p>')
        )
    email_ok = hmac.compare_digest(field("email").strip().lower().encode(), account[0].lower().encode())
    password_ok = hmac.compare_digest(field("password").encode(), account[1].encode())
    if not (email_ok and password_ok):
        return login_page(next_path, error=True)
    session = _remember(_sessions, SESSION_LIFETIME_S)
    cookie = f"{COOKIE}={session}; Path=/secure; HttpOnly; SameSite=Lax; Max-Age={SESSION_LIFETIME_S}"
    return Page(303, "", (("Location", next_path), ("Set-Cookie", cookie)))


def _session(cookie_header: str) -> str:
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return ""


def track(number: str, cookie_header: str) -> Page:
    if _account() is None:
        return _not_configured()
    if not _valid(_sessions, _session(cookie_header)):
        target = quote(f"/secure/track?number={number}", safe="")
        return Page(302, "", (("Location", f"/secure/login?next={target}"),))
    kind = number.upper().removeprefix("DEMO-").split("-", 1)[0]
    steps = {"OK": 5, "TRANSIT": 3, "BOOKED": 1}.get(kind)
    if steps is None:
        return Page(
            200,
            _layout("Consignment", f'<div class="empty">{NOT_FOUND}</div>', True),
        )
    scans = "\n".join(
        f'    <li class="scan"><span class="ts">{when.astimezone(IST):%d-%m-%Y %H:%M}</span>'
        f'<span class="desc">{escape(status)}</span><span class="loc">{escape(city)}</span></li>'
        for when, status, city, _ in (
            (w, s, p.split(",")[0], n) for w, s, p, n in reversed(_history(number, steps))
        )
    )
    return Page(
        200,
        _layout(
            f"Consignment {number}",
            f'<div class="consignment" data-ref="{escape(number)}">\n'
            f"  <h2>Consignment {escape(number)}</h2>\n"
            "  <p>Times are India Standard Time. Newest first.</p>\n"
            f'  <ol class="scans">\n{scans}\n  </ol>\n</div>',
            True,
        ),
    )
