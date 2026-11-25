"""A fresh clone of the repository for one task: read, write, commit, push.

Git is run with a minimal environment and with hooks switched off, so nothing in the clone
can make Git execute something. The GitHub token is handed to Git through its environment
for the two commands that talk to GitHub; it is never part of an address, a file in the
clone or a command line, and it does not reach the sandbox.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path


class GitError(Exception):
    """A Git command failed. The text never contains the token."""


class Workspace:
    def __init__(
        self, url: str, branch: str, work_dir: Path, *, token: str = "", name: str = "", email: str = ""
    ) -> None:
        self._url, self._branch, self._token = url, branch, token
        self._name, self._email = name, email
        work_dir.mkdir(parents=True, exist_ok=True)
        self._root = Path(tempfile.mkdtemp(prefix="task-", dir=work_dir))
        self.repo = self._root / "repo"
        self._home = self._root / "home"
        self._home.mkdir()

    def _git(self, *arguments: str, remote: bool = False, cwd: Path | None = None) -> str:
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self._home),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LANG": "C.UTF-8",
        }
        if remote and self._token and self._url.startswith("https://"):
            basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            environment |= {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", *arguments]
        done = subprocess.run(
            command, cwd=cwd or self.repo, env=environment, capture_output=True, text=True,
            errors="replace", timeout=300, check=False, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()[-3:]
            text = " | ".join(detail)
            if self._token:
                text = text.replace(self._token, "[token]")
            raise GitError(f"git {arguments[0]} failed: {text[:400]}")
        return done.stdout if arguments[0] == "status" else done.stdout.strip()

    def clone(self) -> str:
        """Clones the branch and returns the commit it is at."""
        self._git(
            "clone", "--quiet", "--depth", "20", "--branch", self._branch, "--single-branch",
            self._url, str(self.repo), remote=True, cwd=self._root,
        )  # fmt: skip
        self._git("config", "user.name", self._name or "courier-ai-repair")
        self._git("config", "user.email", self._email or "courier-ai-repair@users.noreply.github.com")
        self._git("config", "commit.gpgsign", "false")
        return self._git("rev-parse", "HEAD")

    def read(self, path: str) -> str | None:
        file = self.repo / path
        return file.read_text(encoding="utf-8") if file.is_file() and not file.is_symlink() else None

    def write(self, path: str, content: str) -> None:
        file = (self.repo / path).resolve()
        if not file.is_relative_to(self.repo.resolve()) or ".git" in Path(path).parts:
            raise GitError("a path outside the repository was refused")
        file.parent.mkdir(parents=True, exist_ok=True)
        if file.is_symlink():
            raise GitError("a symbolic link was refused")
        file.write_text(content, encoding="utf-8")

    def discard(self, path: str, original: str | None) -> None:
        """Puts a file back to its original content, or removes it if it had none."""
        if original is None:
            (self.repo / path).unlink(missing_ok=True)
        else:
            self.write(path, original)

    def changed(self) -> list[str]:
        """Every path that differs from the last commit, including new files."""
        output = self._git("status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
        # Each entry is two status letters, a space and the path.
        return sorted(entry[3:] for entry in output.split("\0") if entry[3:])

    def commit(self, paths: list[str], message: str) -> str:
        """Commits exactly these paths. Anything else that changed is an error."""
        unexpected = sorted(set(self.changed()) - set(paths))
        if unexpected:
            raise GitError(f"files changed that are not part of the work: {', '.join(unexpected[:5])}")
        self._git("add", "--", *paths)
        self._git("commit", "--quiet", "--no-verify", "-m", message)
        return self._git("rev-parse", "HEAD")

    def push(self) -> None:
        """Pushes the commit. Never forced: if the branch moved meanwhile, the push fails."""
        self._git("push", "--quiet", "origin", f"HEAD:refs/heads/{self._branch}", remote=True)

    def remove(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)
