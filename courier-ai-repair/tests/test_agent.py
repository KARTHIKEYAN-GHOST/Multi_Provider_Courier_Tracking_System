"""The agent: its action format, its fence, its tools, and whole tasks done in steps.

The model is a stand-in that answers from a script, so the machinery is tested without an
AI service. The browser is the real one, against the demo courier website started locally.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_repair import guard
from ai_repair.agent import Agent, AgentGaveUp, BadAction, Net, Toolbox, parse_action
from ai_repair.agent.browser import Browser, BrowserUnavailable
from ai_repair.agent.loop import _shortened
from ai_repair.agent.net import NetRefused, origins_in
from ai_repair.config import Settings
from ai_repair.engine import Task
from ai_repair.guard import ADAPTERS, TESTS
from ai_repair.llm import ModelError
from ai_repair.sandbox import Bubblewrap
from ai_repair.service import Repairer

from .conftest import ROOT, VENV, git
from .test_service import (
    FIXTURE,
    ScriptedModel,
    repair_task,
)


@contextlib.contextmanager
def patch_resolution(names: dict[str, str]) -> Iterator[None]:
    """Name resolution answering from ``names``, for the duration of the block."""
    from ai_repair.agent.net import _resolves_local

    def fake(host: str, *args: object, **kwargs: object) -> list[tuple[Any, ...]]:
        if host not in names:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (names[host], 0))]

    _resolves_local.cache_clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "getaddrinfo", fake)
        yield
    _resolves_local.cache_clear()


def act(name: str, /, **arguments: object) -> str:
    return json.dumps({"action": name, **arguments})


# ----------------------------------------------------------------------------- the action format


def test_an_action_is_found_in_whatever_the_model_wraps_it_in() -> None:
    assert parse_action('{"action": "look"}').name == "look"
    wrapped = parse_action(
        'Sure! {"note": 1}\n```json\n{"action":"open_page","url":"https://a.example/x"}\n```'
    )
    assert (wrapped.name, wrapped.args) == ("open_page", {"url": "https://a.example/x"})
    nested = parse_action('{"tool": "click", "args": {"selector": "#go"}}')
    assert (nested.name, nested.text("selector")) == ("click", "#go")
    # A file may follow in a fenced block: no escaping needed.
    written = parse_action('{"action": "write_file", "path": "a.py"}\n```python\nx = "a\\nb"\n\ny = 2\n```')
    assert written.args["content"] == 'x = "a\\nb"\n\ny = 2'
    for bad in ("no object here", '{"summary": "x"}', '{"action": 5}', '{"action": "rm -rf"}'):
        with pytest.raises(BadAction):
            parse_action(bad)
    with pytest.raises(BadAction, match="needs a text argument"):
        parse_action('{"action": "open_page"}').text("url")


# ----------------------------------------------------------------------------- the fence


def test_the_agent_reaches_only_approved_addresses() -> None:
    net = Net({"www.courier.example", "127.0.0.1"}, {("127.0.0.1", 8090)}, pause_s=0)
    assert net.allowed("https://www.courier.example/track?id=1")
    assert net.allowed("http://127.0.0.1:8090/track")
    refused = [
        "https://other.example/",  # not approved
        "https://www.courier.example.evil.net/",  # looks alike
        "https://user:pw@www.courier.example/",  # credentials in the address
        "file:///etc/passwd",
        "ftp://www.courier.example/",
        "http://127.0.0.1:8081/internal/v1/repairs/claim",  # approved host, another local port
        "http://127.0.0.1/",
        "http://localhost:8090/",
        "http://[::1]:8090/",
        "not an address",
    ]
    for url in refused:
        assert not net.allowed(url), url
        with pytest.raises(NetRefused):
            net.spend(url)
    # Parts of a page: libraries and pictures from public hosts, never from this machine.
    assert net.static_allowed("https://cdn.example.net/jquery.js", "script")
    assert not net.static_allowed("https://cdn.example.net/collect", "xhr")
    assert not net.static_allowed("https://cdn.example.net/next", "document")
    assert not net.static_allowed("http://127.0.0.1:5672/x.js", "script")
    assert not net.static_allowed("http://169.254.169.254/latest/meta-data", "image")
    # A public-looking name that points into this machine is refused like the address itself.
    inward = Net({"sneaky.example", "localhost.example"}, pause_s=0)
    with patch_resolution({"sneaky.example": "10.1.2.3", "localhost.example": "93.184.215.14"}):
        assert not inward.allowed("https://sneaky.example/")
        assert not inward.static_allowed("https://sneaky.example/x.js", "script")
        assert inward.allowed("https://localhost.example/")
    assert origins_in('A = "http://127.0.0.1:8090"\nB = "https://x.example/api"') == {
        ("127.0.0.1", 8090),
        ("x.example", None),
    }


def test_a_courier_address_on_this_machine_is_reachable_only_when_written_as_one() -> None:
    # The operator put a courier on this machine into the database: its exact origin is reachable.
    operator = Net({"127.0.0.1"}, {("127.0.0.1", 8090)}, pause_s=0)
    assert operator.allowed("http://127.0.0.1:8090/track?number=1")
    assert not operator.allowed("http://127.0.0.1:8081/internal/v1/repairs/claim"), "only that port"
    # A user of the API registered a public-looking name that points here, with the Engine's
    # internal port: never reachable, although it is the courier's own tracking address.
    named = Net({"courier.attacker.example"}, {("courier.attacker.example", 8081)}, pause_s=0)
    with patch_resolution({"courier.attacker.example": "127.0.0.1"}):
        assert not named.allowed("http://courier.attacker.example:8081/internal/v1/repairs/claim")
    with patch_resolution({"courier.attacker.example": "93.184.215.14"}):
        assert named.allowed("http://courier.attacker.example:8081/track")


def test_the_allowance_of_requests_runs_out() -> None:
    net = Net({"a.example"}, max_requests=2, pause_s=0)
    net.spend("https://a.example/1")
    net.spend("https://a.example/2")
    with pytest.raises(NetRefused, match="used up"):
        net.spend("https://a.example/3")


# ----------------------------------------------------------------------------- the browser


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    """The demo courier website of courier-adapters, on a free local port."""
    if not (VENV / "bin" / "python").exists():
        pytest.skip("courier-adapters/.venv is missing")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [str(VENV / "bin" / "python"), "-m", "demo_site"],
        cwd=ROOT / "courier-adapters", env={**os.environ, "DEMO_SITE_PORT": str(port)},
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )  # fmt: skip
    try:
        assert process.stdout is not None
        assert "demo courier website" in process.stdout.readline()
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=10)


def local_net(site: str) -> Net:
    return Net({"127.0.0.1"}, origins_in(site), pause_s=0)


def test_the_browser_shows_the_page_and_stays_inside_the_fence(site: str) -> None:
    browser = Browser(local_net(site))
    try:
        try:
            seen = browser.open(f"{site}/track?number=DEMO-OK-1")
        except BrowserUnavailable as error:
            pytest.skip(str(error))
        assert "Title: Tracking DEMO-OK-1" in seen
        assert "Delivered" in seen
        assert f"GET {site}/track?number=DEMO-OK-1 -> 200" in seen, "the request is noted for the agent"
        assert "<table" in browser.html()
        assert "Delivered" in browser.html("table")
        # Another local port is refused although the host is approved.
        with pytest.raises(NetRefused):
            browser.open("http://127.0.0.1:1/")
        # A verification page: the form and its fields are described.
        form = browser.open(f"{site}/track?number=DEMO-OTP-1")
        assert "Form 1: method=post" in form
        assert 'name="answer"' in form
        assert 'type="hidden" name="token"' in form, "hidden fields are what an adapter must send back"
        assert browser.type('[name="answer"]', "000000") == 'typed into [name="answer"]'
        after = browser.click("form button")
        assert "POST" in after, "the form post is noted"
        assert "sent:" in after
        assert "answer=000000" in after
    finally:
        browser.close()


# ----------------------------------------------------------------------------- the tools


class Folder:
    """A plain directory with the workspace's file interface."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def read(self, path: str) -> str | None:
        file = self.repo / path
        return file.read_text() if file.is_file() else None

    def write(self, path: str, content: str) -> None:
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(content)


