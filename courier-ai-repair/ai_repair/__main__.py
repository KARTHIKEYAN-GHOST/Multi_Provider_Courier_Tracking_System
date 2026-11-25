"""Run the repair service: ``uv run python -m ai_repair`` (``--once`` for a single task)."""

from __future__ import annotations

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


def run_once(engine: EngineClient, repairer: Repairer) -> bool:
    """Claims and does one task. Returns False when there was none."""
    task = engine.claim()
    if task is None:
        return False
    try:
        done = repairer.process(task)
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
    engine = EngineClient(settings.ai_repair_engine_url, settings.engine_repair_token.get_secret_value())
    repairer = Repairer(settings, model, sandbox)
    once = "--once" in sys.argv[1:]
    log.info("repair_service_started", model=settings.openrouter_model, dry_run=settings.ai_repair_dry_run,
             branch=settings.ai_repair_push_branch)  # fmt: skip
    try:
        while True:
            try:
                worked = run_once(engine, repairer)
            except EngineUnavailable as error:
                log.warning("engine_unavailable", reason=str(error))
                worked = False
            if once:
                return 0
            if not worked:
                time.sleep(settings.ai_repair_poll_s)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
