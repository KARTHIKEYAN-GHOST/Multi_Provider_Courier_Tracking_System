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
import json
import os
import secrets
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
    JOB_INTERVENTION_REQUIRED,
    JOB_RESULT,
    JOB_RESUME,
    JOB_STARTED,
    MAX_MESSAGE_BYTES,
    SCHEMA_VERSION,
    ErrorClass,
    ErrorInfo,
    InterventionRequired,
    InterventionRequiredEnvelope,
    JobResult,
    JobResultEnvelope,
    JobResume,
    JobResumeEnvelope,
    JobStarted,
    JobStartedEnvelope,
    JobTrack,
    JobTrackEnvelope,
    Metrics,
    Outcome,
    new_id,
    utc_now,
)
from .credentials import CredentialDenied, CredentialResolver, HttpCredentialResolver
from .errors import SavedSessionLost, ShipmentNotFound, describe
from .interventions import Challenge, NeedsHuman
from .logging import get_logger
from .sessions import RedisSessionStore, SessionStore, SessionStoreUnavailable
from .testing import FaultInjection, SimulateCrash, SimulateDuplicateResult, SimulateHang
from .topology import WorkerRoutes, routes_for

#: Exit status used when a test adapter asks the worker to crash.
CRASH_EXIT_CODE = 70
#: A session outlives the challenge's own lifetime by this much, so that an answer given at
#: the last moment can still be used after the resume message waited in the queue.
SESSION_MARGIN_S = 900


def _parse_job(body: bytes) -> JobTrackEnvelope | JobResumeEnvelope:
    """Both job messages arrive on one queue; the ``type`` field picks the model. Anything
    that is not a readable ``job.resume`` is validated (and, if wrong, refused) as ``job.track``."""
    try:
        head = json.loads(body)
    except ValueError:
        head = None
    if isinstance(head, dict) and head.get("type") == JOB_RESUME:
        return JobResumeEnvelope.model_validate_json(body)
    return JobTrackEnvelope.model_validate_json(body)


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


class _AlreadyReported(Exception):
    """This resume's outcome already reached the broker; the delivery is a leftover copy."""


@dataclass(frozen=True)
class _Lease:
    """The right to continue one session. ``owner`` is a secret of this execution."""

    session_ref: str
    owner: str = field(repr=False)