def toolbox(tmp_path: Path, handler: object = None, problem: str | None = None) -> Toolbox:
    transport = httpx.MockTransport(handler) if callable(handler) else None
    net = Net({"api.acme.example"}, pause_s=0)
    return Toolbox(Folder(tmp_path), "acme", net, lambda saved: problem, transport=transport)


def run(tools: Toolbox, reply: str) -> str:
    return tools.run(parse_action(reply))[0]


def test_files_are_written_only_where_the_adapter_lives(tmp_path: Path) -> None:
    tools = toolbox(tmp_path)
    assert run(tools, act("list_files")) == "The adapter has no files yet."
    good = f"{ADAPTERS}/acme/adapter.py"
    assert run(tools, act("write_file", path=good, content="x = 1")).startswith("written:")
    assert (tmp_path / good).read_text() == "x = 1\n"
    for path in (
        f"{ADAPTERS}/other/adapter.py",
        f"{ADAPTERS}/acme/../india.py",
        "courier-adapters/courier_adapter_sdk/worker.py",
        ".github/workflows/ci.yml",
        f"{TESTS}/test_other_adapter.py",
        f"{TESTS}/fixtures/acme/page.html",
    ):
        assert run(tools, act("write_file", path=path, content="x = 1")).startswith("Not written"), path
    assert not (tmp_path / ADAPTERS / "india.py").exists()
    assert "not valid Python" in run(tools, act("write_file", path=good, content="def broken(:"))
    assert (tmp_path / good).read_text() == "x = 1\n", "the last valid content stays"
    assert good in run(tools, act("list_files"))
    assert "x = 1" in run(tools, act("read_file", path=good))
    for path in ("../../etc/passwd", ".env", "courier-engine/src/config.rs", f"{ADAPTERS}/../../.env"):
        with pytest.raises(BadAction):
            run(tools, act("read_file", path=path))
    with pytest.raises(BadAction, match="no action"):
        run(tools, act("delete_everything"))


