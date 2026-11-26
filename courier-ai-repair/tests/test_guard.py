"""The rules proposed code must keep, each with code that tries to get around it."""

from __future__ import annotations

import pytest

from ai_repair.guard import ADAPTERS, TESTS, ProposedFile, Rejected, check_proposal, hosts_in, kind_of

KEY = "acme"
HOSTS = {"api.acme.example"}
ADAPTER = f"{ADAPTERS}/{KEY}/adapter.py"
TEST = f"{TESTS}/test_{KEY}_adapter.py"

GOOD_ADAPTER = """
from __future__ import annotations

import os
from typing import ClassVar

from courier_adapter_sdk import Adapter, JobContext, TrackingResult, TrackRequest
from courier_adapter_sdk.http import check_status

from ..india import status_code
from .parser import parse

URL = "https://api.acme.example/track"


class AcmeAdapter(Adapter):
    key: ClassVar[str] = "acme"
    version: ClassVar[str] = "0.1.1"

    async def track(self, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        base = os.environ.get("ACME_BASE_URL") or URL
        response = await ctx.http().get(base)
        check_status(response)
        ctx.keep_evidence(response)
        return parse(response.json(), status_code)
"""
GOOD_TEST = """
from pathlib import Path

import httpx
import pytest

from adapters.acme import AcmeAdapter

from .adapter_support import run_track

PAGE = (Path(__file__).parent / "fixtures" / "acme" / "unreadable_1.json").read_text()


async def test_one() -> None: ...
async def test_two() -> None: ...
def test_three() -> None:
    httpx.Response(200, text="http://test.example/x")
"""
OLD_ADAPTER = GOOD_ADAPTER.replace("0.1.1", "0.1.0")
BEFORE = {
    ADAPTER: OLD_ADAPTER,
    f"{ADAPTERS}/{KEY}/__init__.py": "",
    f"{ADAPTERS}/{KEY}/__main__.py": "",
    TEST: "def test_one(): ...\ndef test_two(): ...\ndef test_three(): ...\n",
}


def check(adapter: str = GOOD_ADAPTER, test: str = GOOD_TEST, **extra: str) -> None:
    files = [ProposedFile(ADAPTER, adapter), ProposedFile(TEST, test)]
    files += [ProposedFile(path, content) for path, content in extra.items()]
    check_proposal(files, KEY, HOSTS, before=BEFORE, fixtures=["unreadable_1.json"])


def test_a_proposal_that_keeps_the_rules_is_accepted() -> None:
    check()


@pytest.mark.parametrize(
    "path",
    [
        "courier-engine/src/main.rs",
        "courier-adapters/adapters/dhl/adapter.py",
        "courier-adapters/adapters/india.py",
        "courier-adapters/courier_adapter_sdk/worker.py",
        "courier-adapters/adapters/acme/../dhl/adapter.py",
        "courier-adapters/adapters/acme/sub/x.py",
        "courier-adapters/adapters/acme/run.sh",
        "courier-adapters/tests/conftest.py",
        "courier-adapters/tests/test_acme_adapter.py.bak",
        "courier-adapters/pyproject.toml",
        ".github/workflows/ci.yml",
        "/etc/passwd",
        ".env",
    ],
)
def test_only_the_adapters_own_files_may_be_written(path: str) -> None:
    assert kind_of(path, KEY) is None
    with pytest.raises(Rejected, match="only the adapter's own files"):
        check(**{path: "x = 1\n"})


@pytest.mark.parametrize(
    "line",
    [
        "import subprocess",
        "import socket",
        "import os.path",
        "from os import system",
        "import sys",
        "import importlib",
        "import pathlib",
        "import ctypes",
        "import pickle",
        "import adapters.dhl",
        "from adapters.indiax import y",
        "from ..dhl import DhlAdapter",
        "from ...courier_adapter_sdk import worker",
        "import courier_adapter_sdk.worker",
        "from courier_adapter_sdk.sessions import x",
        "x = eval('1')",
        "exec('x = 1')",
        "y = __import__('os')",
        "f = open('/etc/passwd')",
        "g = getattr(os, 'sys' + 'tem')",
        "os.system('id')",
        "os.remove('x')",
        "names = os.listdir('/')",
        "import os as o\no.execv('/bin/sh', [])",
        "o = os\no.execv('/bin/sh', [])",
        "run = [os][0].execv",
        "e = os.environ.copy()",
        "v = os.environ",
        "v = os.environ['WORKER_TOKEN']",
        "v = os.environ.get('WORKER_TOKEN')",
        "v = os.environ.get('WORKER_SESSION_KEY')",
        "name = 'ACME_X'; v = os.environ.get(name)",
        "v = os.getenv('ACME_X')",
        "c = ().__class__",
        "b = __builtins__",
        "U = 'https://evil.example/collect'",
        "U = 'https://api.acme.example.evil.example/x'",
        "U = 'https://api.acme.example@evil.example/x'",
        "U = b'http://169.254.169.254/latest/meta-data'",
        "U = 'ftp://files.other.example/x'",
        "U = f'https://evil.example/{1}'",
    ],
)
def test_adapter_code_that_reaches_beyond_its_job_is_rejected(line: str) -> None:
    with pytest.raises(Rejected):
        check(adapter=GOOD_ADAPTER + "\n" + line + "\n")


