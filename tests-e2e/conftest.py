from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from harness import Stack, User, wait_until


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    logs = Path(__file__).parent / "logs"
    logs.mkdir(exist_ok=True)
    for old in logs.glob("*.log"):
        old.unlink()
    instance = Stack(logs)
    instance.create_database()
    try:
        instance.start_engine()
        instance.start_worker()
        instance.start_site()
        instance.start_worker(adapter="demosite")
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture(scope="session")
def user(stack: Stack) -> User:
    return stack.new_user()


@pytest.fixture(autouse=True)
def _healthy_stack(stack: Stack) -> Iterator[None]:
    """Every test starts with the Engine and a 4-slot worker running, and leaves it so."""
    yield
    if not stack.engine.running:
        stack.start_engine()
    if not stack.worker.running or stack.worker.env["WORKER_CONCURRENCY"] != "4":
        stack.restart_worker(4)


@pytest.fixture
def quiet(stack: Stack) -> None:
    """For tests about timing: waits until no other test's job is still active."""
    active = "SELECT 1 FROM tracking_jobs WHERE status IN ('QUEUED','PROCESSING','RETRYING')"
    wait_until(lambda: not stack.query(active), 60, "earlier jobs to finish")