def test_the_http_action_asks_approved_hosts_and_keeps_answers_as_scrubbed_fixtures(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"awb": "AB123456", "status": "Delivered", "phone": "9876543210"})

    tools = toolbox(tmp_path, server)
    answer = run(
        tools,
        act("http", method="post", url="https://api.acme.example/track", form={"awb": "AB123456"},
            headers={"Authorization": "Bearer stolen", "X-Requested-With": "XMLHttpRequest"}),
    )  # fmt: skip
    assert answer.startswith("<<<UNTRUSTED DATA")
    assert "HTTP 200" in answer
    assert seen[0].content == b"awb=AB123456"
    assert "authorization" not in seen[0].headers, "only harmless headers are passed on"
    assert seen[0].headers["x-requested-with"] == "XMLHttpRequest"
    assert run(tools, act("http", url="https://elsewhere.example/")).startswith("Refused")
    assert len(seen) == 1

    saved = run(tools, act("save_fixture", name="delivered", source="http", tracking_number="AB123456"))
    assert f"{TESTS}/fixtures/acme/delivered.json" in saved
    stored = json.loads((tmp_path / TESTS / "fixtures" / "acme" / "delivered.json").read_text())
    assert stored == {"awb": "TRACKING0001", "status": "Delivered", "phone": "9000000000"}
    assert tools.fixtures == {"delivered.json": f"{TESTS}/fixtures/acme/delivered.json"}
    with pytest.raises(BadAction):
        run(tools, act("save_fixture", name="../../x", source="http"))
    assert run(tools, act("open_page", url="https://api.acme.example/")).startswith("Refused"), (
        "no browser here"
    )


def test_finish_is_accepted_only_when_the_checks_pass(tmp_path: Path) -> None:
    failing = toolbox(tmp_path, problem="pytest failed: 1 failed")
    assert failing.run(parse_action(act("finish", summary="done"))) == (
        "Not finished, the checks do not pass yet:\npytest failed: 1 failed",
        False,
    )
    passing = toolbox(tmp_path)
    assert passing.run(parse_action(act("finish", summary="reads the new page"))) == ("", True)
    assert passing.summary == "reads the new page"
    assert run(passing, act("run_checks")).startswith("All checks passed")