@pytest.mark.parametrize(
    "line",
    [
        "import subprocess",
        "import shutil",
        "from pathlib import Path\nPath('x').write_text('y')",
        "from pathlib import Path\nPath('x').unlink()",
        "from pathlib import Path\nPath.home()",
        "U = 'https://collector.evil.net/collect'",
        "from .conftest import x",
        "from tests.conftest import x",
        "import adapters.dhl",
    ],
)
def test_test_code_is_held_to_the_same_rules(line: str) -> None:
    with pytest.raises(Rejected):
        check(test=GOOD_TEST + "\n" + line + "\n")


def test_the_whole_proposal_must_hang_together() -> None:
    with pytest.raises(Rejected, match="version must be raised"):
        check(adapter=OLD_ADAPTER)
    with pytest.raises(Rejected, match="keep_evidence"):
        check(adapter=GOOD_ADAPTER.replace("ctx.keep_evidence(response)", "pass"))
    with pytest.raises(Rejected, match='key = "acme"'):
        check(adapter=GOOD_ADAPTER.replace('= "acme"', '= "dhl"'))
    with pytest.raises(Rejected, match=r"fewer tests than before|existing tests were removed"):
        check(test=GOOD_TEST.replace("async def test_two() -> None: ...", ""))
    with pytest.raises(Rejected, match=r"fixture file unreadable_1\.json"):
        check(test=GOOD_TEST.replace("unreadable_1.json", "other.json"))
    with pytest.raises(Rejected, match="not valid Python"):
        check(adapter="def broken(:\n")
    with pytest.raises(Rejected, match="larger than"):
        check(adapter=GOOD_ADAPTER + "# " + "x" * 60_000)
    with pytest.raises(Rejected, match="no files"):
        check_proposal([], KEY, HOSTS, before=BEFORE, fixtures=[])
    with pytest.raises(Rejected, match="twice"):
        check_proposal([ProposedFile(ADAPTER, GOOD_ADAPTER)] * 2, KEY, HOSTS, before=BEFORE, fixtures=[])
    with pytest.raises(Rejected, match="not valid"):
        check_proposal([ProposedFile(ADAPTER, GOOD_ADAPTER)], "../x", HOSTS, before=BEFORE, fixtures=[])


def test_a_new_adapter_must_bring_every_file_it_needs() -> None:
    files = [ProposedFile(ADAPTER, GOOD_ADAPTER), ProposedFile(TEST, GOOD_TEST)]
    with pytest.raises(Rejected, match=r"__init__\.py is missing"):
        check_proposal(files, KEY, HOSTS, before={}, fixtures=[])


def test_hosts_are_read_from_addresses() -> None:
    text = "see https://API.Acme.example:8443/a?b and http://user:pw@other.example/x, not example.org"
    assert hosts_in(text) == {"api.acme.example", "other.example"}


def test_a_file_keeps_what_its_current_version_already_imports_and_names() -> None:
    old_test = (
        "import threading\nfrom demo_site.pages import render\n"
        'SITE = "http://staging.acme.example"\n'
        "def test_one(): ...\ndef test_two(): ...\ndef test_three(): ...\n"
    )
    new_test = (
        GOOD_TEST
        + "\nimport threading\nfrom demo_site.pages import render\n"
        + 'S = "http://staging.acme.example/x"\n'
    )
    before = {**BEFORE, TEST: old_test}
    files = [ProposedFile(ADAPTER, GOOD_ADAPTER), ProposedFile(TEST, new_test)]
    check_proposal(files, KEY, HOSTS, before=before, fixtures=["unreadable_1.json"])
    # ... but nothing beyond that, and not in the other file.
    for extra in ("import shutil", 'S2 = "http://collector.evil.net/"'):
        with pytest.raises(Rejected):
            check_proposal(
                [files[0], ProposedFile(TEST, new_test + extra + "\n")],
                KEY,
                HOSTS,
                before=before,
                fixtures=[],
            )
    with pytest.raises(Rejected, match="threading"):
        check_proposal(
            [ProposedFile(ADAPTER, GOOD_ADAPTER + "\nimport threading\n"), files[1]],
            KEY, HOSTS, before=before, fixtures=[],
        )  # fmt: skip
    # Modules that start programs or open connections are never inherited.
    dangerous = {**before, TEST: old_test + "import subprocess\n"}
    with pytest.raises(Rejected, match="subprocess"):
        check_proposal(
            [files[0], ProposedFile(TEST, new_test + "import subprocess\n")],
            KEY,
            HOSTS,
            before=dangerous,
            fixtures=[],
        )


