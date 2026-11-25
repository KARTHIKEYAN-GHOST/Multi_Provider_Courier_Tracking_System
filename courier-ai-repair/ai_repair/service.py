"""One task from start to finish: clone, ask the model, check, test in the sandbox, publish.

The order matters and is the safety design:

 1. a fresh clone of the repository, so nothing local can leak in or be damaged;
 2. the provider's answers are scrubbed before they go anywhere;
 3. the model proposes whole files;
 4. ``guard`` checks the proposal before a single file is written;
 5. the adapter's tests, the linter and the type checker run in the sandbox (no network,
    no secrets); a failure goes back to the model, a limited number of times;
 6. only what passed is committed, and only the files that belong to the task;
 7. the commit is pushed, and the Engine is told the outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from . import guard, prompts
from .config import Settings
from .engine import Task
from .guard import ADAPTERS, TESTS, ProposedFile, Rejected
from .llm import Model, ModelError, parse_proposal
from .sandbox import Sandbox
from .scaffold import ProviderSpec, ScaffoldError, scaffold
from .scrub import scrub
from .workspace import GitError, Workspace

log = structlog.get_logger()

#: Shown to the model when it writes a new adapter: one of each kind, plus the shared helpers.
_REFERENCE = {"api": "skyking", "scrape": "balaji"}
_SHARED = (f"{ADAPTERS}/india.py", f"{ADAPTERS}/tables.py", f"{TESTS}/adapter_support.py")


class TaskFailed(Exception):
    """The task cannot be completed. The text is reported to the Engine and holds no secret."""


@dataclass(frozen=True)
class Published:
    commit_sha: str
    summary: str
    paths: list[str]
    pushed: bool


def _adapter_files(ws: Workspace, key: str) -> dict[str, str]:
    """The adapter's current files, by path: what the model may replace."""
    found = {}
    directory = ws.repo / ADAPTERS / key
    for file in sorted(directory.glob("*.py")) if directory.is_dir() else []:
        content = ws.read(f"{ADAPTERS}/{key}/{file.name}")
        if content is not None:
            found[f"{ADAPTERS}/{key}/{file.name}"] = content
    test = ws.read(f"{TESTS}/test_{key}_adapter.py")
    if test is not None:
        found[f"{TESTS}/test_{key}_adapter.py"] = test
    return found


def _fixture(key: str, name: str, body: Any) -> tuple[str, str]:
    """Path and text of a fixture file for an answer."""
    if isinstance(body, str):
        suffix = "html" if "<" in body[:2000] else "txt"
        return f"{TESTS}/fixtures/{key}/{name}.{suffix}", body if body.endswith("\n") else body + "\n"
    return f"{TESTS}/fixtures/{key}/{name}.json", json.dumps(body, indent=2, ensure_ascii=False) + "\n"


