"""The actions the agent can take, and nothing else.

Each action returns a text the model reads next. Everything that came from a web page or a
server is returned inside a marked block: it is material, not instructions. What the agent
can change is one adapter's files in a throwaway clone; whether that work is acceptable is
decided afterwards by ``verify`` (the guard and the sandboxed checks), not by the agent.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

from .. import guard
from ..guard import TESTS
from ..scrub import scrub
from .actions import Action, BadAction
from .browser import Browser, BrowserUnavailable
from .net import Net, NetRefused

_FIXTURE_NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")
_MAX_FIXTURES, _MAX_FIXTURE_CHARS, _MAX_FILE_CHARS = 5, 400_000, 60_000
_READABLE = ("courier-adapters/adapters/", "courier-adapters/tests/", "courier-adapters/courier_adapter_sdk/")
_SENT_HEADERS = frozenset({"accept", "content-type", "x-requested-with", "referer", "origin"})

DESCRIPTION = """\
open_page    {"url": "..."}                      open an approved address in the browser
click        {"selector": "<CSS selector>"}       click a button or link on the open page
type         {"selector": "...", "text": "..."}   fill an input of the open page
select       {"selector": "...", "value": "..."}  choose an option of a select box
look         {}                                   describe the open page again, with new requests
page_html    {"selector": "..."}                  HTML of the open page (selector optional)
http         {"method": "GET|POST", "url": "...", "form": {...}, "json": {...}, "headers": {...}}
                                                  one request without the browser (form/json/headers optional)
save_fixture {"name": "result_page", "source": "page|http", "tracking_number": "..."}
                                                  keep the open page, or the last http answer, as a test
                                                  fixture; give the consignment number you used, it is removed