# ----------------------------------------------------------------------------- the loop


class Flaky(ScriptedModel):
    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        answer = super().complete(system, messages)
        if answer == "BUSY":
            raise ModelError("rate limit", retryable=True)
        return answer


def test_the_loop_survives_bad_replies_and_stops_at_the_limit(tmp_path: Path) -> None:
    model = Flaky(["I will look around.", "BUSY", act("list_files"), act("finish", summary="nothing to do")])
    agent = Agent(model, "system", max_steps=10)
    assert agent.run("the task", toolbox(tmp_path)) == "nothing to do"
    assert agent.steps == 4
    assert "not a usable action" in model.asked[2][-1]["content"]

    endless = ScriptedModel([act("list_files")] * 5)
    with pytest.raises(AgentGaveUp, match="not finished after 5 steps"):
        Agent(endless, "system", max_steps=5).run("the task", toolbox(tmp_path))
    assert any("2 steps are left" in message["content"] for message in endless.asked[-1])


def test_a_model_that_stays_silent_ends_the_task_instead_of_using_up_every_step(tmp_path: Path) -> None:
    # Two silent steps, an answer, two more: the count starts again after every answer.
    patient = Flaky(["BUSY", "BUSY", act("list_files"), "BUSY", "BUSY", act("finish", summary="done")])
    assert Agent(patient, "system", max_steps=40).run("the task", toolbox(tmp_path)) == "done"

    silent = Flaky([act("list_files"), "BUSY", "BUSY", "BUSY", act("finish", summary="never reached")])
    agent = Agent(silent, "system", max_steps=40)
    with pytest.raises(AgentGaveUp, match=r"no answer 3 times in a row.*rate limit"):
        agent.run("the task", toolbox(tmp_path))
    assert agent.steps == 4


def test_old_results_are_shortened_and_the_task_is_kept() -> None:
    messages = [{"role": "user", "content": "TASK " + "t" * 5000}]
    for number in range(10):
        messages += [
            {"role": "assistant", "content": f"action {number}"},
            {"role": "user", "content": f"result {number} " + "r" * 5000},
        ]
    short = _shortened(messages)
    assert len(short) == len(messages)
    assert short[0] == messages[0], "the task is never cut"
    assert short[-12:] == messages[-12:], "the last six exchanges stay whole"
    assert all(len(m["content"]) < 500 for m in short[1:-12])
    assert short[2]["content"].startswith("result 0")


# ----------------------------------------------------------------------------- whole tasks


def agent_settings(settings: Settings, **changes: object) -> Settings:
    return settings.model_copy(update={"ai_repair_agent_browser": False, **changes})


REPAIRS_HEADER = '''\
"""Demo-site adapter: tests added by repairs."""

from __future__ import annotations
'''

#: What the scripted agent adds to the repairs test file. Its own imports go to the top of
#: the file; the file's earlier tests (an earlier repair's) are kept, as the rules require.
REPAIRS_IMPORTS = """\
from pathlib import Path

import pytest

from adapters.demosite.parser import parse_tracking_page
from courier_adapter_sdk import SchemaDrift
"""
REPAIRS_TESTS = """

AGENT_FIXTURES = Path(__file__).parent / "fixtures" / "demosite"


def test_the_recorded_answer_is_no_tracking_page() -> None:
    with pytest.raises(SchemaDrift):
        parse_tracking_page((AGENT_FIXTURES / "RECORDED").read_text())


def test_the_page_the_agent_saved_reads_as_a_delivered_shipment() -> None:
    events = parse_tracking_page((AGENT_FIXTURES / "delivered_page.html").read_text())
    assert len(events) == 5
    assert events[-1].raw_status == "Delivered"
"""


def repairs_file(existing: str | None) -> str:
    """The repairs test file with the scripted agent's tests added to what it already holds."""
    text = existing or REPAIRS_HEADER
    anchor = "from __future__ import annotations\n"
    assert anchor in text
    head, rest = text.split(anchor, 1)
    return (
        head
        + anchor
        + "\n"
        + REPAIRS_IMPORTS
        + rest.rstrip("\n")
        + "\n"
        + REPAIRS_TESTS.replace("RECORDED", FIXTURE)
    )


