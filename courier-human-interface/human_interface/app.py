"""The pages: sign in, see what is waiting, answer it.

What this site must get right
    * It shows a challenge only through the Engine, with the user's own API key, so it can
      never show somebody else's challenge.
    * A CAPTCHA picture is displayed as a picture and nothing else: only PNG, JPEG, GIF and
      WebP, embedded as data. All text (prompts come from adapters) is escaped.
    * An answer is forwarded to the Engine and then forgotten. It is not logged, not kept in
      the sign-in, and never shown again.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Settings
from .engine import EngineClient, EngineUnavailable, NotAnAdmin, NotSignedIn
from .signins import SignIn, SignIns

COOKIE = "hi_session"
_HERE = Path(__file__).parent
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAX_IMAGE_CHARS = 140_000
_HEADERS = {
    # No scripts at all; pictures only as embedded data; forms only back to this site.
    "Content-Security-Policy": (
        "default-src 'none'; img-src data:; style-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_NOTICES = {
    "sent": ("Your answer was sent. The lookup continues.", "ok"),
    "invalid": ("That answer does not have the expected form. Please check it and try again.", "error"),
    "already_answered": ("This request has already been answered.", "info"),
    "expired": ("This request is no longer open.", "error"),
    "signed_out": ("You are signed out.", "info"),
    "bad_key": ("That API key was not accepted.", "error"),
    "admins_only": (
        "Only admins answer verification requests. Your organization's lookups continue on their own.",
        "error",
    ),
    "unavailable": ("The tracking service cannot be reached right now. Please try again.", "error"),
}


def refusal(error: NotSignedIn) -> str:
    """The notice shown after signing a user out."""
    return "admins_only" if isinstance(error, NotAnAdmin) else "bad_key"


def image_src(image: Any) -> str | None:
    """A ``data:`` address for the picture, or nothing if it is not a plain image."""
    if not isinstance(image, dict):
        return None
    content_type, data = image.get("content_type"), image.get("data")
    if content_type not in _IMAGE_TYPES or not isinstance(data, str):
        return None
    if not 0 < len(data) <= _MAX_IMAGE_CHARS or not _BASE64.match(data):
        return None
    return f"data:{content_type};base64,{data}"


def answer_field(response_format: Any) -> dict[str, Any]:
    """Input hints from the challenge's answer format. The Engine still validates."""
    field: dict[str, Any] = {"min_length": 1, "max_length": 64, "inputmode": None, "pattern": None}
    if not isinstance(response_format, dict):
        return field
    low, high = response_format.get("min_length"), response_format.get("max_length")
    if isinstance(low, int) and isinstance(high, int) and 1 <= low <= high <= 64:
        field.update(min_length=low, max_length=high)
    if response_format.get("charset") == "digits":
        field.update(inputmode="numeric", pattern="[0-9]*")
    elif response_format.get("charset") == "alphanumeric":
        field.update(pattern="[A-Za-z0-9]*")
    return field


