"""A tiny HTTP server for the demo courier website (standard library only)."""

from __future__ import annotations

import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .pages import render

_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoCourier/1.0"

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if url.path != "/track":
            return self._send(404, "<p>Page not found.</p>")
        number = parse_qs(url.query).get("number", [""])[0]
        if not _NUMBER.match(number):
            return self._send(400, "<p>Enter a tracking number.</p>")
        page = render(number)
        self._send(page.status, page.body, headers=page.headers)

    def _send(
        self,
        status: int,
        body: str,
        content_type: str = "text/html; charset=utf-8",
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        data = body.encode()
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
