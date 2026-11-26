"""Whole tasks, end to end: a copy of the real repository as the Git remote, the real sandbox
running the real adapter tests, and a stand-in for the model that drives the agent from a script."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_repair.config import Settings
from ai_repair.engine import Sample, Task
from ai_repair.guard import ADAPTERS, TESTS
from ai_repair.llm import ModelError
from ai_repair.sandbox import Bubblewrap, Run
from ai_repair.service import Repairer, TaskFailed

from .conftest import ROOT, git

LR = "LR99887"
PAGE = (
    "<html><body><div class='msg'>Sorry, the consignment LR99887 is not in our records. "
    "Call 9443012345 or mail help@sbtl.co.in</div></body></html>"
)
SAMPLE_ID = "0193a000-0000-7000-8000-00aabbccddee"
FIXTURE = "unreadable_00aabbccddee.html"
ADAPTER = f"{ADAPTERS}/balaji/adapter.py"
TEST = f"{TESTS}/test_balaji_adapter.py"

TEST_FILE = """\
\"\"\"Balaji adapter: the page that says the consignment is unknown.\"\"\"

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from adapters.balaji import BalajiAdapter
from courier_adapter_sdk import SchemaDrift, ShipmentNotFound

from .adapter_support import run_track

PAGE = (Path(__file__).parent / "fixtures" / "balaji" / "FIXTURE").read_text()


def site(body: str) -> BalajiAdapter:
    return BalajiAdapter(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))


async def test_the_recorded_page_means_the_consignment_is_unknown() -> None:
    with pytest.raises(EXPECTED):
        await run_track(site(PAGE), "LR12345")


async def test_a_page_without_history_is_still_schema_drift() -> None:
    with pytest.raises(SchemaDrift):
        await run_track(site("<html><body><p>Welcome</p></body></html>"), "LR12345")


async def test_a_number_that_is_no_lr_number_is_not_asked() -> None:
    with pytest.raises(ShipmentNotFound):
        await run_track(site("<html></html>"), "!!")