@dataclass
class _Execution:
    """One physical execution of a job message."""

    envelope: JobTrackEnvelope | JobResumeEnvelope
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
        resolver: CredentialResolver | None = None,
        sessions: SessionStore | None = None,
        exit_process: Callable[[int], NoReturn] = _hard_exit,
    ) -> None:
        self.adapter = adapter
        self.settings = settings
        self.publisher = publisher
        self.resolver = resolver
        self.sessions = sessions
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
            credential_ref=job.credential_ref,
            _resolver=self.resolver,
        )
        lease: _Lease | None = None
        try:
            refused: _Outcome | None = None
            if isinstance(job, JobResume) and self.sessions is not None:
                # Only one execution at a time may continue a session (see _take_lease).
                try:
                    lease = await self._take_lease(execution, job)
                except _AlreadyReported:
                    # Another delivery of this resume finished and its outcome is with the
                    # broker. Nothing is lost by acknowledging this copy.
                    execution.log.info("resume_already_reported")
                    await delivery.ack()
                    return
                except SessionStoreUnavailable as error:
                    # Without the store nobody can tell who is continuing: do not execute.
                    refused = _Outcome(Outcome.FAILURE, error=describe(error))
            started_at = utc_now()
            clock = time.monotonic()
            await self._publish_started(execution, started_at)
            result = (False, refused) if refused is not None else await self._execute(execution, ctx)
            if result is None:  # a simulated hang: nothing more happens for this job
                return
            duplicate, outcome = result
            if outcome.challenge is not None:
                await self._publish_intervention(execution, started_at, clock, outcome)
            else:
                await self._publish_result(execution, started_at, clock, outcome, duplicate=duplicate)
            if lease is not None:
                await self._mark_reported(execution, lease)
            await delivery.ack()
            if lease is not None and self.sessions is not None:
                # Reported and acknowledged: the session this execution continued is used up.
                await self.sessions.delete(lease.session_ref)
        except SessionLost:
            # Not acknowledged. The session owner drops the connection, RabbitMQ returns the
            # message and counts the failed delivery.
            self.session_lost.set()
        finally:
            # Also on errors, cancellation and shutdown: nothing here may keep a lease.
            try:
                if lease is not None:
                    await self._release_lease(execution, lease)
            finally:
                await ctx._close(self.settings.worker_cleanup_timeout_s)

    # ------------------------------------------------------------------ resume lease

    def _lease_lifetime_s(self, job: JobTrack) -> float:
        """How long a resume lease lasts: the adapter's execution budget (enforced by
        ``asyncio.timeout``, and including loading the session and fetching the answer) plus
        a margin for the start notice, the result and the acknowledgement. An execution
        therefore finishes inside its lease unless the process stalls; a lease that outlives
        its worker is gone after this time at the latest."""
        return job.timeout_ms / 1000 + self.settings.worker_resume_lease_margin_s

    async def _take_lease(self, execution: _Execution, job: JobResume) -> _Lease:
        """Waits for the exclusive right to continue the session of a ``job.resume``.

        RabbitMQ may deliver the same resume to two workers at once. Without this, both
        would load the session, fetch the user's answer and submit it to the provider.

        * Lease free: take it and execute.
        * Lease taken: another execution is running. This one waits, keeping its message
          unacknowledged, and looks again. It ends in one of three ways: the other execution
          reports (this copy is acknowledged), the other execution fails or its worker dies
          (the lease is released or expires and this one executes), or the wait runs out
          (the message goes back to the broker, as when a result cannot be published).

        Raises ``_AlreadyReported``, ``SessionStoreUnavailable`` or ``SessionLost``.
        """
        sessions = self.sessions
        assert sessions is not None
        lifetime_s = self._lease_lifetime_s(job)
        # A lease is released or has expired after one lifetime; twice that covers a second
        # execution that took it first.
        give_up = time.monotonic() + 2 * lifetime_s
        waiting = False
        while True:
            if await sessions.was_reported(job.session_ref):
                raise _AlreadyReported
            owner = await sessions.acquire_lease(job.session_ref, lifetime_s)
            if owner is not None:
                lease = _Lease(job.session_ref, owner)
                # The previous holder may have reported between the two calls above.
                try:
                    reported = await sessions.was_reported(job.session_ref)
                except BaseException:
                    await self._release_lease(execution, lease)
                    raise
                if reported:
                    await self._release_lease(execution, lease)
                    raise _AlreadyReported
                if waiting:
                    execution.log.info("resume_lease_taken_after_waiting")
                return lease
            if not waiting:
                waiting = True
                execution.log.info("resume_deferred", reason="another execution is continuing this session")
            if time.monotonic() >= give_up:
                execution.log.error("resume_lease_wait_exhausted", waited_s=round(2 * lifetime_s, 1))
                raise SessionLost("the resume lease stayed taken")
            await asyncio.sleep(self.settings.worker_resume_lease_poll_s)

    async def _mark_reported(self, execution: _Execution, lease: _Lease) -> None:
        """After the broker confirmed the outcome and before the acknowledgement: a copy of
        this resume that arrives later is recognized instead of being executed again."""
        assert self.sessions is not None
        try:
            await self.sessions.mark_reported(lease.session_ref, self.settings.worker_session_ttl_s)
        except Exception as error:
            # The outcome is reported; only the shortcut for later copies is missing.
            execution.log.warning("resume_not_marked_reported", error_type=type(error).__name__)

    async def _release_lease(self, execution: _Execution, lease: _Lease) -> None:
        """Gives the lease back. Shielded and bounded like job cleanup: a cancelled job still
        releases it, and a store that does not answer cannot hold up shutdown (the lease then
        expires by itself). The owner token is never logged."""
        assert self.sessions is not None
        timeout_s = self.settings.worker_cleanup_timeout_s
        releasing = asyncio.ensure_future(self.sessions.release_lease(lease.session_ref, lease.owner))
        try:
            released = await asyncio.wait_for(asyncio.shield(releasing), timeout_s)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(releasing, timeout_s)
            raise
        except Exception as error:
            execution.log.warning("resume_lease_not_released", error_type=type(error).__name__)
            return
        if not released:
            # It expired (or the store was unreachable) before this execution was done.
            execution.log.warning("resume_lease_was_no_longer_held")

    async def _accept(self, delivery: Delivery) -> _Execution | None:
        """Validates the message and decides whether to execute it."""
        if len(delivery.body) > MAX_MESSAGE_BYTES:
            self.log.warning("job_rejected", reason="message exceeds the size limit")
            await delivery.reject()
            return None
        try:
            envelope = _parse_job(delivery.body)
        except ValidationError as error:
            # Field names and rule names only: message content is not copied into the log.
            reasons = sorted({f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in error.errors()})
            self.log.warning("job_rejected", reason="invalid job message", details=reasons[:10])
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
                    answer = await self._call_adapter(job, request, ctx)
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
        except NeedsHuman as pause:
            try:
                return False, await self._pause(pause.challenge)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                execution.log.error("challenge_not_saved", error_type=type(error).__name__)
                return False, _Outcome(Outcome.FAILURE, error=describe(error))
        except ShipmentNotFound:
            return False, _Outcome(Outcome.NOT_FOUND)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            info = describe(error)
            if info.error_class in (ErrorClass.INTERNAL, ErrorClass.UNKNOWN):
                execution.log.exception("adapter_raised_unexpected_error")
            return False, _Outcome(Outcome.FAILURE, error=info)

    async def _call_adapter(self, job: JobTrack, request: TrackRequest, ctx: JobContext) -> TrackingResult:
        """A new lookup calls ``track``. A resumed one loads the saved session, fetches the
        user's answer from the Engine and calls ``resume``. The answer lives only in this
        call: it is not logged, not stored and not put into any message."""
        if not isinstance(job, JobResume):
            return await self.adapter.track(request, ctx)
        if self.sessions is None:
            raise RuntimeError("this worker has no session store; it cannot resume a lookup")
        if self.resolver is None:
            raise CredentialDenied("this worker is not configured to fetch answers from the Engine")
        session = await self.sessions.load(job.session_ref)
        if session is None:
            raise SavedSessionLost("the session saved for this lookup has expired or is gone")
        response = await self.resolver.intervention_response(
            job.intervention_id, job.session_ref, job.job_id, job.attempt
        )
        return await self.adapter.resume(request, ctx, session, response)

    async def _pause(self, challenge: Challenge) -> _Outcome:
        """Saves the adapter's session under a fresh reference. Only then may the Engine be
        told: a challenge whose session was not stored could never be continued."""
        if self.sessions is None:
            raise RuntimeError("the adapter raised a challenge but this worker has no session store")
        session_ref = new_id()
        ttl_s = self.settings.worker_session_ttl_s
        if challenge.expires_in_s is not None:
            ttl_s = min(ttl_s, challenge.expires_in_s + SESSION_MARGIN_S)
        await self.sessions.save(session_ref, challenge.session, ttl_s)
        return _Outcome(None, challenge=challenge, session_ref=session_ref)

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

    async def _publish_intervention(
        self, execution: _Execution, started_at: datetime, clock: float, outcome: _Outcome
    ) -> None:
        """Tells the Engine this execution stopped for a human answer. The message carries the
        prompt, the picture (CAPTCHA) and the session *reference* — never the session."""
        challenge = outcome.challenge
        assert challenge is not None
        assert outcome.session_ref is not None
        payload = InterventionRequired(
            **self._identity(execution, started_at),
            finished_at=utc_now(),
            kind=challenge.kind,
            prompt=challenge.prompt,
            session_ref=outcome.session_ref,
            expires_in_s=challenge.expires_in_s,
            response_format=challenge.response_format,
            image=challenge.image,
        )
        message = InterventionRequiredEnvelope.model_validate(
            {**self._envelope(execution, JOB_INTERVENTION_REQUIRED), "payload": payload}
        )
        body = message.to_bytes()
        if len(body) > MAX_MESSAGE_BYTES:
            failure = _Outcome(
                Outcome.FAILURE,
                error=ErrorInfo(
                    error_class=ErrorClass.INTERNAL,
                    message=f"challenge of {len(body)} bytes exceeds the message size limit",
                    retryable=False,
                ),
            )
            execution.log.error("challenge_too_large", size=len(body))
            # If this raises (the failure could not be published) the session stays: the
            # message is redelivered, and whatever is then left expires by itself.
            await self._publish_result(execution, started_at, clock, failure, duplicate=False)
            # Reported: nobody will ever be asked this question, so nobody will resume it.
            if self.sessions is not None:
                await self.sessions.delete(outcome.session_ref)
            return
        try:
            await self.publisher.publish(
                JOB_INTERVENTION_REQUIRED, body, message.message_id, message.correlation_id
            )
        except Exception as error:
            execution.log.error("intervention_not_published", error=repr(error))
            raise SessionLost from error
        execution.log.info(
            "job_waiting_for_human",
            kind=challenge.kind,
            session_ref=outcome.session_ref,
            duration_ms=int((time.monotonic() - clock) * 1000),
        )

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
        assert outcome.outcome is not None
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
    #: ``None`` when the execution stopped for a human answer instead of finishing.
    outcome: Outcome | None
    answer: TrackingResult | None = None
    error: ErrorInfo | None = None
    challenge: Challenge | None = None
    session_ref: str | None = None


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
        self._keys = {
            JOB_STARTED: routes.started_routing_key,
            JOB_RESULT: routes.result_routing_key,
            JOB_INTERVENTION_REQUIRED: routes.intervention_routing_key,
        }
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
        # One resolver (and connection pool) for the whole process, if the worker has a token.
        self.resolver: HttpCredentialResolver | None = None
        if settings.worker_engine_internal_url and settings.worker_token is not None:
            self.resolver = HttpCredentialResolver(
                settings.worker_engine_internal_url, settings.worker_token.get_secret_value()
            )
        # One session store for the whole process, if the adapter may wait for human answers.
        self.sessions: SessionStore | None = None
        store_url = (
            settings.worker_session_store_url.get_secret_value() if settings.worker_session_store_url else ""
        )
        session_key = settings.worker_session_key.get_secret_value() if settings.worker_session_key else ""
        if store_url and session_key:
            self.sessions = RedisSessionStore(
                store_url,
                session_key,
                prefix=f"{settings.worker_topology_prefix}courier:session:{adapter.key}:",
                control_prefix=f"{settings.worker_topology_prefix}courier:resume:{adapter.key}:",
            )
        elif store_url or session_key:
            # Half configured: the worker runs, but a lookup that needs a person will fail.
            self.log.warning(
                "session_store_disabled",
                reason="WORKER_SESSION_STORE_URL and WORKER_SESSION_KEY must both be set",
            )
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
            if self.resolver is not None:
                await self.resolver.aclose()
            if self.sessions is not None:
                await self.sessions.aclose()
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
                resolver=self.resolver,
                sessions=self.sessions,
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
