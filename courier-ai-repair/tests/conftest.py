"""A copy of the real repository as a local Git remote, and the real sandbox."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ai_repair.config import Settings
from ai_repair.sandbox import Bubblewrap, SandboxUnavailable, python_roots

ROOT = Path(__file__).resolve().parents[2]
VENV = ROOT / "courier-adapters" / ".venv"
_SKIP = shutil.ignore_patterns(".venv", "__pycache__", ".*_cache", "target")


def git(cwd: Path, *arguments: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *arguments],
        cwd=cwd, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return done.stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """The parts of the repository the repair service touches, as a fresh Git repository."""
    repo = tmp_path / "source"
    for part in ("courier-adapters", "courier-contracts"):
        shutil.copytree(ROOT / part, repo / part, ignore=_SKIP)
    shutil.copytree(ROOT / "courier-engine" / "migrations", repo / "courier-engine" / "migrations")
    (repo / "infrastructure").mkdir()
    shutil.copy(ROOT / "infrastructure" / "docker-compose.yml", repo / "infrastructure")
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "start")
    return repo


@pytest.fixture
def remote(tmp_path: Path, source: Path) -> Path:
    """Stands in for GitHub: a bare repository the service clones from and pushes to."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(source), str(bare))
    return bare


@pytest.fixture
def settings(tmp_path: Path, remote: Path) -> Settings:
    return Settings(
        _env_file=None,
        ai_repair_repo_url=f"file://{remote}",
        ai_repair_work_dir=tmp_path / "work",
        ai_repair_adapters_venv=str(VENV),
        openrouter_model="test/fake-model",
        # Said here, so the tests do not depend on what the default is.
        ai_repair_dry_run=False,
        # The browser has its own tests; whole tasks here are scripted and need none.
        ai_repair_agent_browser=False,
    )


@pytest.fixture(scope="session")
def sandbox() -> Bubblewrap:
    if not (VENV / "bin" / "python").exists():
        pytest.skip("courier-adapters/.venv is missing (run: cd courier-adapters && uv sync)")
    try:
        return Bubblewrap(readonly=python_roots(VENV))
    except SandboxUnavailable as error:
        pytest.skip(f"no sandbox on this machine: {error}")