list_files   {}                                   the adapter's files and fixtures
read_file    {"path": "..."}                      a file under courier-adapters/
write_file   {"path": "...", "content": "..."}    create or replace one of the adapter's files (whole file)
run_checks   {}                                   code rules, ruff, mypy and the adapter's tests
finish       {"summary": "<one sentence>"}        hand the work in; it is accepted only if the checks pass"""


class Files(Protocol):
    """The part of the workspace the tools use."""

    repo: Path

    def read(self, path: str) -> str | None: ...
    def write(self, path: str, content: str) -> None: ...


def untrusted(title: str, text: str) -> str:
    return f"<<<UNTRUSTED DATA: {title}>>>\n{text}\n<<<END OF UNTRUSTED DATA>>>"


class Toolbox:
    def __init__(
        self,
        files: Files,
        key: str,
        net: Net,
        verify: Callable[[list[str]], str | None],
        *,
        browser: Browser | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._files, self._key, self._net, self._verify = files, key, net, verify
        self._browser = browser
        self._http = httpx.Client(timeout=30.0, follow_redirects=False, transport=transport, trust_env=False)
        self._last_http: tuple[str, str] | None = None  # content type, body
        #: Fixtures saved by the agent: file name -> path. They are part of the work.
        self.fixtures: dict[str, str] = {}
        self.summary = ""

    def close(self) -> None:
        self._http.close()
        if self._browser is not None:
            self._browser.close()

    # ------------------------------------------------------------------ dispatch

    def run(self, action: Action) -> tuple[str, bool]:
        """Does one action. Returns what the model reads next, and whether the work is done."""
        handler = getattr(self, f"_do_{action.name}", None)
        if handler is None:
            raise BadAction(f'there is no action "{action.name}"')
        try:
            result = handler(action)
        except (NetRefused, BrowserUnavailable) as error:
            return f"Refused: {error}", False
        if action.name == "finish":
            return result, result == ""
        return result, False

    # ------------------------------------------------------------------ browser

    def _need_browser(self) -> Browser:
        if self._browser is None:
            raise BrowserUnavailable("this task runs without a browser; use the http action")
        return self._browser

    def _do_open_page(self, action: Action) -> str:
        return untrusted("the page", self._need_browser().open(action.text("url")))

    def _do_click(self, action: Action) -> str:
        return untrusted(
            "the page after the click", self._need_browser().click(action.text("selector", limit=300))
        )

    def _do_type(self, action: Action) -> str:
        text = action.text("text", default="", limit=200)
        return self._need_browser().type(action.text("selector", limit=300), text)

    def _do_select(self, action: Action) -> str:
        return self._need_browser().select(
            action.text("selector", limit=300), action.text("value", limit=200)
        )

    def _do_look(self, action: Action) -> str:
        return untrusted("the page", self._need_browser().describe())

    def _do_page_html(self, action: Action) -> str:
        selector = action.args.get("selector")
        html = self._need_browser().html(selector if isinstance(selector, str) and selector.strip() else None)
        return untrusted("HTML of the page", html)

    # ------------------------------------------------------------------ plain HTTP

    def _do_http(self, action: Action) -> str:
        method = action.text("method", default="GET", limit=10).upper()
        if method not in ("GET", "POST"):
            raise BadAction("the http action supports GET and POST")
        url = action.text("url")
        form, body, headers = action.args.get("form"), action.args.get("json"), action.args.get("headers")
        if form is not None and not isinstance(form, dict):
            raise BadAction('"form" must be an object of field names and values')
        sent = {
            str(name): str(value)[:500]
            for name, value in (headers.items() if isinstance(headers, dict) else [])
            if str(name).lower() in _SENT_HEADERS
        }
        self._net.spend(url)
        try:
            response = self._http.request(
                method, url, headers=sent,
                data={str(k): str(v) for k, v in form.items()} if form else None,
                json=body if body is not None and not form else None,
            )  # fmt: skip
        except httpx.HTTPError as error:
            return f"The request failed: {type(error).__name__}"
        text = response.text
        self._last_http = (response.headers.get("content-type", ""), text)
        lines = [f"HTTP {response.status_code}", f"content-type: {response.headers.get('content-type', '')}"]
        if "location" in response.headers:
            lines.append(f"location: {response.headers['location'][:300]} (redirects are not followed)")
        if "set-cookie" in response.headers:
            lines.append("the answer sets cookies: " + ", ".join(sorted(response.cookies.keys()))[:300])
        shown = text if len(text) <= 15_000 else text[:15_000] + "\n[... cut ...]"
        return untrusted("the server's answer", "\n".join(lines) + "\n\n" + shown)

    # ------------------------------------------------------------------ fixtures

    def _do_save_fixture(self, action: Action) -> str:
        name = action.text("name", limit=40)
        if not _FIXTURE_NAME.fullmatch(name):
            raise BadAction("a fixture name is lowercase letters, digits and underscores")
        source = action.text("source", default="page", limit=10)
        number = action.args.get("tracking_number")
        number = number if isinstance(number, str) and number.strip() else None
        if source == "http":
            if self._last_http is None:
                raise BadAction("there is no http answer to save yet")
            kind, body = self._last_http
        elif source == "page":
            kind, body = "text/html", self._need_browser().content()
        else:
            raise BadAction('"source" is "page" or "http"')
        if len(body) > _MAX_FIXTURE_CHARS:
            raise BadAction("the answer is too large to keep as a fixture")
        try:
            parsed: Any = json.loads(body) if "json" in kind else None
        except ValueError:
            parsed = None
        if parsed is not None:
            file, text = (
                f"{name}.json",
                json.dumps(scrub(parsed, number), indent=2, ensure_ascii=False) + "\n",
            )
        else:
            suffix = "html" if "<" in body[:2000] else "txt"
            file, text = f"{name}.{suffix}", str(scrub(body, number)).rstrip("\n") + "\n"
        if file not in self.fixtures and len(self.fixtures) >= _MAX_FIXTURES:
            raise BadAction(f"at most {_MAX_FIXTURES} fixtures per task")
        path = f"{TESTS}/fixtures/{self._key}/{file}"
        self._files.write(path, text)
        self.fixtures[file] = path
        note = " The consignment number in it is now TRACKING0001." if number else ""
        return (
            f"saved as {path} ({len(text)} characters).{note} Read it in your tests with "
            f'Path(__file__).parent / "fixtures" / "{self._key}" / "{file}".'
        )

    # ------------------------------------------------------------------ files

    def _own(self) -> list[str]:
        root = self._files.repo
        found = [str(p.relative_to(root)) for p in sorted((root / guard.ADAPTERS / self._key).glob("*.py"))]
        found += [path for path in guard.test_paths(self._key) if (root / path).is_file()]
        found += [str(p.relative_to(root)) for p in sorted((root / TESTS / "fixtures" / self._key).glob("*"))]
        return found

    def _do_list_files(self, action: Action) -> str:
        own = self._own()
        return "The adapter's files:\n" + "\n".join(own) if own else "The adapter has no files yet."

    def _do_read_file(self, action: Action) -> str:
        path = action.text("path", limit=300).strip().lstrip("/")
        if ".." in Path(path).parts or not path.startswith(_READABLE):
            raise BadAction("only files under courier-adapters/ (adapters, tests, the SDK) can be read")
        content = self._files.read(path)
        if content is None:
            return f"There is no file {path}."
        return f"=== {path} ===\n" + (
            content if len(content) <= 30_000 else content[:30_000] + "\n[... cut ...]"
        )

    def _do_write_file(self, action: Action) -> str:
        path = action.text("path", limit=300).strip().lstrip("/")
        content = action.text("content", default="", limit=_MAX_FILE_CHARS)
        if guard.kind_of(path, self._key) is None:
            allowed = f"{guard.ADAPTERS}/{self._key}/*.py, " + ", ".join(guard.test_paths(self._key))
            return f"Not written: only these files may be written: {allowed}"
        try:
            ast.parse(content)
        except SyntaxError as error:
            return f"Not written: the content is not valid Python (line {error.lineno}: {error.msg})"
        self._files.write(path, content if content.endswith("\n") else content + "\n")
        return f"written: {path} ({len(content.splitlines())} lines)"

    # ------------------------------------------------------------------ checks and the end

    def _do_run_checks(self, action: Action) -> str:
        problem = self._verify(sorted(self.fixtures))
        return "All checks passed. Use finish to hand the work in." if problem is None else problem

    def _do_finish(self, action: Action) -> str:
        summary = action.text("summary", default="no summary given", limit=1000)
        problem = self._verify(sorted(self.fixtures))
        if problem is not None:
            return "Not finished, the checks do not pass yet:\n" + problem
        self.summary = summary.strip()[:300] or "no summary given"
        return ""
