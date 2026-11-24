"""The worker against a real RabbitMQ (needs ``make up-deps``).

Each test declares its own copy of the topology under a random prefix and deletes it
afterwards, so tests cannot disturb each other or the development queues. The prefixed
copies are created and used with the Engine account: the worker account is deliberately
unable to create anything and may only read queues with the real names. What the worker
account can and cannot do is checked separately at the end of this file.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aio_pika
import aiormq.exceptions
import pytest
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection

from adapters.dummy import DummyAdapter
from courier_adapter_sdk.topology import load_raw
from courier_adapter_sdk.worker import CRASH_EXIT_CODE, WorkerProcess

from .conftest import CONTRACTS, job_message, make_settings, schema_errors

REPO = CONTRACTS.parent
ADAPTERS = REPO / "courier-adapters"
TOPOLOGY = load_raw(CONTRACTS / "topology.yaml")


def env(name: str) -> str:
    """A setting from the environment or the repository's .env file."""
    if value := os.environ.get(name):
        return value
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    raise KeyError(name)


class Broker:
    """A private, prefixed copy of the topology plus helpers to feed and observe it."""

    def __init__(self, connection: AbstractRobustConnection, channel: AbstractChannel, prefix: str) -> None:
        self.connection = connection
        self.channel = channel
        self.prefix = prefix
        self.queues: dict[str, AbstractQueue] = {}

    async def declare(self) -> None:
        for exchange in TOPOLOGY["exchanges"]:
            await self.channel.declare_exchange(
                self.prefix + exchange["name"], exchange["type"], durable=True
            )
        for queue in TOPOLOGY["queues"]:
            arguments = dict(queue["arguments"])
            if "x-dead-letter-exchange" in arguments:
                arguments["x-dead-letter-exchange"] = self.prefix + arguments["x-dead-letter-exchange"]
            declared = await self.channel.declare_queue(
                self.prefix + queue["name"], durable=True, arguments=arguments
            )
            for binding in queue["bindings"]:
                await declared.bind(self.prefix + binding["exchange"], binding["routing_key"])
            self.queues[queue["name"]] = declared

    async def delete(self) -> None:
        for queue in self.queues.values():
            await queue.delete(if_unused=False, if_empty=False)
        for exchange in TOPOLOGY["exchanges"]:
            await self.channel.exchange_delete(self.prefix + exchange["name"])

    async def send_job(self, message: dict[str, Any] | bytes) -> None:
        exchange = await self.channel.get_exchange(self.prefix + "courier.jobs", ensure=False)
        body = message if isinstance(message, bytes) else json.dumps(message).encode()
        await exchange.publish(
            aio_pika.Message(body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            "job.track.v1.dummy",
            mandatory=True,
        )

    async def take(
        self, queue: str, count: int, timeout_s: float = 10.0
    ) -> list[aio_pika.abc.AbstractIncomingMessage]:
        """Removes and returns the next ``count`` messages of a queue."""
        taken: list[aio_pika.abc.AbstractIncomingMessage] = []
        async with asyncio.timeout(timeout_s):
            while len(taken) < count:
                message = await self.queues[queue].get(no_ack=True, fail=False)
                if message is None:
                    await asyncio.sleep(0.02)
                else:
                    taken.append(message)
        return taken

    async def events(self, count: int, timeout_s: float = 10.0) -> list[dict[str, Any]]:
        return [json.loads(m.body) for m in await self.take("engine.events", count, timeout_s)]

    async def peek(self, queue: str) -> aio_pika.abc.AbstractIncomingMessage | None:
        """Takes the next message of a queue if there is one. (The message count RabbitMQ
        returns for a quorum queue lags behind, so tests look instead of counting.)"""
        return await self.queues[queue].get(no_ack=True, fail=False)


@pytest.fixture
async def broker() -> AsyncIterator[Broker]:
    connection = await aio_pika.connect_robust(env("ENGINE_AMQP_URL"), timeout=10)
    channel = await connection.channel()
    instance = Broker(connection, channel, f"test-{secrets.token_hex(6)}.")
    await instance.declare()
    try:
        yield instance
    finally:
        await instance.delete()
        await connection.close()


def settings_for(broker: Broker, **overrides: Any) -> Any:
    return make_settings(
        worker_amqp_url=env("ENGINE_AMQP_URL"),
        worker_topology_prefix=broker.prefix,
        worker_id="integration-worker",
        **overrides,
    )


@pytest.fixture
async def worker(broker: Broker) -> AsyncIterator[WorkerProcess]:
    process = WorkerProcess(DummyAdapter(), settings_for(broker, worker_concurrency=4))
    running = asyncio.create_task(process.run())
    async with asyncio.timeout(10):
        await process.consuming.wait()
    try:
        yield process
    finally:
        process.stop()
        await asyncio.wait_for(running, 15)


def by_type(events: list[dict[str, Any]], message_type: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == message_type]


# ----------------------------------------------------------------------------- in-process


async def test_a_job_is_consumed_and_its_started_and_result_reach_the_engine_queue(
    broker: Broker, worker: WorkerProcess
) -> None:
    job = job_message("DUMMY-OK-1")
    await broker.send_job(job)
    messages = await broker.take("engine.events", 2)
    started, result = (json.loads(m.body) for m in messages)

    assert (started["type"], result["type"]) == ("job.started", "job.result")
    assert schema_errors(started) == []
    assert schema_errors(result) == []
    assert result["correlation_id"] == job["correlation_id"]
    assert result["payload"]["outcome"] == "SUCCESS"
    assert result["payload"]["delivery_count"] == 0
    # The AMQP properties repeat the envelope, and the message survives a broker restart.
    for raw, parsed in zip(messages, (started, result), strict=True):
        assert raw.message_id == parsed["message_id"]
        assert raw.correlation_id == parsed["correlation_id"]
        assert raw.type == parsed["type"]
        assert raw.content_type == "application/json"
        assert raw.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert [m.routing_key for m in messages] == ["job.started.v1", "job.result.v1"]

    await asyncio.sleep(0.2)
    assert await broker.peek("adapter.dummy.jobs") is None, "acknowledged after the result was confirmed"
    assert await broker.peek("adapter.dummy.jobs.dlq") is None


async def test_slow_jobs_do_not_hold_up_quick_ones(broker: Broker, worker: WorkerProcess) -> None:
    slow = [job_message(f"DUMMY-DELAY-1500-{n}") for n in range(2)]
    quick = [job_message(f"DUMMY-OK-{n}") for n in range(6)]
    for job in (*slow, *quick):
        await broker.send_job(job)

    events = await broker.events(2 + 2 * len(quick), timeout_s=1.2)  # well before a slow job ends
    done = {e["payload"]["job_id"] for e in by_type(events, "job.result")}
    assert done == {j["payload"]["job_id"] for j in quick}
    assert len(by_type(events, "job.started")) == 8, "the slow jobs are running meanwhile"

    late = await broker.events(2, timeout_s=5)
    assert {e["payload"]["job_id"] for e in late} == {j["payload"]["job_id"] for j in slow}


async def test_an_invalid_job_message_is_dead_lettered_and_later_jobs_still_run(
    broker: Broker, worker: WorkerProcess
) -> None:
    await broker.send_job(b"{not json")
    await broker.send_job(job_message("DUMMY-OK-1"))
    (dead,) = await broker.take("adapter.dummy.jobs.dlq", 1)
    assert dead.body == b"{not json"
    assert dead.headers["x-first-death-reason"] == "rejected"
    assert len(by_type(await broker.events(2), "job.result")) == 1


async def test_stopping_the_worker_finishes_the_running_job_first(broker: Broker) -> None:
    process = WorkerProcess(DummyAdapter(), settings_for(broker))
    running = asyncio.create_task(process.run())
    await asyncio.wait_for(process.consuming.wait(), 10)
    await broker.send_job(job_message("DUMMY-DELAY-800-1"))
    assert (await broker.events(1))[0]["type"] == "job.started"

    process.stop()
    await asyncio.sleep(0.3)  # the consumer is cancelled; the running job is still in flight
    assert not running.done()
    await broker.send_job(job_message("DUMMY-OK-AFTER-STOP"))
    await asyncio.wait_for(running, 10)

    (result,) = await broker.events(1, timeout_s=2)
    assert (result["type"], result["payload"]["outcome"]) == ("job.result", "SUCCESS")
    left = await broker.peek("adapter.dummy.jobs")
    assert left is not None, "the job sent after the stop was left for another worker"
    assert not left.redelivered, "and was never handed to the stopping worker"


async def test_a_result_that_cannot_be_published_ends_in_the_dead_letter_queue_not_in_a_loop(
    broker: Broker,
) -> None:
    """With nowhere to route results, every execution fails to report. The worker must drop its
    session each time so RabbitMQ counts the delivery; after the limit the job is dead-lettered
    and the Engine's dead-letter handler takes over."""
    await broker.queues["engine.events"].unbind(broker.prefix + "courier.events", "job.result.v1")
    process = WorkerProcess(DummyAdapter(), settings_for(broker))
    running = asyncio.create_task(process.run())
    try:
        job = job_message("DUMMY-OK-1")
        await broker.send_job(job)
        (dead,) = await broker.take("adapter.dummy.jobs.dlq", 1, timeout_s=40)
        assert json.loads(dead.body)["payload"]["job_id"] == job["payload"]["job_id"]
        assert dead.headers["x-first-death-reason"] == "delivery_limit"

        limit = TOPOLOGY["queues"][0]["arguments"]["x-delivery-limit"]
        started = await broker.events(limit + 1)
        assert [e["type"] for e in started] == ["job.started"] * (limit + 1)
        assert [e["payload"]["delivery_count"] for e in started] == list(range(limit + 1))
        assert len({e["payload"]["execution_id"] for e in started}) == limit + 1
        assert await broker.peek("adapter.dummy.jobs") is None
    finally:
        process.stop()
        await asyncio.wait_for(running, 15)


async def test_the_worker_waits_for_its_queue_and_reconnects(broker: Broker) -> None:
    """Started before the Engine has declared the topology: retries until the queue exists."""
    other_prefix = f"test-{secrets.token_hex(6)}."
    late = Broker(broker.connection, broker.channel, other_prefix)
    process = WorkerProcess(DummyAdapter(), settings_for(late))
    running = asyncio.create_task(process.run())
    try:
        await asyncio.sleep(0.7)
        assert not process.consuming.is_set()
        assert not running.done(), "a missing queue is not fatal"
        await late.declare()
        await asyncio.wait_for(process.consuming.wait(), 10)
        await late.send_job(job_message("DUMMY-OK-1"))
        assert len(await late.events(2)) == 2
    finally:
        process.stop()
        await asyncio.wait_for(running, 15)
        await late.delete()


# ----------------------------------------------------------------------------- real process


async def start_worker_process(broker: Broker, **extra: str) -> asyncio.subprocess.Process:
    """Runs ``python -m adapters.dummy`` exactly as a deployment would."""
    environment = {
        **os.environ,
        "WORKER_AMQP_URL": env("ENGINE_AMQP_URL"),
        "WORKER_TOPOLOGY_PREFIX": broker.prefix,
        "WORKER_ID": "process-worker",
        "LOG_FORMAT": "json",
        **extra,
    }
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "adapters.dummy", cwd=ADAPTERS, env=environment, stderr=asyncio.subprocess.PIPE
    )