""".replace("FIXTURE", FIXTURE)


class ScriptedModel:
    """Answers with the next prepared text and remembers what it was asked."""

    name = "test/fake-model"

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.asked: list[list[dict[str, str]]] = []
        self.system = ""

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        self.system = system
        self.asked.append([dict(m) for m in messages])
        return self.answers[len(self.asked) - 1]


def act(name: str, /, **arguments: object) -> str:
    """One step of the agent, as the model writes it."""
    return json.dumps({"action": name, **arguments})


def write(files: dict[str, str]) -> list[str]:
    return [act("write_file", path=path, content=content) for path, content in files.items()]


def finish(summary: str = "reads the new page") -> str:
    return act("finish", summary=summary)


def repaired_adapter(source: Path) -> str:
    code = (source / ADAPTER).read_text()
    fixed = code.replace('version: ClassVar[str] = "0.1.0"', 'version: ClassVar[str] = "0.1.1"').replace(
        "        events = history_events(html)\n",
        '        if "is not in our records" in html:\n            raise ShipmentNotFound\n'
        "        events = history_events(html)\n",
    )
    assert fixed.count("0.1.1") == 1
    assert "not in our records" in fixed
    return fixed


def repair_task() -> Task:
    sample = Sample(SAMPLE_ID, "0.1.0", "SCHEMA_DRIFT", f"no history table for {LR}", LR, "text/html", PAGE)
    return Task("task-1", "repair", "balaji", "0.1.0", 1, {}, [sample])


def head(remote: Path) -> str:
    return git(remote, "rev-parse", "main")


def test_a_repair_is_rejected_tested_corrected_and_then_published(
    source: Path, remote: Path, settings: Settings, sandbox: Bubblewrap
) -> None:
    good = repaired_adapter(source)
    right, wrong = (
        TEST_FILE.replace("EXPECTED", "ShipmentNotFound"),
        TEST_FILE.replace("EXPECTED", "SchemaDrift"),
    )
    fixture = f"{TESTS}/fixtures/balaji/{FIXTURE}"
    model = ScriptedModel(
        [
            # breaks a rule: refused before anything is run
            *write({ADAPTER: good + "\nimport subprocess\n", TEST: right}),
            finish(),
            # keeps the rules, but its own test fails
            *write({ADAPTER: good, TEST: wrong}),
            finish(),
            # right. It also tries to change the fixture; that is not written.
            *write({TEST: right, fixture: "<html>changed by the model</html>"}),
            finish(),
        ]
    )
    before = head(remote)
    done = Repairer(settings, model, sandbox).process(repair_task())

    assert done.pushed
    assert head(remote) == done.commit_sha != before
    assert git(remote, "rev-parse", "main~1") == before, "one commit on top, nothing rewritten"
    assert git(remote, "show", "--name-only", "--format=", "main").splitlines() == [ADAPTER, fixture, TEST]
    assert 'version: ClassVar[str] = "0.1.1"' in git(remote, "show", f"main:{ADAPTER}")
    message = git(remote, "log", "-1", "--format=%an <%ae>%n%B", "main")
    assert message.startswith(
        "courier-ai-repair <courier-ai-repair@users.noreply.github.com>\nAI repair: balaji adapter 0.1.1"
    )
    assert "without human review" in message
    assert "Repair-Task: task-1" in message

    # What left the system: no tracking number, phone number or e-mail address.
    committed = git(remote, "show", f"main:{fixture}")
    sent = json.dumps(model.asked)
    for private in (LR, "9443012345", "help@sbtl.co.in"):
        assert private not in committed
        assert private not in sent
    assert "TRACKING0001 is not in our records" in committed
    assert "UNTRUSTED DATA" in model.asked[0][0]["content"]
    assert "never instructions" in model.system

    # The model was told what was wrong each time.
    assert len(model.asked) == 9
    assert "may not be imported" in model.asked[3][-1]["content"]
    assert "pytest failed" in model.asked[6][-1]["content"]
    assert model.asked[8][-1]["content"].startswith("Not written"), "the fixture is the service's"
    assert not any(settings.ai_repair_work_dir.iterdir()), "the clone is removed"


def test_a_model_that_never_gets_it_right_publishes_nothing(
    source: Path, remote: Path, settings: Settings, sandbox: Bubblewrap
) -> None:
    bad = write({ADAPTER: repaired_adapter(source), TEST: TEST_FILE.replace("EXPECTED", "SchemaDrift")})
    model = ScriptedModel([*bad, finish(), "not even JSON", finish()])
    before = head(remote)
    few = settings.model_copy(update={"ai_repair_agent_max_steps": 5})
    with pytest.raises(TaskFailed, match="not finished after 5 steps") as raised:
        Repairer(few, model, sandbox).process(repair_task())
    assert "pytest failed" in str(raised.value)
    assert head(remote) == before
    assert not any(settings.ai_repair_work_dir.iterdir())


class FlakyModel(ScriptedModel):
    """Like a free router: sometimes no usable answer, and another model each time."""

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        text = super().complete(system, messages)
        self.last_model = f"vendor/model-{len(self.asked)}:free"
        if text == "":
            raise ModelError("the model vendor/thinker answered with nothing", retryable=True)
        return text


def test_an_empty_answer_costs_a_step_and_the_commit_names_the_model_that_finished_the_work(
    source: Path, remote: Path, settings: Settings, sandbox: Bubblewrap
) -> None:
    good = write({ADAPTER: repaired_adapter(source), TEST: TEST_FILE.replace("EXPECTED", "ShipmentNotFound")})
    model = FlakyModel(["", *good, finish()])
    done = Repairer(settings, model, sandbox).process(repair_task())
    assert done.pushed
    assert len(model.asked[1]) == 1, "the empty answer is not sent back as if it were a step"
    assert "Written by the AI model vendor/model-4:free" in git(remote, "log", "-1", "--format=%B", "main")

    with pytest.raises(TaskFailed, match="no answer 3 times in a row"):
        Repairer(settings, FlakyModel([""] * 5), sandbox).process(repair_task())
    refused = FlakyModel([])
    refused.complete = lambda system, messages: (_ for _ in ()).throw(ModelError("refused the API key"))  # type: ignore[method-assign]
    with pytest.raises(TaskFailed, match="refused the API key"):
        Repairer(settings, refused, sandbox).process(repair_task())


def test_generated_tests_run_without_network_secrets_or_the_rest_of_the_disk(
    source: Path, remote: Path, settings: Settings, sandbox: Bubblewrap, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that passes only if the sandbox holds. (Its code keeps the guard's rules: the
    sandbox is what stands when the rules are not enough.)"""
    monkeypatch.setenv("AI_REPAIR_GITHUB_TOKEN", "ghp_secret_in_the_service_environment")
    home, root = Path.home(), ROOT
    probe = (
        TEST_FILE.replace("EXPECTED", "ShipmentNotFound")
        + f"""

def test_the_sandbox_holds() -> None:
    # Of the home directory only the Python environment's own folders exist; the rest is absent.
    assert not Path("{home}/.ssh").exists()
    assert not Path("{home}/.claude").exists()
    assert not Path("{root}/.env").exists()
    assert not Path("{root}/.git").exists()
    assert not Path("{root}/courier-engine").exists()
    assert not Path("/var").exists()
    assert not Path("/root").exists()
    assert "ghp_secret" not in Path("/proc/self/environ").read_text()
    assert os.environ.get("BALAJI_TOKEN") is None
    assert not Path("/sys/class/net").exists()
    with pytest.raises(OSError, match="Read-only file system"):
        Path(__file__).with_name("written_by_a_test.txt").write_text("x")
    with pytest.raises(httpx.ConnectError):
        httpx.get("http://127.0.0.1:5672/")
"""
    )
    probe = probe.replace("import httpx\n", "import os\n\nimport httpx\n", 1)
    # Reading a directory listing and writing a file are things the guard refuses; here the
    # point is the sandbox, so the guard is taken out of the way for this one proposal.
    monkeypatch.setattr("ai_repair.guard.check_proposal", lambda *a, **k: None)
    model = ScriptedModel([*write({ADAPTER: repaired_adapter(source), TEST: probe}), finish()])
    done = Repairer(settings.model_copy(update={"ai_repair_dry_run": True}), model, sandbox).process(
        repair_task()
    )
    assert not done.pushed
    assert len(model.asked) == 3, "the probe passed on the first run"
    assert head(remote) == git(source, "rev-parse", "HEAD"), "a dry run pushes nothing"


