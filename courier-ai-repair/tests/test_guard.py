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
    TEST: "def test_a(): ...\ndef test_b(): ...\ndef test_c(): ...\n",
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
    with pytest.raises(Rejected, match="fewer tests"):
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
