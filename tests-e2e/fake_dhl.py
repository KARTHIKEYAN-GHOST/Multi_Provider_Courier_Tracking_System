"""A stand-in for DHL's tracking API, so the DHL adapter can be tested without the internet.

It answers ``GET /track/shipments?trackingNumber=...`` in DHL's format and, like DHL, accepts
only requests that carry the right ``DHL-API-Key`` header. It records what it was asked.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def _shipment(number: str, codes: list[str]) -> dict[str, object]:
    events = [
        {
            "timestamp": f"2026-03-0{day + 1}T10:00:00Z",
            "statusCode": code,
            "status": code.upper(),
            "description": f"Shipment is {code}",
            "location": {"address": {"countryCode": "IN", "addressLocality": "Chennai"}},
        }
        for day, code in enumerate(codes)
    ]
    return {"shipments": [{"id": number, "service": "express", "status": events[-1], "events": events[::-1]}]}


class FakeDhl:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.requests: list[tuple[float, str, str | None]] = []  # (time, tracking number, key presented)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                import time

                url = urlsplit(self.path)
                number = parse_qs(url.query).get("trackingNumber", [""])[0]
                presented = self.headers.get("DHL-API-Key")
                fake.requests.append((time.time(), number, presented))
                if url.path != "/track/shipments":
                    return self._send(404, {"title": "Not found"})
                if presented != fake.api_key:
                    return self._send(401, {"title": "Unauthorized", "detail": "Invalid API key"})
                if "DELIVERED" in number:
                    return self._send(200, _shipment(number, ["pre-transit", "transit", "delivered"]))
                if "TRANSIT" in number:
                    return self._send(200, _shipment(number, ["pre-transit", "transit"]))
                return self._send(404, {"title": "No result found", "status": 404})

            def _send(self, status: int, body: dict[str, object]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