class Repairer:
    def __init__(self, settings: Settings, model: Model, sandbox: Sandbox) -> None:
        self._settings, self._model, self._sandbox = settings, model, sandbox
        self._used = model.name

    # ------------------------------------------------------------------ the checks

    def _python(self, ws: Workspace) -> tuple[str, Path]:
        """The interpreter the checks run with, and the directory tree it lives in."""
        venv = Path(self._settings.ai_repair_adapters_venv or ws.repo / "courier-adapters" / ".venv")
        python = venv / "bin" / "python"
        if not python.exists():
            raise TaskFailed("the Python environment for the adapter checks is missing")
        return str(python), venv

    def _checks(self, ws: Workspace, key: str, python_files: list[str]) -> str | None:
        """Runs formatter, linter, type checker and tests. Returns what failed, or ``None``."""
        python, _ = self._python(ws)
        adapters = ws.repo / "courier-adapters"
        local = [path.removeprefix("courier-adapters/") for path in python_files]
        test = f"tests/test_{key}_adapter.py"
        timeout = self._settings.ai_repair_sandbox_timeout_s

        def run(arguments: list[str], *, writable: bool = False) -> tuple[bool, str]:
            done = self._sandbox.run(
                [python, *arguments], repo=ws.repo, cwd=adapters, timeout_s=timeout, writable=writable
            )
            return done.ok, done.output

        # Formatting is mechanical; the formatter does not run the code it formats.
        run(["-m", "ruff", "format", "--no-cache", *local], writable=True)
        run(["-m", "ruff", "check", "--no-cache", "--fix", "--select", "I,UP,F401", *local], writable=True)
        steps = [
            ("ruff check", ["-m", "ruff", "check", "--no-cache", "--output-format", "concise", *local]),
            ("mypy", ["-m", "mypy", "--cache-dir", "/tmp/mypy", "--no-error-summary",
                      f"adapters/{key}", test]),
            # The adapter's own tests, and the SDK tests every adapter must keep passing.
            ("pytest", ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--no-header", test,
                        "tests/test_evidence.py", "tests/test_contracts.py"]),
        ]  # fmt: skip
        for name, arguments in steps:
            ok, output = run(arguments)
            if not ok:
                return f"{name} failed:\n{output[-5000:]}"
        return None

    def _propose_until_accepted(
        self,
        ws: Workspace,
        key: str,
        first_message: str,
        hosts: set[str],
        before: dict[str, str],
        fixtures: list[str],
    ) -> tuple[str, list[str]]:
        """Asks the model, checks, tests; feeds problems back. Returns summary and written paths."""
        messages = [{"role": "user", "content": first_message}]
        problem = "the model was not asked"
        for round_number in range(1, self._settings.ai_repair_max_rounds + 1):
            try:
                answer = self._model.complete(prompts.SYSTEM, messages)
            except ModelError as error:
                if not error.retryable:
                    raise
                # No usable answer this time (a busy or weak model). Asking again costs a round.
                problem = f"the model gave no usable answer: {error}"
                log.info("proposal_not_accepted", adapter=key, round=round_number, problem=problem[:300])
                continue
            self._used = str(getattr(self._model, "last_model", self._model.name))
            log.info("model_answered", adapter=key, round=round_number, model=self._used)
            try:
                proposal = parse_proposal(answer)
                # Models like to send the fixture files back. They exist already, written by
                # this service from the real answers; a copy from the model is ignored.
                fixture_dir = f"{TESTS}/fixtures/{key}/"
                proposal.files[:] = [f for f in proposal.files if not f.path.startswith(fixture_dir)]
                guard.check_proposal(proposal.files, key, hosts, before=before, fixtures=fixtures)
            except (ModelError, Rejected) as error:
                problem = f"rejected before running: {error}"
            else:
                for path in [p for p in ws.changed() if guard.kind_of(p, key)]:
                    ws.discard(path, before.get(path))
                for file in proposal.files:
                    ws.write(file.path, file.content)
                paths = [file.path for file in proposal.files]
                failed = self._checks(ws, key, paths)
                if failed is None:
                    # The formatter rewrote the files: what is committed is checked once more.
                    final = [ProposedFile(path, ws.read(path) or "") for path in paths]
                    guard.check_proposal(final, key, hosts, before=before, fixtures=fixtures)
                    return proposal.summary.strip()[:300] or "no summary given", paths
                problem = failed
            log.info("proposal_not_accepted", adapter=key, round=round_number, problem=problem[:300])
            messages += [
                {"role": "assistant", "content": answer[:60_000]},
                {"role": "user", "content": prompts.retry(problem)},
            ]
        raise TaskFailed(
            f"no acceptable proposal after {self._settings.ai_repair_max_rounds} rounds; last problem: "
            f"{problem[:1200]}"
        )

    # ------------------------------------------------------------------ the two kinds of task

    def _repair(self, ws: Workspace, task: Task) -> tuple[str, list[str], str]:
        key = task.adapter_key
        before = _adapter_files(ws, key)
        if f"{ADAPTERS}/{key}/adapter.py" not in before:
            raise TaskFailed(f"the repository has no adapter '{key}'")
        if not task.samples:
            raise TaskFailed("the task came without an answer to repair from")
        hosts = set().union(
            *(guard.hosts_in(text) for path, text in before.items() if path.startswith(ADAPTERS))
        )
        failures, fixture_paths, fixture_names = [], [], []
        for sample in task.samples:
            scrubbed = scrub(sample.body, sample.tracking_number)
            path, text = _fixture(key, f"unreadable_{sample.id[-12:]}", scrubbed)
            ws.write(path, text)
            fixture_paths.append(path)
            fixture_names.append(Path(path).name)
            failures.append(
                (Path(path).name, sample.error_class, scrub(sample.error_message, sample.tracking_number),
                 prompts.as_text(text))
            )  # fmt: skip
        # Tests of this adapter kept in a shared file are shown, so they are not lost from view.
        context = {}
        for file in sorted((ws.repo / TESTS).glob("test_*.py")):
            content = ws.read(f"{TESTS}/{file.name}") or ""
            if f"adapters.{key}" in content and f"{TESTS}/{file.name}" not in before:
                context[f"{TESTS}/{file.name}"] = content
        message = prompts.repair(key, before, context, failures, hosts)
        summary, paths = self._propose_until_accepted(ws, key, message, hosts, before, fixture_names)
        version = guard.adapter_version(ws.read(f"{ADAPTERS}/{key}/adapter.py") or "") or "?"
        return summary, [*paths, *fixture_paths], f"AI repair: {key} adapter {version}"

    def _new_provider(self, ws: Workspace, task: Task) -> tuple[str, list[str], str]:
        try:
            spec = ProviderSpec.from_request(task.request)
            if spec.code != task.adapter_key:
                raise ScaffoldError("the request does not match the task")
            scaffolded = scaffold(ws.repo, spec)
        except ScaffoldError as error:
            raise TaskFailed(f"the provider could not be set up: {error}") from None
        key = spec.code
        sample, fixture_paths, fixture_names = None, [], []
        if spec.sample:
            try:
                body: Any = json.loads(spec.sample)
            except ValueError:
                body = spec.sample
            path, text = _fixture(key, "sample", scrub(body))
            ws.write(path, text)
            fixture_paths, fixture_names = [path], [Path(path).name]
            sample = (Path(path).name, prompts.as_text(text))
        reference_key = _REFERENCE[spec.method]
        reference = {**_adapter_files(ws, reference_key)}
        for path in _SHARED:
            reference[path] = ws.read(path) or ""
        message = prompts.new_provider(
            key, spec.name, spec.method, spec.hosts, spec.needs_credential, spec.notes, sample, reference
        )
        summary, paths = self._propose_until_accepted(ws, key, message, set(spec.hosts), {}, fixture_names)
        return summary, [*paths, *fixture_paths, *scaffolded], f"AI: new adapter for {spec.name} ({key})"

    # ------------------------------------------------------------------ entry

    def process(self, task: Task) -> Published:
        """Does one task. Raises ``TaskFailed`` (or a model / Git error) when it cannot be done."""
        if not guard.valid_key(task.adapter_key):
            raise TaskFailed("the task names an adapter key that is not valid")
        settings = self._settings
        ws = Workspace(
            settings.repo_url(),
            settings.ai_repair_push_branch,
            settings.ai_repair_work_dir,
            token=settings.ai_repair_github_token.get_secret_value(),
            name=settings.ai_repair_git_name,
            email=settings.ai_repair_git_email,
        )
        try:
            base = ws.clone()
            log.info("task_started", task=task.id, kind=task.kind, adapter=task.adapter_key, base=base[:12])
            if task.kind == "repair":
                summary, paths, title = self._repair(ws, task)
            elif task.kind == "new_provider":
                summary, paths, title = self._new_provider(ws, task)
            else:
                raise TaskFailed(f"unknown kind of task '{task.kind}'")
            message = (
                f"{title}\n\n{summary}\n\n"
                f"Written by the AI model {self._used} and published by the repair service "
                f"without human review, after its automatic checks passed "
                f"(code rules, ruff, mypy, the adapter's tests in a sandbox).\n\nRepair-Task: {task.id}"
            )
            sha = ws.commit(sorted(set(paths)), message)
            if settings.ai_repair_dry_run:
                log.info("dry_run_not_pushed", task=task.id, commit=sha[:12], paths=paths)
                return Published(sha, summary, sorted(set(paths)), pushed=False)
            ws.push()
            log.info("published", task=task.id, commit=sha[:12], branch=settings.ai_repair_push_branch)
            return Published(sha, summary, sorted(set(paths)), pushed=True)
        except GitError as error:
            raise TaskFailed(str(error)) from None
        except ModelError as error:
            raise TaskFailed(f"the AI model could not be used: {error}") from None
        except Rejected as error:
            raise TaskFailed(f"the result broke a rule after formatting: {error}") from None
        finally:
            ws.remove()