def test_an_agent_looks_at_the_site_with_the_browser_and_tests_against_what_it_saw(
    source: Path, remote: Path, settings: Settings, sandbox: Bubblewrap, site: str
) -> None:
    try:
        probe = Browser(local_net(site))
        probe.open(f"{site}/track?number=DEMO-OK-1")
        probe.close()
    except BrowserUnavailable as error:
        pytest.skip(str(error))
    # The adapter in the repository points at this test's copy of the demo site.
    adapter = f"{ADAPTERS}/demosite/adapter.py"
    code = (source / adapter).read_text().replace("http://127.0.0.1:8090", site)
    (source / adapter).write_text(code)
    git(source, "commit", "--quiet", "-am", "demo site address of this test")
    git(source, "push", "--quiet", str(remote), "main")
    # Whatever version the adapter has now (the AI may have raised it), the agent raises it by one.
    current = guard.adapter_version(code) or ""
    major, minor, patch = (int(part) for part in current.split("."))
    bumped = f"{major}.{minor}.{patch + 1}"
    newer = code.replace(f'version: ClassVar[str] = "{current}"', f'version: ClassVar[str] = "{bumped}"')
    assert newer != code
    repairs = f"{TESTS}/test_demosite_repairs.py"
    existing = source / repairs
    repairs_test = repairs_file(existing.read_text() if existing.exists() else None)

    model = ScriptedModel(
        [
            act("open_page", url="http://127.0.0.1:8081/internal/v1/repairs/claim"),  # the Engine: refused
            act("open_page", url=f"{site}/track?number=DEMO-OK-4711"),
            act("save_fixture", name="delivered_page", source="page", tracking_number="DEMO-OK-4711"),
            act("write_file", path=adapter, content=newer),
            f'{{"action": "write_file", "path": "{repairs}"}}\n```python\n{repairs_test}```',
            act("finish", summary="adds a test against a page read from the site"),
        ]
    )
    task = Task("task-2", "repair", "demosite", current, 1, {}, repair_task().samples)
    used = agent_settings(settings, ai_repair_agent_browser=True)
    done = Repairer(used, model, sandbox).process(task)

    assert done.pushed
    results = [exchange[-1]["content"] for exchange in model.asked[1:]]
    assert results[0].startswith("Refused: not an approved address")
    assert results[1].startswith("<<<UNTRUSTED DATA: the page>>>")
    assert "Delivered" in results[1]
    assert "TRACKING0001" in results[2]
    fixture = f"{TESTS}/fixtures/demosite/delivered_page.html"
    assert fixture in done.paths
    assert repairs in done.paths
    saved = git(remote, "show", f"main:{fixture}")
    assert "TRACKING0001" in saved
    assert "DEMO-OK-4711" not in saved, "the consignment number never reaches the repository"
    assert f'"{bumped}"' in git(remote, "show", f"main:{adapter}")


# ----------------------------------------------------------------------------- the courier's login

EMAIL, PASSWORD = "ops@acme.example.com", "p@ss word&<Secret>/42"


def login_toolbox(tmp_path: Path, handler: object, browser: object = None) -> Toolbox:
    net = Net({"api.acme.example"}, pause_s=0)
    return Toolbox(
        Folder(tmp_path), "acme", net, lambda saved: None,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        browser=browser,  # type: ignore[arg-type]
        login={"email": EMAIL, "password": PASSWORD},
    )  # fmt: skip