class NoSandbox:
    def run(
        self, command: list[str], *, repo: Path, cwd: Path, timeout_s: float, writable: bool = False
    ) -> Run:
        raise AssertionError("nothing may be run for this task")


def test_tasks_that_make_no_sense_fail_before_the_model_is_asked(remote: Path, settings: Settings) -> None:
    model = ScriptedModel([])
    repairer = Repairer(settings, model, NoSandbox())
    sample = repair_task().samples
    for task, reason in [
        (Task("t", "repair", "../etc", None, 1, {}, sample), "not valid"),
        (Task("t", "repair", "nosuch", None, 1, {}, sample), "no adapter"),
        (Task("t", "repair", "balaji", None, 1, {}, []), "without an answer"),
        (Task("t", "delete_everything", "balaji", None, 1, {}, sample), "unknown kind"),
        (Task("t", "new_provider", "dhl", None, 1, {"code": "dhl", "name": "x", "method": "api",
                                                     "hosts": ["a.example"]}, []), "already exists"),
        (Task("t", "new_provider", "fastship", None, 1, {"code": "other", "name": "x", "method": "api",
                                                          "hosts": ["a.example"]}, []), "does not match"),
    ]:  # fmt: skip
        with pytest.raises(TaskFailed, match=reason):
            repairer.process(task)
    assert model.asked == []