def create_app(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    engine = EngineClient(settings.engine_url, transport)
    signins = SignIns(settings.session_ttl_s)
    templates = Environment(
        loader=FileSystemLoader(_HERE / "templates"), autoescape=select_autoescape(default=True)
    )
    stylesheet = (_HERE / "style.css").read_text(encoding="utf-8")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in _HEADERS.items():
            response.headers[name] = value
        return response

    def page(name: str, status: int = 200, *, notice: str | None = None, **context: Any) -> HTMLResponse:
        text, kind = _NOTICES.get(notice or "", (None, None))
        html = templates.get_template(name).render(notice=text, notice_kind=kind, **context)
        return HTMLResponse(html, status)

    def to(path: str) -> RedirectResponse:
        return RedirectResponse(path, status_code=303)

    def signed_in(request: Request) -> SignIn | None:
        return signins.get(request.cookies.get(COOKIE))

    def sign_out(request: Request, notice: str) -> RedirectResponse:
        signins.remove(request.cookies.get(COOKIE))
        response = to(f"/login?notice={notice}")
        response.delete_cookie(COOKIE)
        return response

    def csrf_ok(user: SignIn, sent: str) -> bool:
        return secrets.compare_digest(user.csrf.encode(), sent.encode())

    @app.get("/healthz")
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/static/style.css")
    async def style() -> Response:
        return Response(stylesheet, media_type="text/css")

    @app.get("/")
    async def home() -> RedirectResponse:
        return to("/interventions")

    @app.get("/login")
    async def login_form(notice: str | None = None) -> HTMLResponse:
        return page("login.html", notice=notice, csrf=None)

    @app.post("/login")
    async def login(api_key: str = Form(max_length=200)) -> Response:
        # The key is checked by using it: the Engine is the only judge of API keys.
        try:
            await engine.pending(api_key.strip())
        except NotAnAdmin:
            return page("login.html", 403, notice="admins_only", csrf=None)
        except NotSignedIn:
            return page("login.html", 401, notice="bad_key", csrf=None)
        except EngineUnavailable:
            return page("login.html", 503, notice="unavailable", csrf=None)
        response = to("/interventions")
        response.set_cookie(
            COOKIE,
            signins.add(api_key.strip()),
            max_age=settings.session_ttl_s,
            httponly=True,
            samesite="strict",
            secure=settings.cookie_secure,
        )
        return response

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form(default="", max_length=200)) -> Response:
        user = signed_in(request)
        if user is not None and not csrf_ok(user, csrf):
            return page(
                "message.html", 403, heading="Not allowed", text="Please use the page's own form.", csrf=None
            )
        return sign_out(request, "signed_out")

    @app.get("/interventions")
    async def waiting(request: Request, notice: str | None = None) -> Response:
        user = signed_in(request)
        if user is None:
            return to("/login")
        try:
            items = await engine.pending(user.api_key)
        except NotSignedIn as refused:
            return sign_out(request, refusal(refused))
        except EngineUnavailable:
            return page(
                "list.html", 503, notice="unavailable", items=[], csrf=user.csrf, refresh_s=settings.refresh_s
            )
        shown = [item for item in items if _UUID.match(str(item.get("id", "")))]
        return page("list.html", notice=notice, items=shown, csrf=user.csrf, refresh_s=settings.refresh_s)

    async def detail(
        user: SignIn, intervention_id: str, status: int = 200, notice: str | None = None
    ) -> Response:
        item = await engine.one(user.api_key, intervention_id) if _UUID.match(intervention_id) else None
        if item is None:
            return page(
                "message.html",
                404,
                heading="Not found",
                text="There is no such request for you.",
                csrf=user.csrf,
            )
        if item.get("status") != "pending":
            return to("/interventions?notice=already_answered")
        return page(
            "detail.html",
            status,
            notice=notice,
            item=item,
            image_src=image_src(item.get("image")),
            field=answer_field(item.get("response_format")),
            csrf=user.csrf,
        )

    @app.get("/interventions/{intervention_id}")
    async def challenge(request: Request, intervention_id: str) -> Response:
        user = signed_in(request)
        if user is None:
            return to("/login")
        try:
            return await detail(user, intervention_id)
        except NotSignedIn as refused:
            return sign_out(request, refusal(refused))
        except EngineUnavailable:
            return to("/interventions?notice=unavailable")

    @app.post("/interventions/{intervention_id}")
    async def respond(
        request: Request,
        intervention_id: str,
        answer: str = Form(max_length=64),
        csrf: str = Form(default="", max_length=200),
    ) -> Response:
        user = signed_in(request)
        if user is None:
            return to("/login")
        if not csrf_ok(user, csrf):
            return page(
                "message.html",
                403,
                heading="Not allowed",
                text="Please use the page's own form.",
                csrf=user.csrf,
            )
        if not _UUID.match(intervention_id):
            return page(
                "message.html",
                404,
                heading="Not found",
                text="There is no such request for you.",
                csrf=user.csrf,
            )
        try:
            submitted = await engine.answer(user.api_key, intervention_id, answer.strip())
            if submitted.outcome == "accepted":
                return to("/interventions?notice=sent")
            if submitted.outcome == "invalid":
                return await detail(user, intervention_id, 422, "invalid")
            if submitted.outcome == "not_found":
                return page(
                    "message.html",
                    404,
                    heading="Not found",
                    text="There is no such request for you.",
                    csrf=user.csrf,
                )
            return to(f"/interventions?notice={submitted.outcome}")
        except NotSignedIn as refused:
            return sign_out(request, refusal(refused))
        except EngineUnavailable:
            return to("/interventions?notice=unavailable")

    return app
