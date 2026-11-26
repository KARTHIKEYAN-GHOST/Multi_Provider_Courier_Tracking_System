"""Runs the checks of proposed code where it can do no harm.

Proposed code is executed for the first time when its tests run. That happens inside a
bubblewrap sandbox:

  * no network at all (own network namespace): not the Engine, the database, RabbitMQ,
    Redis or the internet;
  * an empty environment: none of the service's tokens, no ``.env``;
  * the file system is not there, except system libraries, the Python installation and the
    clone, read-only while tests run; ``/tmp`` and ``/home`` are empty and private;
  * a time limit, and the whole process group is killed when it is over.

If bubblewrap is not available the service refuses to run anything. There is no unsafe mode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SandboxUnavailable(Exception):
    """No sandbox can be built on this machine, so nothing is executed."""


@dataclass(frozen=True)
class Run:
    ok: bool
    output: str


class Sandbox(Protocol):
    def run(
        self, command: list[str], *, repo: Path, cwd: Path, timeout_s: float, writable: bool = False
    ) -> Run: ...


def python_roots(venv: Path) -> list[Path]:
    """The environment and every Python installation directory its interpreter links through."""
    roots, hop = [venv], venv / "bin" / "python"
    for _ in range(10):
        if not hop.is_symlink():
            break
        hop = (hop.parent / os.readlink(hop)).absolute()
        if not hop.is_relative_to(venv):
            roots.append(Path(os.path.normpath(hop)).parents[1])
    return roots


class Bubblewrap:
    def __init__(self, readonly: list[Path] | None = None, *, proc: bool | None = None) -> None:
        program = shutil.which("bwrap")
        if program is None:
            raise SandboxUnavailable("bubblewrap (bwrap) is not installed")
        self._program = program
        #: Extra read-only directories: the Python installation the environment points to.
        self._readonly = list(readonly or [])
        base = [program, "--unshare-all", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64"]  # fmt: skip

        def works(*extra: str) -> bool:
            probe = subprocess.run(
                [*base, *extra, "/usr/bin/true"], capture_output=True, timeout=20, check=False
            )
            return probe.returncode == 0

        if not works():
            raise SandboxUnavailable("bubblewrap cannot create a sandbox on this machine")
        #: Inside a container the kernel refuses a new /proc (the container's own is partly
        #: covered). The sandbox then has none at all: the outer /proc is never shown to it.
        self.proc = works("--proc", "/proc") if proc is None else proc

    def _arguments(self, repo: Path, cwd: Path, writable: bool) -> list[str]:
        arguments = [
            self._program, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--ro-bind", "/usr", "/usr", *(("--proc", "/proc") if self.proc else ()), "--dev", "/dev",
            "--tmpfs", "/tmp", "--tmpfs", "/home", "--tmpfs", "/run",
        ]  # fmt: skip
        for name in ("bin", "sbin", "lib", "lib32", "lib64"):
            path = Path("/") / name
            if path.is_symlink():
                arguments += ["--symlink", os.readlink(path), str(path)]
            elif path.is_dir():
                arguments += ["--ro-bind", str(path), str(path)]
        for path in (Path("/etc/ld.so.cache"), Path("/etc/ld.so.conf"), Path("/etc/ld.so.conf.d")):
            if path.exists():
                arguments += ["--ro-bind", str(path), str(path)]
        for path in self._readonly:
            arguments += ["--ro-bind", str(path), str(path)]
        # Writable only for the formatter. Tests run with the clone read-only.
        arguments += ["--bind" if writable else "--ro-bind", str(repo.resolve()), str(repo.resolve())]
        arguments += [
            "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "PYTHONHASHSEED", "0",
            "--chdir", str(cwd.resolve()),
        ]  # fmt: skip
        return arguments

    def run(
        self, command: list[str], *, repo: Path, cwd: Path, timeout_s: float, writable: bool = False
    ) -> Run:
        try:
            done = subprocess.run(
                [*self._arguments(repo, cwd, writable), *command],
                capture_output=True, text=True, errors="replace", timeout=timeout_s, check=False,
                env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL, start_new_session=True,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return Run(False, f"the check did not finish within {int(timeout_s)} seconds")
        return Run(done.returncode == 0, (done.stdout + done.stderr).strip())