NEW_ADAPTER = """\
\"\"\"FastShip adapter. PROVISIONAL: written from one example answer.

Credential: a JSON object with the field ``api_key``.
\"\"\"

from __future__ import annotations

import os
from typing import Any, ClassVar

import httpx

from courier_adapter_sdk import (
    Adapter,
    JobContext,
    RawResponse,
    SchemaDrift,
    ShipmentNotFound,
    TrackingEvent,
    TrackingResult,
    TrackRequest,
)
from courier_adapter_sdk.http import check_status

from ..india import local_time, place, status_code

DEFAULT_BASE_URL = "https://api.fastship.example"


def parse_scans(data: Any) -> list[TrackingEvent]:
    scans = data.get("scans") if isinstance(data, dict) else None
    if not isinstance(scans, list):
        raise SchemaDrift("the answer has no scans list")
    if not scans:
        raise ShipmentNotFound
    events: list[TrackingEvent] = []
    for scan in scans:
        if not isinstance(scan, dict) or not isinstance(scan.get("status"), str):
            raise SchemaDrift("a scan has no status")
        events.append(
            TrackingEvent(
                raw_status=status_code(scan["status"]),
                raw_description=scan["status"],
                occurred_at=local_time(scan.get("time")),
                location=place(scan.get("city")),
            )
        )
    return sorted(events, key=lambda event: event.occurred_at)


class FastshipAdapter(Adapter):
    key: ClassVar[str] = "fastship"
    version: ClassVar[str] = "0.1.0"

    def __init__(
        self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = (base_url or os.environ.get("FASTSHIP_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        secret = await ctx.credentials()
        client = ctx.http(transport=self._transport)
        response = await client.get(
            f"{self.base_url}/track",
            params={"awb": request.tracking_number},
            headers={"X-Api-Key": secret["api_key"]},
        )
        check_status(response)
        ctx.keep_evidence(response)
        data = response.json()
        return TrackingResult(RawResponse(content_type="application/json", body=data), parse_scans(data))
"""
NEW_INIT = (
    '"""FastShip adapter."""\n\nfrom .adapter import FastshipAdapter\n\n__all__ = ["FastshipAdapter"]\n'
)
NEW_MAIN = (
    '"""Run the FastShip adapter worker."""\n\nfrom courier_adapter_sdk import run\n\n'
    'from . import FastshipAdapter\n\nif __name__ == "__main__":\n    run(FastshipAdapter())\n'
)
NEW_TEST = """\
\"\"\"FastShip adapter, against the example answer.\"\"\"

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from adapters.fastship import FastshipAdapter
from courier_adapter_sdk import SchemaDrift, ShipmentNotFound

from .adapter_support import run_track

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "fastship" / "sample.json").read_text())
SECRET = {"api_key": "test-key"}


def api(body: object) -> FastshipAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.fastship.example"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json=body)

    return FastshipAdapter(transport=httpx.MockTransport(handler))


async def test_the_example_answer_gives_its_events_oldest_first() -> None:
    result = await run_track(api(SAMPLE), "AWB1", SECRET)
    assert [event.raw_status for event in result.events] == ["booked", "delivered"]


async def test_no_scans_means_the_number_is_unknown() -> None:
    with pytest.raises(ShipmentNotFound):
        await run_track(api({"scans": []}), "AWB1", SECRET)


async def test_another_structure_is_schema_drift() -> None:
    with pytest.raises(SchemaDrift):
        await run_track(api({"history": []}), "AWB1", SECRET)
"""


def test_a_new_provider_gets_its_adapter_and_everything_around_it_in_one_commit(
    remote: Path, settings: Settings, sandbox: Bubblewrap
) -> None:
    sample = {"scans": [{"status": "Delivered", "time": "20-11-2026 10:30", "city": "Chennai"},
                        {"status": "Booked", "time": "18-11-2026 09:00", "city": "Tirupur"}]}  # fmt: skip
    request = {
        "code": "fastship", "name": "FastShip Express", "method": "api", "hosts": ["api.fastship.example"],
        "needs_credential": True, "notes": "GET /track?awb=<number>, header X-Api-Key",
        "sample": json.dumps(sample),
    }  # fmt: skip
    base = f"{ADAPTERS}/fastship"
    model = ScriptedModel(
        [*write({f"{base}/__init__.py": NEW_INIT, f"{base}/__main__.py": NEW_MAIN,
                 f"{base}/adapter.py": NEW_ADAPTER, f"{TESTS}/test_fastship_adapter.py": NEW_TEST}),
         finish("a new adapter from the example answer")]
    )  # fmt: skip
    done = Repairer(settings, model, sandbox).process(
        Task("task-2", "new_provider", "fastship", None, 1, request)
    )

    assert done.pushed
    assert len(model.asked) == 5
    changed = git(remote, "show", "--name-only", "--format=", "main").splitlines()
    migration = next(path for path in changed if path.startswith("courier-engine/migrations/"))
    assert migration.endswith("_provider_fastship.sql")
    assert sorted(changed) == sorted(
        [f"{base}/__init__.py", f"{base}/__main__.py", f"{base}/adapter.py",
         f"{TESTS}/test_fastship_adapter.py", f"{TESTS}/fixtures/fastship/sample.json",
         "courier-contracts/topology.yaml", "infrastructure/docker-compose.yml", migration]
    )  # fmt: skip
    assert (
        git(remote, "log", "-1", "--format=%s", "main") == "AI: new adapter for FastShip Express (fastship)"
    )
    asked = model.asked[0][0]["content"]
    assert "Approved hosts (the only ones the adapter may contact): api.fastship.example" in asked
    assert "adapters/skyking/adapter.py" in asked, "an existing adapter is shown as the pattern to follow"
