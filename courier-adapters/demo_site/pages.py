"""Pages of the demo courier website.

A deliberately ordinary tracking page: a status headline and a history table with local
(IST) times, the way many courier sites present it. The prefix of the tracking number
selects what the site does, so the scraping adapter can be exercised against every case
a real site produces - including a redesigned page.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape

IST = timezone(timedelta(hours=5, minutes=30), "IST")
_STEPS = [
    ("Booked", "Chennai, IN", "Shipment booked at origin"),
    ("Picked up", "Chennai, IN", "Collected by courier"),
    ("In transit", "Bengaluru, IN", "Arrived at sorting hub"),
    ("Out for delivery", "Coimbatore, IN", "With delivery agent"),
    ("Delivered", "Coimbatore, IN", "Delivered to consignee"),
]


@dataclass(frozen=True)
class Page:
    status: int
    body: str
    headers: tuple[tuple[str, str], ...] = ()


def _history(number: str, steps: int) -> list[tuple[datetime, str, str, str]]:
    """A fixed history: the same number always gives the same page."""
    seed = int.from_bytes(hashlib.sha256(number.encode()).digest()[:4])
    first = datetime(2026, 1, 1, 9, 0, tzinfo=IST) + timedelta(days=seed % 250, minutes=seed % 480)
    return [(first + timedelta(hours=11 * i), *_STEPS[i]) for i in range(steps)]


def _layout(title: str, content: str) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        f"<title>{escape(title)} - Demo Courier</title>\n"
        "<script>window.analytics = {page: 'track'};</script>\n"
        "<style>table{border-collapse:collapse} td,th{border:1px solid #ccc;padding:4px}</style>\n"
        '</head>\n<body>\n<header><h1>Demo Courier</h1><nav><a href="/">Home</a></nav></header>\n'
        f"<main>\n{content}\n</main>\n<footer>&copy; Demo Courier. For testing only.</footer>\n</body>\n</html>\n"
    )


def _tracking_page(number: str, steps: int, *, date_format: str = "%d %b %Y, %H:%M IST") -> str:
    history = _history(number, steps)
    rows = "\n".join(
        f'      <tr><td class="when">{when.strftime(date_format)}</td><td class="status">{escape(status)}</td>'
        f'<td class="place">{escape(place)}</td><td class="note">{escape(note)}</td></tr>'
        for when, status, place, note in history
    )
    return _layout(
        f"Tracking {number}",
        f'<section id="shipment" data-number="{escape(number)}">\n'
        f'  <h2>Shipment <span class="number">{escape(number)}</span></h2>\n'
        f'  <p>Current status: <strong class="current-status">{escape(history[-1][1])}</strong></p>\n'
        '  <table id="history">\n    <thead><tr><th>Date</th><th>Status</th><th>Location</th><th>Details</th></tr></thead>\n'
        f"    <tbody>\n{rows}\n    </tbody>\n  </table>\n</section>",
    )


def _redesigned_page(number: str) -> str:
    """The same information after a redesign: no table, different class names."""
    cards = "\n".join(
        f'  <li class="tl-item"><time>{when.isoformat()}</time><b>{escape(status)}</b><i>{escape(place)}</i></li>'
        for when, status, place, _ in _history(number, 3)
    )
    return _layout(
        f"Tracking {number}", f'<div class="tracking-v2"><ul class="timeline">\n{cards}\n</ul></div>'
    )


def render(number: str) -> Page:
    """The site's answer for a tracking number."""
    kind = number.upper().removeprefix("DEMO-").split("-", 1)[0]
    if kind == "OK":
        return Page(200, _tracking_page(number, 5))
    if kind == "TRANSIT":
        return Page(200, _tracking_page(number, 3))
    if kind == "BOOKED":
        return Page(200, _tracking_page(number, 1))
    if kind == "BUSY":
        return Page(503, _layout("Unavailable", "<p>The service is temporarily unavailable.</p>"))
    if kind == "LIMIT":
        return Page(429, _layout("Slow down", "<p>Too many requests.</p>"), (("Retry-After", "2"),))
    if kind == "BLOCKED":
        return Page(403, _layout("Access denied", "<p>Automated access is not permitted.</p>"))
    if kind == "NEWLAYOUT":
        return Page(200, _redesigned_page(number))
    if kind == "BADDATE":
        return Page(200, _tracking_page(number, 2, date_format="%A week %W"))
    # Like many sites: "not found" is a normal page with status 200.
    message = f'<p class="not-found">No shipment found for <span class="number">{escape(number)}</span>.</p>'
    return Page(200, _layout("Not found", f'<section id="shipment">\n  {message}\n</section>'))
