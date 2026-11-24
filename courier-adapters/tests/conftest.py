"""Shared test helpers: fake broker pieces, message builders and the JSON Schema validators."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from courier_adapter_sdk.config import WorkerSettings
from courier_adapter_sdk.contracts import new_id
from courier_adapter_sdk.logging import configure_logging

CONTRACTS = Path(__file__).resolve().parents[2] / "courier-contracts"
FIXTURES = CONTRACTS / "fixtures" / "v1"

_SCHEMAS = {
    path.name.removesuffix(".schema.json"): json.loads(path.read_text(encoding="utf-8"))
    for path in sorted((CONTRACTS / "schemas" / "v1").glob("*.schema.json"))
}
_REGISTRY: Registry[Any] = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema)) for schema in _SCHEMAS.values()
)


def schema_errors(message: dict[str, Any]) -> list[str]:
    """Problems the shared JSON Schema finds in a message (empty when it is valid)."""
    validator = Draft202012Validator(_SCHEMAS[message["type"]], registry=_REGISTRY)
    return sorted(error.message for error in validator.iter_errors(message))


def fixture_files(kind: str, message_type: str) -> list[Path]:
    return sorted(p for p in (FIXTURES / kind).glob(f"{message_type}.*.json") if not p.name.startswith("_"))


@pytest.fixture(autouse=True)
def _logging() -> None:
    configure_logging("json")


def make_settings(**overrides: Any) -> WorkerSettings:
    values: dict[str, Any] = {
        "worker_amqp_url": "amqp://unused",
        "worker_concurrency": 4,
        "worker_cleanup_timeout_s": 1.0,
        "worker_id": "test-worker",
        "worker_topology_prefix": "",
    }
    values.update(overrides)
    return WorkerSettings(_env_file=None, **values)


def job_message(
    tracking_number: str = "DUMMY-OK-1",
    *,
    attempt: int = 1,
    timeout_ms: int = 10_000,
    expires_in_s: float = 900,
    adapter_key: str = "dummy",
    job_id: str | None = None,
) -> dict[str, Any]:
    """A ``job.track`` message as the Engine would send it."""
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in_s)
    return {
        "message_id": new_id(),
        "type": "job.track",
        "schema_version": "1.0",
        "occurred_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "correlation_id": new_id(),
        "causation_id": None,
        "producer": "engine@0.1.0",
        "payload": {
            "job_id": job_id or new_id(),
            "attempt": attempt,
            "adapter_key": adapter_key,
            "provider_code": "dummy",
            "tracking_number": tracking_number,
            "params": {},
            "timeout_ms": timeout_ms,
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        },
    }


@dataclass
class FakeDelivery:
    """A job message as the worker receives it, recording what the worker does with it."""

    message: dict[str, Any] | bytes
    delivery_count: int = 0
    acked: bool = False
    rejected: bool = False

    @property
    def body(self) -> bytes:
        return self.message if isinstance(self.message, bytes) else json.dumps(self.message).encode()

    async def ack(self) -> None:
        self.acked = True

    async def reject(self) -> None:
        self.rejected = True

    @property
    def settled(self) -> bool:
        return self.acked or self.rejected


@dataclass
class FakePublisher:
    """Records published worker events in order. Can be told to fail per message type."""

    fail: set[str] = field(default_factory=set)
    sent: list[dict[str, Any]] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def publish(self, message_type: str, body: bytes, message_id: str, correlation_id: str) -> None:
        if message_type in self.fail:
            raise ConnectionError("broker unreachable")
        message = json.loads(body)
        assert message["type"] == message_type
        assert message["message_id"] == message_id
        assert message["correlation_id"] == correlation_id
        self.sent.append(message)
        self.bodies.append(body)
        self.changed.set()

    def of(self, message_type: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["type"] == message_type]

    @property
    def results(self) -> list[dict[str, Any]]:
        return self.of("job.result")

    def result_for(self, tracking_job_id: str) -> dict[str, Any]:
        (found,) = [m for m in self.results if m["payload"]["job_id"] == tracking_job_id]
        return found

    async def wait_for_results(self, count: int, timeout_s: float = 5.0) -> None:
        async with asyncio.timeout(timeout_s):
            while len(self.results) < count:
                self.changed.clear()
                await self.changed.wait()


async def until(condition: Any, timeout_s: float = 5.0) -> None:
    """Waits for a condition without fixed sleeps."""
    async with asyncio.timeout(timeout_s):
        while not condition():  # noqa: ASYNC110 - polls arbitrary test state
            await asyncio.sleep(0.005)
