"""Check, without any AI model, that this machine can do what a repair needs.

``python -m ai_repair.selfcheck REPO DEMO_URL``: REPO holds a copy of ``courier-adapters``
and ``courier-contracts``. Runs the demo-site adapter's checks in the sandbox exactly as
a repair does, confirms that the sandbox has no network, no secrets and no view of the
service, and opens a page in the agent's browser. Used by ``make check-ai`` inside the
container. Exit code 0: everything passed.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .agent.browser import Browser
from .agent.net import Net, NetRefused
from .config import Settings
from .llm import ModelError, OpenRouterModel
from .sandbox import Bubblewrap, python_roots
from .workspace import GitError, Workspace

_PROBE = """
import os, pathlib, socket
print("ENV", sorted(os.environ))
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3)
    print("NETWORK open")
except OSError:
    print("NETWORK closed")
print("SERVICE", pathlib.Path({service!r}).exists())
try:
    open("adapters/selfcheck_probe.py", "w").close()
    print("WRITE done")
except OSError:
    print("WRITE refused")
"""


def main() -> int:
    repo, demo_url = Path(sys.argv[1]).resolve(), sys.argv[2]
    venv = Path(Settings().ai_repair_adapters_venv or "")
    python, adapters = str(venv / "bin" / "python"), repo / "courier-adapters"
    sandbox = Bubblewrap(readonly=python_roots(venv), proc=False if "--no-proc" in sys.argv else None)
    print("The sandbox has " + ("its own /proc." if sandbox.proc else "no /proc (as inside a container)."))
    failed: list[str] = []

    def report(name: str, ok: bool, detail: str = "") -> None:
        print(("OK    " if ok else "FAILED") + " " + name)
        if not ok:
            failed.append(name)
            print("       " + detail.strip()[-1500:].replace("\n", "\n       "))

    for name, arguments in [
        ("sandbox: ruff", ["-m", "ruff", "check", "--no-cache", "adapters/demosite"]),
        ("sandbox: mypy", ["-m", "mypy", "--cache-dir", "/tmp/mypy", "adapters/demosite"]),
        (
            "sandbox: the adapter's tests",
            ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--no-header",
             "tests/test_demosite_adapter.py", "tests/test_evidence.py", "tests/test_contracts.py"],
        ),
    ]:  # fmt: skip
        done = sandbox.run([python, *arguments], repo=repo, cwd=adapters, timeout_s=300)
        report(name, done.ok, done.output)

    service = str(Path(__file__).resolve().parent)
    probe = sandbox.run([python, "-c", _PROBE.format(service=service)], repo=repo, cwd=adapters, timeout_s=60)
    names = [name for name in os.environ if any(word in name for word in ("TOKEN", "KEY", "SECRET"))]
    report("sandbox: no network", "NETWORK closed" in probe.output, probe.output)
    report(
        "sandbox: none of the service's secrets",
        "ENV [" in probe.output and not any(name in probe.output for name in names),
        probe.output,
    )
    report("sandbox: the service's own files are not visible", "SERVICE False" in probe.output, probe.output)
    report("sandbox: tests cannot change the files", "WRITE refused" in probe.output, probe.output)

    host = demo_url.split("//", 1)[1].split("/", 1)[0]
    name, port = host.rsplit(":", 1) if ":" in host else (host, "80")
    browser = Browser(Net({name}, {(name, int(port))}, pause_s=0))
    try:
        page = browser.open(demo_url)
        report("browser: opens a page of the demo site", "-> 200" in page, page[:600])
        try:
            browser.open("https://example.com/")
            report("browser: refuses an address that is not approved", False, "the page was opened")
        except NetRefused:
            report("browser: refuses an address that is not approved", True)
    except Exception as error:
        report("browser: starts", False, f"{type(error).__name__}: {error}")
    finally:
        browser.close()

    # The steps of a repair before and after the agent's work: the clone, the commit and,
    # only when asked for with --push, the push.
    settings = Settings()
    if not (settings.ai_repair_github_repo or settings.ai_repair_repo_url):
        print("SKIPPED github: AI_REPAIR_GITHUB_REPO is not set")
    else:
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
            report(f"github: the repository can be cloned (at {base[:12]})", True)
            path = "courier-adapters/adapters/demosite/push_check.txt"
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            ws.write(path, f"Written by the AI repair service's self-check on {stamp}. Safe to delete.\n")
            sha = ws.commit([path], "AI repair self-check: a test file (safe to delete)")
            report("git: a commit can be made in the clone", len(sha) == 40)
            if "--push" in sys.argv:
                # The same push a finished repair makes: to the branch, never forced.
                ws.push()
                report(f"github: commit {sha[:12]} was pushed to {settings.ai_repair_push_branch}", True)
            else:
                print("       (not pushed; make check-ai-push also pushes a test file)")
        except GitError as error:
            report("github: clone, commit and push", False, str(error))
        finally:
            ws.remove()

    # One short question. A refusal for the rate limit is the supplier's state, not a fault here.
    model = OpenRouterModel(
        settings.openrouter_api_key.get_secret_value(),
        settings.openrouter_model,
        settings.openrouter_base_url,
        60,
        2000,
        retries=0,
        retry_wait_s=0,
    )
    try:
        model.complete("Answer with the single word: ready", [{"role": "user", "content": "Are you there?"}])
        print(f"OK     model: answers ({getattr(model, 'last_model', model.name)})")
    except ModelError as error:
        print(
            f"NOTE   model: no answer now ({str(error)[:200]}). A repair would wait or fail for this reason."
        )

    print("\nEverything passed." if not failed else f"\n{len(failed)} check(s) failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
