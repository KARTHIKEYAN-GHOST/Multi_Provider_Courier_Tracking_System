"""Where the agent's browser and HTTP client may go, and how often.

The agent reads pages written by strangers and is steered by a model, so its reach is
fixed here in code: the approved hosts of the adapter it works on, nothing else. An
address on this machine or in a private network is reachable only with the exact port the
adapter itself names; otherwise "127.0.0.1" being approved for the demo site would open
every local service (the Engine's internal API, the database, the broker).
"""

from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

_ORIGIN = re.compile(r"https?://([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::(\d{1,5}))?")
#: Page parts a site may load from other public hosts (libraries, fonts, pictures).
STATIC_KINDS = frozenset({"script", "stylesheet", "image", "font", "media"})


class NetRefused(Exception):
    """The address is outside what the agent may reach, or its allowance is used up."""


def origins_in(text: str) -> set[tuple[str, int | None]]:
    """(host, port) of the web addresses written in a text; port None = the scheme's own."""
    return {(host.lower(), int(port) if port else None) for host, port in _ORIGIN.findall(text)}


def _local(host: str) -> bool:
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return not address.is_global


class Net:
    def __init__(
        self,
        hosts: set[str],
        origins: set[tuple[str, int | None]] | None = None,
        *,
        max_requests: int = 60,
        pause_s: float = 1.0,
    ) -> None:
        self.hosts = {host.lower() for host in hosts}
        self._origins = origins or set()
        self._left, self._pause_s, self._last = max_requests, pause_s, 0.0

    def allowed(self, url: str) -> bool:
        """May the agent itself ask for this address (a page, a form, an API call)?"""
        try:
            parts = urlsplit(url)
            host, port = (parts.hostname or "").lower(), parts.port
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
            return False
        if host not in self.hosts:
            return False
        return not _local(host) or (host, port) in self._origins

    def static_allowed(self, url: str, kind: str) -> bool:
        """May a page load this part? Approved hosts, and static parts from public hosts."""
        if self.allowed(url):
            return True
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        return kind in STATIC_KINDS and parts.scheme == "https" and bool(host) and not _local(host)

    def spend(self, url: str) -> None:
        """Counts one request of the agent and keeps a polite distance to the last one."""
        if not self.allowed(url):
            raise NetRefused(f"not an approved address: {url[:200]}")
        if self._left <= 0:
            raise NetRefused("the allowance of requests for this task is used up")
        self._left -= 1
        host = (urlsplit(url).hostname or "").lower()
        wait = self._last + self._pause_s - time.monotonic()
        if wait > 0 and not _local(host):
            time.sleep(wait)
        self._last = time.monotonic()