def test_the_login_is_filled_into_requests_and_never_shown_back(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # A careless site echoes everything it got, in every spelling.
        echoed = request.content.decode() + " " + request.url.query.decode()
        return httpx.Response(
            200,
            json={"you_sent": echoed, "user": EMAIL, "password": PASSWORD,
                  "auth": request.headers.get("authorization", "")},
        )  # fmt: skip

    tools = login_toolbox(tmp_path, server)
    assert tools.login_fields == ["email", "password"]
    answer = run(
        tools,
        act("http", method="post", url="https://api.acme.example/login?u={{login.email}}",
            form={"email": "{{login.email}}", "password": "{{login.password}}"},
            headers={"Authorization": "Basic {{login.password}}"}),
    )  # fmt: skip
    # Sent with the real values ...
    assert seen[0].url.params["u"] == EMAIL
    assert dict(httpx.QueryParams(seen[0].content.decode())) == {"email": EMAIL, "password": PASSWORD}
    assert seen[0].headers["authorization"] == f"Basic {PASSWORD}"
    # ... and read back only as placeholders.
    for value in (EMAIL, PASSWORD, httpx.QueryParams({"p": PASSWORD}).get("p")):
        assert value not in answer
    assert "urlencoded" not in answer
    assert "%40" not in answer
    assert "p%40ss" not in answer
    assert "{{login.password}}" in answer
    assert "{{login.email}}" in answer

    # A JSON body too.
    run(
        tools,
        act("http", method="post", url="https://api.acme.example/api", json={"pw": "{{login.password}}"}),
    )
    assert json.loads(seen[1].content) == {"pw": PASSWORD}

    # A fixture goes into Git: the login is not in it.
    run(tools, act("save_fixture", name="logged_in", source="http"))
    stored = (tmp_path / TESTS / "fixtures" / "acme" / "logged_in.json").read_text()
    assert PASSWORD not in stored
    assert EMAIL not in stored
    assert "{{login.password}}" in stored

    # An unknown field is a mistake the model is told about, without values.
    with pytest.raises(BadAction, match="no login field 'pin'") as refused:
        run(tools, act("http", url="https://api.acme.example/?pin={{login.pin}}"))
    assert PASSWORD not in str(refused.value)

    # A key the agent found somewhere else is still not sent.
    run(
        tools,
        act("http", url="https://api.acme.example/x", headers={"Authorization": "Bearer found-in-a-script"}),
    )
    assert "authorization" not in seen[-1].headers

    # Refusals name the address with placeholders only.
    refused_answer = run(tools, act("http", url="https://elsewhere.example/?p={{login.password}}"))
    assert refused_answer.startswith("Refused")
    assert PASSWORD not in refused_answer


def test_without_a_login_a_placeholder_is_refused(tmp_path: Path) -> None:
    tools = toolbox(tmp_path, lambda request: httpx.Response(200))
    assert tools.login_fields == []
    with pytest.raises(BadAction, match="no login is stored"):
        run(tools, act("http", url="https://api.acme.example/?p={{login.password}}"))


def test_the_browser_types_the_login_and_the_page_shows_placeholders(tmp_path: Path) -> None:
    class PageWithEcho:
        def __init__(self) -> None:
            self.typed: list[tuple[str, str]] = []

        def type(self, selector: str, text: str) -> str:
            self.typed.append((selector, text))
            return f"typed into {selector}"

        def describe(self) -> str:
            return f'<input name="email" value="{EMAIL}"> Welcome {EMAIL}! ({PASSWORD})'

        def close(self) -> None:
            pass

    page = PageWithEcho()
    tools = login_toolbox(tmp_path, lambda request: httpx.Response(200), browser=page)
    assert run(tools, act("type", selector="#password", text="{{login.password}}")) == "typed into #password"
    assert page.typed == [("#password", PASSWORD)]
    looked = run(tools, act("look"))
    assert EMAIL not in looked
    assert PASSWORD not in looked
    assert "Welcome {{login.email}}!" in looked


def test_a_whole_agent_run_with_a_login_never_shows_it_to_the_model(tmp_path: Path) -> None:
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<p>Signed in as {EMAIL}</p><p>Status: Delivered</p>")

    tools = login_toolbox(tmp_path, server)
    model = ScriptedModel([
        act("http", method="post", url="https://api.acme.example/login",
            form={"email": "{{login.email}}", "password": "{{login.password}}"}),
        act("finish", summary="logged in"),
    ])  # fmt: skip
    Agent(model, "system", max_steps=5).run(f"task; a login exists, fields {tools.login_fields}", tools)
    everything = model.system + json.dumps(model.asked)
    assert EMAIL not in everything
    assert PASSWORD not in everything
    assert "Signed in as {{login.email}}" in everything