def _existing_adapters() -> list[str]:
    from .conftest import ROOT

    return sorted(
        p.parent.name
        for p in (ROOT / ADAPTERS).glob("*/adapter.py")
        if p.parent.name != "dummy" and (ROOT / TESTS / f"test_{p.parent.name}_adapter.py").exists()
    )


@pytest.mark.parametrize("key", _existing_adapters())
def test_every_existing_adapter_could_be_repaired_without_breaking_the_rules(key: str) -> None:
    """The current code of each adapter, resubmitted with a higher version, must be acceptable:
    otherwise a repair that keeps the existing code could never get through."""
    import re

    from ai_repair.guard import hosts_in as hosts

    from .conftest import ROOT

    before = {
        f"{ADAPTERS}/{key}/{p.name}": p.read_text() for p in sorted((ROOT / ADAPTERS / key).glob("*.py"))
    }
    test = f"{TESTS}/test_{key}_adapter.py"
    before[test] = (ROOT / test).read_text()
    adapter = f"{ADAPTERS}/{key}/adapter.py"
    bumped = re.sub(r'(version: ClassVar\[str\] = ")\d+\.\d+\.\d+', r"\g<1>9.9.9", before[adapter])
    files = [ProposedFile(path, bumped if path == adapter else text) for path, text in before.items()]
    allowed = set().union(*(hosts(text) for path, text in before.items() if path.startswith(ADAPTERS)))
    check_proposal(files, key, allowed, before=before, fixtures=[])


def test_a_repair_may_add_its_tests_in_a_separate_file_and_leave_the_big_one_alone() -> None:
    repairs = f"{TESTS}/test_{KEY}_repairs.py"
    assert kind_of(repairs, KEY) == "test"
    new_tests = GOOD_TEST.replace("test_one", "test_recorded_1").replace("test_two", "test_recorded_2")
    new_tests = new_tests.replace("test_three", "test_recorded_3")
    files = [ProposedFile(ADAPTER, GOOD_ADAPTER), ProposedFile(repairs, new_tests)]
    check_proposal(files, KEY, HOSTS, before=BEFORE, fixtures=["unreadable_1.json"])
    # Without a test that uses the recorded answer it is not a repair.
    with pytest.raises(Rejected, match=r"fixture file unreadable_1\.json"):
        check_proposal(files[:1], KEY, HOSTS, before=BEFORE, fixtures=["unreadable_1.json"])
    # Emptying the existing test file is still refused.
    gutted = [*files, ProposedFile(TEST, "x = 1\n")]
    with pytest.raises(Rejected, match="had 3 tests and would have 0: existing tests were removed"):
        check_proposal(gutted, KEY, HOSTS, before=BEFORE, fixtures=["unreadable_1.json"])
    with pytest.raises(Rejected, match="fewer tests than before"):
        check_proposal([files[0], ProposedFile(TEST, "x = 1\n")], KEY, HOSTS, before=BEFORE, fixtures=[])


def test_the_recorded_answer_may_be_named_without_its_ending_and_new_test_files_inherit_imports() -> None:
    old_test = "import threading\ndef test_one(): ...\ndef test_two(): ...\ndef test_three(): ...\n"
    before = {**BEFORE, TEST: old_test}
    repairs = ProposedFile(
        f"{TESTS}/test_{KEY}_repairs.py",
        'import threading\n\n\ndef test_new() -> None:\n    page("unreadable_1")\n',
    )
    check_proposal(
        [ProposedFile(ADAPTER, GOOD_ADAPTER), repairs],
        KEY,
        HOSTS,
        before=before,
        fixtures=["unreadable_1.json"],
    )
    with pytest.raises(Rejected, match="shutil"):
        check_proposal(
            [ProposedFile(ADAPTER, GOOD_ADAPTER), ProposedFile(repairs.path, "import shutil\n" + repairs.content)],  # noqa: E501
            KEY, HOSTS, before=before, fixtures=["unreadable_1.json"],
        )  # fmt: skip
