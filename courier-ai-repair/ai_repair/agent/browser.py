"""A headless browser the agent drives, fenced in by ``Net``.

Every request a page makes passes ``_gate``: documents, form posts and scripted calls only
to approved addresses, static parts (scripts, styles, pictures) also from public hosts.
Requests to approved addresses are noted down, because what the agent is after is exactly
that: which request, with which fields, brings the tracking answer. The adapter it writes
repeats that request without a browser.

The browser process is started with an empty environment and a fresh profile, so no
secret of the service and nothing from an earlier task is within its reach.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .net import Net, NetRefused

_DESCRIBE = """() => {
  const cut = (s, n) => (s || '').replace(/\\s+/g, ' ').trim().slice(0, n);
  const field = (e) => ({
    tag: e.tagName.toLowerCase(), type: e.getAttribute('type') || '', name: e.getAttribute('name') || '',
    id: e.id || '', value: cut(e.value, 60), placeholder: cut(e.getAttribute('placeholder'), 40),
    text: cut(e.innerText, 40),
  });
  const forms = [...document.forms].slice(0, 8).map((f) => ({
    action: f.getAttribute('action') || '', method: (f.getAttribute('method') || 'get').toLowerCase(),
    id: f.id || '', fields: [...f.querySelectorAll('input,select,textarea,button')].slice(0, 40).map(field),
  }));
  const loose = [...document.querySelectorAll('input,select,textarea,button')]
    .filter((e) => !e.form).slice(0, 20).map(field);
  const links = [...document.querySelectorAll('a[href]')].slice(0, 40)
    .map((a) => ({ text: cut(a.innerText, 40), href: a.getAttribute('href') }))
    .filter((a) => a.text);
  const pictures = [...document.querySelectorAll('img')].slice(0, 10)
    .map((i) => ({ alt: cut(i.alt, 40), src: cut(i.getAttribute('src'), 120), id: i.id || '' }));
  return { title: document.title, text: cut(document.body ? document.body.innerText : '', 3000),
           forms, loose, links, pictures };
}"""


class BrowserUnavailable(Exception):
    """The browser cannot be started on this machine. The text says how to install it."""


def _field_line(field: dict[str, str]) -> str:
    parts = [field["tag"]]
    for name in ("type", "name", "id", "value", "placeholder", "text"):
        if field.get(name):
            parts.append(f'{name}="{field[name]}"')
    return "    " + " ".join(parts)


class Browser:
    def __init__(self, net: Net, *, timeout_s: float = 30.0) -> None:
        self._net, self._timeout_ms = net, int(timeout_s * 1000)
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._requests: list[str] = []
        self._refused: list[str] = []

    # ------------------------------------------------------------------ start and stop

    def _start(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError:
            raise BrowserUnavailable("the playwright package is not installed (run: uv sync)") from None
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True, env={})
        except Error as error:
            self.close()
            raise BrowserUnavailable(
                "the browser could not be started (run: uv run playwright install chromium-headless-shell): "
                + str(error).splitlines()[0][:200]
            ) from None
        context = self._browser.new_context(accept_downloads=False, service_workers="block")
        context.set_default_timeout(self._timeout_ms)
        context.route("**/*", self._gate)
        context.on("response", self._note)
        self._page = context.new_page()
        return self._page

    def close(self) -> None:
        for thing, method in ((self._browser, "close"), (self._playwright, "stop")):
            if thing is not None:
                with contextlib.suppress(Exception):
                    getattr(thing, method)()
        self._playwright = self._browser = self._page = None

    # ------------------------------------------------------------------ the fence

    def _gate(self, route: Any) -> None:
        request = route.request
        if self._net.static_allowed(request.url, request.resource_type):
            route.continue_()
        else:
            if len(self._refused) < 20:
                self._refused.append(f"{request.method} {request.url[:160]}")
            route.abort()

    def _note(self, response: Any) -> None:
        request = response.request
        if request.resource_type in ("document", "xhr", "fetch") and self._net.allowed(request.url):
            line = f"{request.method} {request.url[:300]} -> {response.status}"
            body = request.post_data
            if body:
                line += f"\n      sent: {body[:1500]}"
            if len(self._requests) < 200:
                self._requests.append(line)

    # ------------------------------------------------------------------ what the agent can do

    def _act(self, what: Any) -> str:
        from playwright.sync_api import Error

        try:
            what()
            self._page.wait_for_load_state("networkidle", timeout=min(self._timeout_ms, 8000))
        except Error as error:
            first = str(error).splitlines()[0][:300]
            if "networkidle" not in first and "Timeout" not in first:
                return f"The browser reports: {first}\n\n{self.describe()}"
        return self.describe()

    def open(self, url: str) -> str:
        self._net.spend(url)
        page = self._start()
        return self._act(lambda: page.goto(url, wait_until="domcontentloaded"))

    def click(self, selector: str) -> str:
        page = self._require_page()
        self._net.spend(page.url)
        return self._act(lambda: page.click(selector))

    def type(self, selector: str, text: str) -> str:
        page = self._require_page()
        from playwright.sync_api import Error

        try:
            page.fill(selector, text)
        except Error as error:
            return f"The browser reports: {str(error).splitlines()[0][:300]}"
        return f"typed into {selector}"

    def select(self, selector: str, value: str) -> str:
        page = self._require_page()
        from playwright.sync_api import Error

        try:
            page.select_option(selector, value)
        except Error as error:
            return f"The browser reports: {str(error).splitlines()[0][:300]}"
        return f"selected {value} in {selector}"

    def _require_page(self) -> Any:
        if self._page is None:
            raise NetRefused("no page is open yet: use open_page first")
        return self._page

    def html(self, selector: str | None = None, limit: int = 20_000) -> str:
        """The page's HTML as it is now, or of one element."""
        page = self._require_page()
        from playwright.sync_api import Error

        try:
            text: str = (
                page.locator(selector).first.evaluate("e => e.outerHTML") if selector else page.content()
            )
        except Error as error:
            return f"The browser reports: {str(error).splitlines()[0][:300]}"
        return text if len(text) <= limit else text[:limit] + "\n[... cut ...]"

    def content(self) -> str:
        """The whole page, uncut (for saving as a fixture)."""
        return str(self._require_page().content())

    def describe(self) -> str:
        """What a person would see and could use on the page, and the requests made so far."""
        page = self._require_page()
        from playwright.sync_api import Error

        try:
            seen: dict[str, Any] = page.evaluate(_DESCRIBE)
        except Error as error:
            return f"The page could not be read: {str(error).splitlines()[0][:300]}"
        lines = [f"URL: {page.url}", f"Title: {seen['title']}", "", "Text on the page:", seen["text"], ""]
        for number, form in enumerate(seen["forms"], 1):
            lines.append(
                f'Form {number}: method={form["method"]} action="{form["action"]}" id="{form["id"]}"'
            )
            lines += [_field_line(field) for field in form["fields"]]
        if seen["loose"]:
            lines.append("Inputs and buttons outside a form:")
            lines += [_field_line(field) for field in seen["loose"]]
        if seen["pictures"]:
            lines.append("Pictures: " + "; ".join(f'{p["src"]} (alt="{p["alt"]}")' for p in seen["pictures"]))
        if seen["links"]:
            lines.append("Links: " + "; ".join(f"{a['text']} -> {a['href']}" for a in seen["links"]))
        if self._requests:
            lines += ["", "Requests the browser made to the approved addresses since the last look:"]
            lines += ["  " + line for line in self._requests[-25:]]
            self._requests.clear()
        if self._refused:
            lines += ["", "Refused (not approved addresses): " + "; ".join(self._refused[:8])]
            self._refused.clear()
        return "\n".join(lines)