async def finish(
    process: asyncio.subprocess.Process, timeout_s: float = 15
) -> tuple[int, list[dict[str, Any]]]:
    _, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    lines = [json.loads(line) for line in stderr.decode().splitlines() if line.startswith("{")]
    assert env("ENGINE_AMQP_URL").rsplit("@", 1)[0].rsplit(":", 1)[1] not in stderr.decode(), (
        "no password in logs"
    )
    assert process.returncode is not None
    return process.returncode, lines


async def test_sigterm_lets_the_in_flight_job_report_before_the_process_exits(broker: Broker) -> None:
    process = await start_worker_process(broker)
    try:
        job = job_message("DUMMY-DELAY-1000-1")
        await broker.send_job(job)
        assert (await broker.events(1, timeout_s=15))[0]["type"] == "job.started"
        process.send_signal(signal.SIGTERM)
        code, logs = await finish(process)
    finally:
        if process.returncode is None:
            process.kill()

    assert code == 0
    (result,) = await broker.events(1, timeout_s=2)
    assert result["payload"]["outcome"] == "SUCCESS"
    assert await broker.peek("adapter.dummy.jobs") is None
    # Structured JSON logs carrying the Engine's correlation ID.
    finished = [line for line in logs if line["message"] == "job_finished"]
    assert [line["correlation_id"] for line in finished] == [job["correlation_id"]]
    assert logs[-1]["message"] == "worker_stopped"


