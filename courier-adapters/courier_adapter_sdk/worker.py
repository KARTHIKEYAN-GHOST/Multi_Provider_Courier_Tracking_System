"""The worker runtime: runs an adapter's jobs concurrently and reports to the Engine.

Delivery is at-least-once. The worker never assumes a message arrives exactly once, and it
acknowledges a job message only after its result has been confirmed by the broker.

Concurrency model
    RabbitMQ's prefetch only limits how many unacknowledged messages the broker hands over;
    how they run is decided here. Each delivery takes a slot from a semaphore of size
    ``WORKER_CONCURRENCY`` and runs as its own asyncio task. The consume loop never waits for
    a job, so one slow job cannot hold up the others.

Per-job sequence
    validate → drop if expired → publish ``job.started`` → run the adapter under a timeout →
    publish ``job.result`` (confirmed) → acknowledge.

When the result cannot be published
    The message is left unacknowledged and the broker session is ended. RabbitMQ then returns
    the message and counts the failed delivery, so the queue's delivery limit bounds the
    loop. (An explicit nack-with-requeue is NOT counted by RabbitMQ 4 quorum queues and would
    loop forever; this module never uses it.)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn, Protocol

import aio_pika
import structlog
from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError

from .base import Adapter, TrackingResult, TrackRequest
from .config import WorkerSettings
from .context import JobContext
from .contracts import (
    JOB_RESULT,
    JOB_STARTED,
    MAX_MESSAGE_BYTES,
    SCHEMA_VERSION,
    ErrorClass,
    ErrorInfo,
    JobResult,
    JobResultEnvelope,
    JobStarted,
    JobStartedEnvelope,
    JobTrackEnvelope,
    Metrics,
    Outcome,
    new_id,
    utc_now,
)
from .errors import ShipmentNotFound, describe
from .logging import get_logger
from .testing import FaultInjection, SimulateCrash, SimulateDuplicateResult, SimulateHang
from .topology import WorkerRoutes, routes_for

#: Exit status used when a test adapter asks the worker to crash.
CRASH_EXIT_CODE = 70


class Delivery(Protocol):
    """One message from the job queue. Deliberately has no ``nack``: see the module docstring."""

    @property
    def body(self) -> bytes: ...

    @property
    def delivery_count(self) -> int:
        """How many times the broker delivered this message before. 0 on the first delivery."""
        ...

    async def ack(self) -> None: ...

    async def reject(self) -> None:
        """Refuse without requeue: the broker dead-letters the message."""
        ...


class EventPublisher(Protocol):
    """Publishes a worker event and returns only when the broker has confirmed it."""

    async def publish(self, message_type: str, body: bytes, message_id: str, correlation_id: str) -> None: ...


class SessionLost(Exception):
    """The broker session must be torn down (a result could not be published)."""


@dataclass
class _Execution:
    """One physical execution of a job message."""

    envelope: JobTrackEnvelope
    execution_id: str
    delivery_count: int
    log: structlog.stdlib.BoundLogger


def _hard_exit(code: int) -> NoReturn:
    os._exit(code)


class Worker:
    """Runs deliveries for one adapter. Independent of RabbitMQ: it works on the ``Delivery``
    and ``EventPublisher`` interfaces, which is what makes it testable without a broker."""

    def __init__(
        self,
        adapter: Adapter,
        settings: WorkerSettings,
        publisher: EventPublisher,
        *,
        exit_process: Callable[[int], NoReturn] = _hard_exit,
    ) -> None:
        self.adapter = adapter
        self.settings = settings
        self.publisher = publisher
        self.worker_id = settings.worker_id or f"{adapter.key}-{secrets.token_hex(4)}"
        self.log = get_logger(worker_id=self.worker_id, adapter_key=adapter.key)
        self._slots = asyncio.Semaphore(settings.worker_concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        self._exit_process = exit_process
        #: Set when a result could not be published; the session owner must reconnect.
        self.session_lost = asyncio.Event()
        self._producer = f"adapter-{adapter.key}@{adapter.version}"

    # ------------------------------------------------------------------ task handling

    @property
    def running(self) -> int:
        """Number of jobs executing right now."""
        return len(self._tasks)

    @property
    def free_slots(self) -> int:
        return self.settings.worker_concurrency - self.running

    async def submit(self, delivery: Delivery) -> None:
        """Starts a job for the delivery as soon as a concurrency slot is free.

        Returns once the job has *started*; it never waits for the job to finish.
        """
        await self._slots.acquire()
        task = asyncio.create_task(self._run(delivery))
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._slots.release()
        if not task.cancelled() and (error := task.exception()) is not None:
            self.log.error("job_task_crashed", error=repr(error))

    async def drain(self, grace_s: float) -> None:
        """Waits up to ``grace_s`` for running jobs, then cancels the rest. Cleanup still runs
        for cancelled jobs; their messages stay unacknowledged and return to the queue."""
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=grace_s)
        if pending:
            self.log.warning("cancelling_jobs_after_grace_period", count=len(pending))
            for task in pending:
                task.cancel()
            await asyncio.wait(pending)

    # ------------------------------------------------------------------ one job

    async def _run(self, delivery: Delivery) -> None:
        execution = await self._accept(delivery)
        if execution is None:
            return
        job = execution.envelope.payload
        ctx = JobContext(
            job_id=job.job_id,
            attempt=job.attempt,
            execution_id=execution.execution_id,
            delivery_count=execution.delivery_count,
            correlation_id=execution.envelope.correlation_id,
            log=execution.log,
        )
        started_at = utc_now()
        clock = time.monotonic()
        try:
            await self._publish_started(execution, started_at)
            result = await self._execute(execution, ctx)
            if result is None:  # a simulated hang: nothing more happens for this job
                return
            duplicate, result_message = result
            await self._publish_result(execution, started_at, clock, result_message, duplicate=duplicate)
            await delivery.ack()
        except SessionLost:
            # Not acknowledged. The session owner drops the connection, RabbitMQ returns the
            # message and counts the failed delivery.
            self.session_lost.set()
        finally:
            await ctx._close(self.settings.worker_cleanup_timeout_s)

    async def _accept(self, delivery: Delivery) -> _Execution | None:
        """Validates the message and decides whether to execute it."""
        if len(delivery.body) > MAX_MESSAGE_BYTES:
            self.log.warning("job_rejected", reason="message exceeds the size limit")
            await delivery.reject()
            return None
        try:
            envelope = JobTrackEnvelope.model_validate_json(delivery.body)
        except ValidationError as error:
            # Field names and rule names only: message content is not copied into the log.
            reasons = sorted({f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in error.errors()})
            self.log.warning("job_rejected", reason="invalid job.track message", details=reasons[:10])
            await delivery.reject()
            return None
        job = envelope.payload
        log = self.log.bind(
            correlation_id=envelope.correlation_id,
            job_id=job.job_id,
            attempt=job.attempt,
            delivery_count=delivery.delivery_count,
        )
        if job.adapter_key != self.adapter.key:
            log.warning("job_rejected", reason="message is for another adapter")
            await delivery.reject()
            return None

        # Queue-wait deadline. A FIRST delivery that arrives after expires_at is dropped: the
        # Engine has closed, or is about to close, this attempt. A redelivery is always
        # executed, because an earlier execution may already have told the Engine it started.
        deadline = job.expires_at + timedelta(seconds=self.settings.worker_expiry_skew_s)
        if delivery.delivery_count == 0 and utc_now() > deadline:
            log.info("job_dropped_expired", expires_at=job.expires_at.isoformat())
            await delivery.ack()
            return None

        execution_id = new_id()
        return _Execution(
            envelope, execution_id, delivery.delivery_count, log.bind(execution_id=execution_id)
        )

    async def _execute(self, execution: _Execution, ctx: JobContext) -> tuple[bool, _Outcome] | None:
        """Runs the adapter under the job's execution budget and classifies what happened.

        The budget starts now, when execution begins. Time the message spent in the queue or
        waiting for a slot is not deducted.
        """
        job = execution.envelope.payload
        request = TrackRequest(job.provider_code, job.tracking_number, dict(job.params))
        execution.log.info("job_started", timeout_ms=job.timeout_ms)
        duplicate = False
        try:
            try:
                async with asyncio.timeout(job.timeout_ms / 1000):
                    answer = await self.adapter.track(request, ctx)
            except FaultInjection as fault:
                if not self.settings.worker_allow_fault_injection:
                    raise RuntimeError("fault injection is disabled") from fault
                if isinstance(fault, SimulateCrash):
                    execution.log.warning("simulated_crash")
                    self._exit_process(CRASH_EXIT_CODE)
                if isinstance(fault, SimulateHang):
                    execution.log.warning("simulated_hang")
                    await asyncio.Event().wait()  # until shutdown cancels this task
                    return None
                if not isinstance(fault, SimulateDuplicateResult):
                    raise RuntimeError(f"unknown fault {type(fault).__name__}") from fault
                answer, duplicate = fault.result, True
            return duplicate, _Outcome(Outcome.SUCCESS, answer=answer)
        except ShipmentNotFound:
            return False, _Outcome(Outcome.NOT_FOUND)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            info = describe(error)
            if info.error_class in (ErrorClass.INTERNAL, ErrorClass.UNKNOWN):
                execution.log.exception("adapter_raised_unexpected_error")
            return False, _Outcome(Outcome.FAILURE, error=info)

    # ------------------------------------------------------------------ messages

    def _identity(self, execution: _Execution, started_at: datetime) -> dict[str, Any]:
        job = execution.envelope.payload
        return {
            "job_id": job.job_id,
            "attempt": job.attempt,
            "execution_id": execution.execution_id,
            "delivery_count": execution.delivery_count,
            "worker_id": self.worker_id,
            "adapter_key": self.adapter.key,
            "adapter_version": self.adapter.version,
            "started_at": started_at,
        }

    def _envelope(self, execution: _Execution, message_type: str) -> dict[str, Any]:
        return {
            "message_id": new_id(),
            "type": message_type,
            "schema_version": SCHEMA_VERSION,
            "occurred_at": utc_now(),
            # The Engine's correlation ID is carried through unchanged.
            "correlation_id": execution.envelope.correlation_id,
            "causation_id": execution.envelope.message_id,
            "producer": self._producer,
        }

    async def _publish_started(self, execution: _Execution, started_at: datetime) -> None:
        message = JobStartedEnvelope.model_validate(
            {
                **self._envelope(execution, JOB_STARTED),
                "payload": JobStarted.model_validate(self._identity(execution, started_at)),
            }
        )
        try:
            await self.publisher.publish(
                JOB_STARTED, message.to_bytes(), message.message_id, message.correlation_id
            )
        except Exception as error:
            # job.started is advisory: the result carries everything the Engine needs, so a
            # lost start notice must not stop the job.
            execution.log.warning("job_started_not_published", error=repr(error))

    async def _publish_result(
        self,
        execution: _Execution,
        started_at: datetime,
        clock: float,
        outcome: _Outcome,
        *,
        duplicate: bool,
    ) -> None:
        duration_ms = int((time.monotonic() - clock) * 1000)
        payload = JobResult(
            **self._identity(execution, started_at),
            finished_at=utc_now(),
            outcome=outcome.outcome,
            raw_response=outcome.answer.raw_response if outcome.answer else None,
            events=list(outcome.answer.events) if outcome.answer else None,
            error=outcome.error,
            metrics=Metrics(duration_ms=duration_ms),
        )
        message = JobResultEnvelope.model_validate(
            {**self._envelope(execution, JOB_RESULT), "payload": payload}
        )
        body = message.to_bytes()
        if len(body) > MAX_MESSAGE_BYTES:
            # The answer is too large for the contract. Report that instead of sending it.
            execution.log.error("result_too_large", size=len(body))
            payload = payload.model_copy(
                update={
                    "outcome": Outcome.FAILURE,
                    "raw_response": None,
                    "events": None,
                    "error": ErrorInfo(
                        error_class=ErrorClass.INTERNAL,
                        message=f"result of {len(body)} bytes exceeds the message size limit",
                        retryable=False,
                    ),
                }
            )
            message = JobResultEnvelope.model_validate(
                {**self._envelope(execution, JOB_RESULT), "payload": payload}
            )
            body = message.to_bytes()
        try:
            for _ in range(2 if duplicate else 1):
                await self.publisher.publish(JOB_RESULT, body, message.message_id, message.correlation_id)
        except Exception as error:
            execution.log.error("job_result_not_published", error=repr(error))
            raise SessionLost from error
        execution.log.info(
            "job_finished",
            outcome=payload.outcome.value,
            error_class=payload.error.error_class.value if payload.error else None,
            duration_ms=duration_ms,
        )


@dataclass(frozen=True)
class _Outcome:
    outcome: Outcome
    answer: TrackingResult | None = None
    error: ErrorInfo | None = None


# ---------------------------------------------------------------------- RabbitMQ transport


class _AmqpDelivery:
    """Adapts an aio-pika message to the ``Delivery`` interface."""

    def __init__(self, message: AbstractIncomingMessage) -> None:
        self._message = message

    @property
    def body(self) -> bytes:
        return self._message.body

    @property
    def delivery_count(self) -> int:
        # Quorum queues report earlier failed deliveries in this header (absent on the first).
        count = self._message.headers.get("x-delivery-count", 0)
        if isinstance(count, int) and count > 0:
            return count
        return 1 if self._message.redelivered else 0

    async def ack(self) -> None:
        await self._message.ack()

    async def reject(self) -> None:
        await self._message.reject(requeue=False)


class AmqpPublisher:
    """Publishes worker events on a channel with publisher confirms."""

    def __init__(
        self, exchange: aio_pika.abc.AbstractExchange, routes: WorkerRoutes, timeout_s: float = 10.0
    ) -> None:
        self._exchange = exchange
        self._keys = {JOB_STARTED: routes.started_routing_key, JOB_RESULT: routes.result_routing_key}
        self._timeout_s = timeout_s

    async def publish(self, message_type: str, body: bytes, message_id: str, correlation_id: str) -> None:
        message = aio_pika.Message(
            body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=message_id,
            correlation_id=correlation_id,
            type=message_type,
        )
        # mandatory + confirms: returns only when the broker has routed and stored the message.
        await self._exchange.publish(
            message, self._keys[message_type], mandatory=True, timeout=self._timeout_s
        )


class WorkerProcess:
    """Owns the RabbitMQ session for one adapter: connect, consume, reconnect, shut down."""

    def __init__(
        self,
        adapter: Adapter,
        settings: WorkerSettings,
        *,
        exit_process: Callable[[int], NoReturn] = _hard_exit,
    ) -> None:
        self.adapter = adapter
        self.settings = settings
        self.routes = routes_for(adapter.key, settings.topology_path, settings.worker_topology_prefix)
        self.log = get_logger(adapter_key=adapter.key)
        self._stopping = asyncio.Event()
        self._exit_process = exit_process
        #: Set while a session is consuming. Tests wait on it.
        self.consuming = asyncio.Event()

    def stop(self) -> None:
        """Begin a graceful shutdown (also what SIGTERM does)."""
        self._stopping.set()

    async def run(self) -> None:
        """Runs until ``stop()``; reconnects with a capped back-off when the session is lost."""
        await self.adapter.startup()
        try:
            delay = 0.5
            while not self._stopping.is_set():
                began = time.monotonic()
                try:
                    await self._session()
                except Exception as error:
                    self.log.error("broker_session_ended", error=repr(error), retry_in_s=delay)
                if self._stopping.is_set():
                    break
                if time.monotonic() - began > 30:
                    delay = 0.5
                await self._sleep_or_stop(delay)
                delay = min(delay * 2, 15.0)
        finally:
            await self.adapter.shutdown()
            self.log.info("worker_stopped")

    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)

    async def _session(self) -> None:
        connection = await aio_pika.connect(self.settings.worker_amqp_url.get_secret_value(), timeout=10)
        try:
            publish_channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            exchange = await publish_channel.get_exchange(self.routes.events_exchange, ensure=False)
            worker = Worker(
                self.adapter,
                self.settings,
                AmqpPublisher(exchange, self.routes),
                exit_process=self._exit_process,
            )

            consume_channel = await connection.channel()
            await consume_channel.set_qos(prefetch_count=self.settings.prefetch)
            # Passive lookup: fails if the Engine has not declared the queue yet, and the
            # session is retried. The worker never creates topology.
            queue = await consume_channel.get_queue(self.routes.job_queue, ensure=True)

            async def on_message(message: AbstractIncomingMessage) -> None:
                await worker.submit(_AmqpDelivery(message))

            consumer_tag = await queue.consume(on_message)
            self.consuming.set()
            self.log.info(
                "worker_consuming",
                worker_id=worker.worker_id,
                queue=self.routes.job_queue,
                concurrency=self.settings.worker_concurrency,
                prefetch=self.settings.prefetch,
            )

            waiters: list[asyncio.Future[Any]] = [
                asyncio.ensure_future(self._stopping.wait()),
                asyncio.ensure_future(worker.session_lost.wait()),
                asyncio.ensure_future(connection.closed()),
            ]
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for waiter in waiters:
                waiter.cancel()
            self.consuming.clear()

            if self._stopping.is_set() and not connection.is_closed:
                # Graceful: stop taking new jobs, let the running ones finish and report.
                await queue.cancel(consumer_tag)
                await worker.drain(self.settings.worker_shutdown_grace_s)
            else:
                # The session is unusable. Running jobs cannot report on it; cancel them
                # (cleanup still runs) and let RabbitMQ redeliver their messages.
                await worker.drain(0)
                if worker.session_lost.is_set():
                    raise SessionLost("a result could not be published")
        finally:
            self.consuming.clear()
            if not connection.is_closed:
                await connection.close()


def install_signal_handlers(process: WorkerProcess) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, process.stop)
