"""A tiny HTTP server for the demo courier website (standard library only)."""

from __future__ import annotations

import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .pages import captcha_image, inbox, render, verify

_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_CAPTCHA = re.compile(r"^/captcha/([0-9a-f]{32})\.png$")
_MAX_FORM_BYTES = 2048


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoCourier/1.0"

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if picture := _CAPTCHA.match(url.path):
            image = captcha_image(picture.group(1))
            if image is None:
                return self._send(404, "<p>Page not found.</p>")
            return self._send(200, image, "image/png", (("Cache-Control", "no-store"),))
        if url.path not in ("/track", "/inbox"):
            return self._send(404, "<p>Page not found.</p>")
        number = parse_qs(url.query).get("number", [""])[0]
        if not _NUMBER.match(number):
            return self._send(400, "<p>Enter a tracking number.</p>")
        page = render(number) if url.path == "/track" else inbox(number)
        self._send(page.status, page.body, headers=page.headers)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/verify":
            return self._send(404, "<p>Page not found.</p>")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(400, "<p>Bad request.</p>")
        if not 0 < length <= _MAX_FORM_BYTES:
            return self._send(400, "<p>Bad request.</p>")
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        page = verify(form.get("token", [""])[0], form.get("answer", [""])[0])
        self._send(page.status, page.body, headers=page.headers)

    def _send(
        self,
        status: int,
        body: str | bytes,
        content_type: str = "text/html; charset=utf-8",
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        """Quiet: this is a test fixture."""


def make_server(host: str = "127.0.0.1", port: int = 8090) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)