async def test_a_crashed_worker_leaves_the_job_for_the_restarted_one(broker: Broker) -> None:
    job = job_message("DUMMY-CRASHONCE-1")
    await broker.send_job(job)
    first = await start_worker_process(broker, WORKER_ALLOW_FAULT_INJECTION="true")
    code, _ = await finish(first)
    assert code == CRASH_EXIT_CODE
    (started,) = await broker.events(1)
    assert (started["type"], started["payload"]["delivery_count"]) == ("job.started", 0)

    second = await start_worker_process(broker, WORKER_ALLOW_FAULT_INJECTION="true")
    try:
        restarted, result = await broker.events(2, timeout_s=15)
        second.send_signal(signal.SIGTERM)
        assert (await finish(second))[0] == 0
    finally:
        if second.returncode is None:
            second.kill()
    assert (result["type"], result["payload"]["outcome"]) == ("job.result", "SUCCESS")
    assert result["payload"]["delivery_count"] == 1
    assert result["payload"]["attempt"] == 1, "same logical attempt"
    assert restarted["payload"]["execution_id"] != started["payload"]["execution_id"], (
        "new physical execution"
    )


# ----------------------------------------------------------------------------- worker account


async def test_the_worker_account_can_only_read_its_queue_and_publish_events() -> None:
    """Checked against the real topology, which the Engine declares when it starts."""
    engine = await aio_pika.connect(env("ENGINE_AMQP_URL"), timeout=10)
    try:
        channel = await engine.channel()
        try:
            await channel.declare_queue("adapter.dummy.jobs", passive=True)
        except aio_pika.exceptions.ChannelClosed:
            pytest.skip("the Engine has not declared the topology on this broker yet")
    finally:
        await engine.close()

    worker = await aio_pika.connect(env("WORKER_AMQP_URL"), timeout=10)
    try:
        # Allowed: look its queue up without creating it, and reach the events exchange.
        channel = await worker.channel()
        await channel.get_queue("adapter.dummy.jobs", ensure=True)
        await channel.get_exchange("courier.events", ensure=False)

        async def refused(action: Any) -> None:
            fresh = await worker.channel(publisher_confirms=True)
            with pytest.raises(aiormq.exceptions.ChannelAccessRefused):
                await action(fresh)

        async def publish_job(ch: AbstractChannel) -> None:
            exchange = await ch.get_exchange("courier.jobs", ensure=False)
            await exchange.publish(aio_pika.Message(b"{}"), "job.track.v1.dummy")

        async def read_engine_events(ch: AbstractChannel) -> None:
            queue = await ch.get_queue("engine.events", ensure=False)
            await queue.get(fail=False)

        await refused(lambda ch: ch.declare_queue(f"adapter.probe{secrets.token_hex(4)}.jobs", durable=True))
        await refused(lambda ch: ch.declare_exchange("courier.probe", "topic"))
        await refused(publish_job)
        await refused(read_engine_events)
    finally:
        await worker.close()


def test_the_env_helper_reads_the_repository_env_file() -> None:
    assert Path(REPO / ".env").exists()
    assert env("WORKER_AMQP_URL").startswith("amqp://")
