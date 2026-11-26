"""Run the repair service: ``uv run python -m ai_repair`` (``--once`` for a single task)."""

from __future__ import annotations

import contextlib
import signal
import sys
import time
from pathlib import Path

import structlog

from .config import Settings
from .engine import EngineClient, EngineUnavailable
from .llm import OpenRouterModel
from .sandbox import Bubblewrap, SandboxUnavailable, python_roots
from .service import Repairer, TaskFailed

log = structlog.get_logger()


def _stop(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def run_once(engine: EngineClient, repairer: Repairer) -> bool:
    """Claims and does one task. Returns False when there was none."""
    task = engine.claim()
    if task is None:
        return False
    try:
        done = repairer.process(task)
    except KeyboardInterrupt:
        # Stopped by hand or by the container: hand the task back, so that the adapter's
        # lookups are not held back for a task nobody works on.
        log.warning("task_interrupted", task=task.id, adapter=task.adapter_key)
        with contextlib.suppress(EngineUnavailable):
            engine.failed(task.id, "the repair service was stopped while working on this task")
        raise
    except TaskFailed as error:
        log.warning("task_failed", task=task.id, adapter=task.adapter_key, reason=str(error)[:500])
        engine.failed(task.id, str(error))
    except Exception as error:
        # Reported by kind only: the text of an unexpected error is not known to be harmless.
        log.error("task_crashed", task=task.id, error=type(error).__name__)
        engine.failed(task.id, f"unexpected error in the repair service ({type(error).__name__})")
    else:
        if done.pushed:
            engine.published(task.id, done.commit_sha, done.summary)
        else:
            engine.failed(task.id, "dry run: checked and committed locally, not pushed")
    return True


def main() -> int:
    settings = Settings()
    missing = settings.missing()
    if missing:
        print("not configured: set " + ", ".join(missing) + " (see docs/phase-9.md)", file=sys.stderr)
        return 2
    venv = Path(settings.ai_repair_adapters_venv) if settings.ai_repair_adapters_venv else None
    if venv is None or not (venv / "bin" / "python").exists():
        print("not configured: AI_REPAIR_ADAPTERS_VENV must point to courier-adapters/.venv", file=sys.stderr)
        return 2
    try:
        # The environment and the Python installation it links to are readable in the sandbox.
        sandbox = Bubblewrap(readonly=python_roots(venv))
    except SandboxUnavailable as error:
        print(f"refusing to start: {error}. Generated code is never run without a sandbox.", file=sys.stderr)
        return 3
    model = OpenRouterModel(
        settings.openrouter_api_key.get_secret_value(),
        settings.openrouter_model,
        settings.openrouter_base_url,
        settings.ai_repair_llm_timeout_s,
        settings.ai_repair_max_output_tokens,
        retries=settings.ai_repair_llm_retries,
        retry_wait_s=settings.ai_repair_llm_retry_wait_s,
    )
    if settings.ai_repair_agent_browser:
        try:
            from playwright.sync_api import sync_playwright

            # Started for real: the path Playwright names is the full browser's, which is not
            # what is installed (the headless shell is).
            with sync_playwright() as started:
                started.chromium.launch(headless=True, env={}).close()
            browser_ready = True
        except Exception:
            browser_ready = False
        if not browser_ready:
            print(
                "The agent's browser is not installed; it will work with plain HTTP only. To install it: "
                "cd courier-ai-repair && uv run playwright install chromium-headless-shell",
                file=sys.stderr,
            )
            settings = settings.model_copy(update={"ai_repair_agent_browser": False})
    engine = EngineClient(settings.ai_repair_engine_url, settings.engine_repair_token.get_secret_value())
    repairer = Repairer(settings, model, sandbox)
    once = "--once" in sys.argv[1:]
    log.info(
        "repair_service_started",
        model=settings.openrouter_model,
        dry_run=settings.ai_repair_dry_run,
        branch=settings.ai_repair_push_branch,
    )
    print(
        "The repair service keeps running and waits for tasks; it is silent while there are none. "
        "Stop it with Ctrl+C.",
        file=sys.stderr,
    )
    idle_reported = False
    # "docker stop" sends SIGTERM: treated like Ctrl+C, so a task in work is handed back.
    signal.signal(signal.SIGTERM, _stop)
    try:
        while True:
            try:
                worked = run_once(engine, repairer)
            except EngineUnavailable as error:
                log.warning("engine_unavailable", reason=str(error))
                worked, idle_reported = False, False
            else:
                if worked:
                    idle_reported = False
                elif not idle_reported:
                    # Said once, not every minute: the Engine answers, there is just no work.
                    log.info("no_task_waiting", engine="reachable", next_check_s=settings.ai_repair_poll_s)
                    idle_reported = True
            if once:
                return 0
            if not worked:
                time.sleep(settings.ai_repair_poll_s)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
