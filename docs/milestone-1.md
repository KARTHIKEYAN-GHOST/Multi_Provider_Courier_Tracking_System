# Milestone 1 — Final Implementation Plan

Rust Engine ↔ Python dummy adapter through RabbitMQ and PostgreSQL.

## Context

The overall architecture and the Milestone 1 scope are approved. This document is the Milestone 1 plan only, updated with the six final clarifications (worker concurrency, DLQ/reaper coordination, logical attempts vs physical executions, no future directories, concurrent API requests, hashed API keys).

The approved system-wide architecture (later phases: credentials, sessions, OTP/CAPTCHA, AI security, repair sandbox, shipment retention) is unchanged. The first step of Phase 1 writes it, together with the sections below, into `docs/architecture.md` so it lives in the repository rather than in this plan file.

**In scope:** Rust Engine, PostgreSQL, RabbitMQ, shared contracts, Python Adapter SDK, dummy adapter, hashed user API keys, Docker Compose, tests, CI.

**Out of scope — no code, service, dependency or directory:** real courier adapters, AI service, repair, Redis, provider credentials, OTP/CAPTCHA, human interface, polling scheduler, circuit breaker, metrics.

Working directory `/home/karthikeyan/courier-tracking-system` is empty and not yet a git repository.

---

## 1. Design rules Milestone 1 implements

### 1.1 Delivery guarantees

At-least-once delivery with idempotent consumers. Exactly-once is never assumed; ordering is never assumed.

| Hop | Possible duplication | Made harmless by |
|---|---|---|
| Outbox → broker | Relay dies after confirm, before mark-sent → re-publish with the same `message_id` | Consumer-side dedupe |
| Broker → worker | Redelivery → the same logical attempt is physically executed more than once | Lookups are read-only; every execution reports the same `(job_id, attempt)` |
| Worker → Engine | Same message twice; two different results for one attempt; late result for an old attempt | Inbox `(consumer, message_id)`; guarded close of the attempt (first wins); event `UNIQUE(shipment_id, dedupe_key)` |
| Engine consumer | Crash after commit, before ack | Inbox row is written in the same transaction as the effects; ack only after commit |

### 1.2 Job lifecycle and the single close path

M1 states: `QUEUED → PROCESSING → COMPLETED | RETRYING | FAILED`, `RETRYING → QUEUED` (attempt + 1). A job is inserted as QUEUED together with its outbox row. (`PENDING`, `WAITING_*`, `EXPIRED`, `CANCELLED` exist in the enum but are unused in M1.)

An attempt can be closed by exactly three actors: the **result handler**, the **dead-letter handler**, and the **reaper**. All three call one function, `close_attempt(job_id, attempt, outcome)`, whose core is a guarded update:

```sql
UPDATE tracking_jobs SET status = $new, ...
WHERE id = $job AND attempt = $attempt AND status IN ('QUEUED','PROCESSING')
```

Whichever actor commits first wins; the others match zero rows and do nothing. `close_attempt` decides the next state in one place: success → COMPLETED; retryable failure with attempts left → RETRYING with `next_attempt_at = base · 2^(attempt−1) ± 20 % jitter` (capped); otherwise FAILED.

Only the **retry scheduler** opens a new attempt: it selects `status = 'RETRYING' AND next_attempt_at <= now()` with `FOR UPDATE SKIP LOCKED` and, in one transaction, sets QUEUED, increments `attempt`, sets a new `queue_deadline_at`, clears `execution_deadline_at`, inserts the `job_attempts` row and the outbox row. Therefore one logical attempt produces exactly one outbox row, and no two actors can schedule competing retries.

### 1.3 Dead-letter queue and reaper coordination

**How a job message reaches the DLQ:** the worker process dies (or nacks) on every delivery of a `job.track` message; when the quorum queue's `x-delivery-limit` is exceeded RabbitMQ dead-letters it to `adapter.dummy.jobs.dlq`. At that moment the job row is still QUEUED (no `job.started` ever arrived) or PROCESSING.

**Dead-letter handler (primary path).** The Engine consumes every `adapter.*.jobs.dlq`. The dead-lettered message still contains the envelope, so the affected job is identified directly by `job_id` + `attempt`; the `x-death` header gives the reason and source queue. In one transaction:

1. Inbox dedupe under consumer name `engine.dead-letter`.
2. Call `close_attempt` with error class `DELIVERY_LIMIT_EXCEEDED`. The counter increment is **part of the same guarded statement**, not a separate update:
   ```sql
   UPDATE tracking_jobs
   SET dead_letter_count = dead_letter_count + 1,
       status = CASE WHEN dead_letter_count + 1 > $max_dead_letter_retries
                       OR attempt >= max_attempts THEN 'FAILED' ELSE 'RETRYING' END,
       last_error_class = 'DELIVERY_LIMIT_EXCEEDED', next_attempt_at = …
   WHERE id = $job AND attempt = $msg_attempt AND status IN ('QUEUED','PROCESSING')
   RETURNING status, dead_letter_count
   ```
   The counter can only move when the dead-lettered message belongs to the job's **current, still-open** attempt. A dead-letter for an attempt that was already closed (by a result or the reaper), or for an older attempt while a newer one is running, matches zero rows: the counter, the status and the running attempt are untouched.
3. Insert a `dead_letters` row (message, headers, reason, job_id, attempt, `applied` = whether step 2 matched a row) for operators — stale dead-letters are still recorded, with `applied = false`.
4. Commit, then ack (the DLQ empties; the database holds the record).

**Retry policy for `DELIVERY_LIMIT_EXCEEDED`.** A worker crash kills every job that process was running, so an innocent job can be dead-lettered because of a poisonous neighbour. The class is therefore retryable, but only `max_dead_letter_retries` times (default 1): the first dead-letter moves the job to RETRYING; when `dead_letter_count` exceeds the limit the job goes to FAILED regardless of remaining attempts. A truly poisonous job thus fails after two dead-letter cycles; an innocent neighbour completes on its retry.

**Reaper (backstop).** For cases where no dead-letter notification arrives — worker hung, result lost, result itself dead-lettered into `engine.events.dlq`, DLQ consumer down, no worker running. Every few seconds:

```sql
SELECT id, attempt, status FROM tracking_jobs
WHERE (status = 'QUEUED'     AND queue_deadline_at     < now())
   OR (status = 'PROCESSING' AND execution_deadline_at < now())
FOR UPDATE SKIP LOCKED
```

and calls `close_attempt` with `QUEUE_WAIT_EXCEEDED` for the first case and `WORKER_LOST` for the second (both retryable, both count as an attempt). The two deadlines and the adapter timeout are separate timers, defined in 1.7.

**No conflicts between the two:**

| Sequence | Result |
|---|---|
| Dead-letter handler first, reaper later | Job is RETRYING/FAILED, or QUEUED with a newer attempt and a new deadline → reaper does not select the old attempt |
| Reaper first, dead-letter for attempt N arrives later (attempt N+1 may already be running) | Guarded statement matches nothing: `dead_letter_count` is **not** incremented, attempt N+1 is not disturbed, no second retry; a `dead_letters` row with `applied = false` is kept |
| Either closes attempt N, then a late result for attempt N arrives | Recorded as a `stale` execution (1.4); job state untouched |
| Old `job.track` for attempt N still in the queue after attempt N+1 was opened | Worker drops it if it is a first delivery past `expires_at`; otherwise runs it and the result is logged as `stale` and ignored |

`engine.events.dlq` (events the Engine itself rejected as malformed) is a parked queue, not auto-consumed; each rejection is logged at WARN with the message ID. Any job affected is finished by the reaper. Every path ends in COMPLETED or FAILED — a job cannot remain QUEUED, PROCESSING or RETRYING forever.

### 1.4 Logical attempts vs physical executions

- A **logical attempt** is `(job_id, attempt)`. It is created by the Engine (dispatch or retry scheduler), has one outbox message, and is closed once.
- A **physical execution** is one run of that message by a worker. Redelivery or a duplicate publish can produce several executions of one attempt. The worker generates an `execution_id` (UUID) per delivery and sends it, with the broker's `delivery_count`, in `job.started` and `job.result`.

| Table | Grain | Key | Content |
|---|---|---|---|
| `job_attempts` | one row per logical attempt | `UNIQUE(job_id, attempt)` | dispatched_at, closed_at, authoritative outcome and error class, `closed_by` (result / dead_letter / reaper), `accepted_execution_id`, `execution_count` |
| `adapter_execution_logs` | one row per physical execution | PK `execution_id`, FK `(job_id, attempt)` | worker_id, adapter_version, delivery_count, started_at, finished_at, reported outcome and error class, metrics, `disposition` |

`disposition` values:
- `incomplete` — the initial value; a start is known but no result has been processed (still running, crashed or hung).
- `accepted` — its result closed the attempt.
- `duplicate` — its result arrived after another execution's result had already closed the same attempt.
- `stale` — its result arrived after the attempt had been closed by the reaper or the dead-letter handler.

A disposition moves only from `incomplete` to one final value, once, and never changes afterwards.

**Message order is not assumed — both handlers upsert on `execution_id`:**

- **`job.result` handler** (does not require a prior `job.started`). In one transaction:
  1. `INSERT INTO adapter_execution_logs (execution_id, job_id, attempt, worker_id, adapter_version, delivery_count, started_at, finished_at, outcome, error_class, metrics) … ON CONFLICT (execution_id) DO UPDATE SET finished_at, outcome, error_class, metrics = EXCLUDED.…, started_at = COALESCE(existing.started_at, EXCLUDED.started_at) WHERE existing.finished_at IS NULL`. `job.result` carries `started_at` and `finished_at`, so the row is complete even if `job.started` never arrives.
  2. `close_attempt`. If it wins: set this row's disposition to `accepted`, and `UPDATE job_attempts SET accepted_execution_id = $execution_id, closed_by = 'result' … WHERE accepted_execution_id IS NULL`; store the raw response and events. If it loses: set disposition to `duplicate` or `stale` per the definitions above; store nothing else.
- **`job.started` handler.** `INSERT … (execution_id, …, started_at, disposition = 'incomplete') ON CONFLICT (execution_id) DO UPDATE SET started_at = COALESCE(existing.started_at, EXCLUDED.started_at)`. On conflict it can only fill a missing `started_at`; it never writes `finished_at`, `outcome`, `error_class` or `disposition`, so a late `job.started` cannot reopen or relabel a finished execution. Its job transition is guarded (`status = 'QUEUED' AND attempt = $n` → PROCESSING), so on a job that is already COMPLETED, RETRYING, FAILED or on a newer attempt it changes nothing.
- `job_attempts.execution_count` is incremented only when the upsert actually inserted a new row (whichever handler got there first), so an execution is counted once regardless of order.
- `accepted_execution_id` is written once (guarded by `IS NULL`) and is never changed by any later message: first result wins.

Aggregation rule: first result to pass the guard is `accepted`; only that execution's raw response and events are stored. Every other execution is still logged with its own disposition, and `job_attempts.execution_count` is maintained in the same transaction. The same message delivered twice (same `message_id`) is stopped by the inbox and creates no second execution row. Example: `DUMMY-CRASHONCE` yields one `job_attempts` row with `execution_count = 2` — one `incomplete`, one `accepted`.

### 1.5 Worker concurrency model

RabbitMQ prefetch only bounds how many unacknowledged messages the broker hands over; the SDK controls how they run.

- **Configuration:** `WORKER_CONCURRENCY` (default 4), `WORKER_PREFETCH` (default = concurrency), `WORKER_SHUTDOWN_GRACE_S` (30), `WORKER_CLEANUP_TIMEOUT_S` (5).
- **Task handling:** one consume loop; for each delivery it acquires an `asyncio.Semaphore(WORKER_CONCURRENCY)` slot and starts an independent `asyncio.Task` for the job, tracked in a set. The loop never awaits a job. A task's completion releases the slot and logs any unexpected exception. Consuming and publishing use separate channels; publishing uses confirms.
- **Per-job sequence:** validate message → drop if it is a first delivery and `expires_at` has passed → publish `job.started` → run `adapter.track` → publish `job.result` (confirmed) → ack. If the result cannot be published the message is nacked for redelivery.
- **Timeout cancellation:** the adapter call runs inside `asyncio.timeout(timeout_ms)`, started when execution begins and measured on the worker's monotonic clock. Time spent waiting in the queue or for a concurrency slot is not deducted — every execution gets the full budget (1.7). On expiry the task is cancelled at its current await point and the SDK publishes a FAILURE result with class `ADAPTER_TIMEOUT` (retryable). Other tasks are unaffected.
- **Resource cleanup:** each job gets a `JobContext` owning an `AsyncExitStack`. Adapters register everything they open (HTTP clients now; browser contexts later) on it. The SDK closes the stack in a `finally` block, shielded from cancellation and bounded by `WORKER_CLEANUP_TIMEOUT_S`, on success, error, timeout and shutdown alike. Process-wide resources use adapter `startup()` / `shutdown()` hooks.
- **Blocking code:** adapters must be async; CPU-heavy or blocking work goes through `asyncio.to_thread`. Tests run with asyncio debug mode so a blocking call in an adapter is reported.
- **Shutdown:** SIGTERM stops consuming, waits up to the grace period for in-flight tasks, then cancels the rest (cleanup still runs); their unacked messages return to the queue.

### 1.6 Shipment model, access control, concurrent requests

- `shipments` is one *generation* of a tracking number: partial unique index `(provider_id, tracking_number, access_scope) WHERE archived_at IS NULL`. `access_scope` is always `public` in M1.
- Users own `tracking_requests` (subscriptions), never shipments. The API is request-centric; every query is filtered by the caller's `user_id`; another user's request ID returns 404. Subscribers see status, events and job summaries — not other subscribers and not raw provider responses.
- **Authentication:** API keys of the form `ctk_<prefix>_<secret>` (256-bit random secret). Only the SHA-256 hash and the prefix are stored; lookup by prefix, constant-time comparison; `last_used_at` updated; revocable. Keys are created by CLI and printed once; never logged.
- **Concurrent `POST /v1/tracking-requests`** — one transaction, constraints as the final authority:
  1. `INSERT INTO shipments … ON CONFLICT (active-shipment index) DO UPDATE SET updated_at = now() RETURNING id` — the upsert locks the shipment row, so concurrent transactions for the same shipment run one after another.
  2. `INSERT INTO tracking_requests … ON CONFLICT (user_id, shipment_id) WHERE status = 'active' DO NOTHING`; if nothing inserted, select the existing row.
  3. If the shipment is not in a terminal status: `INSERT INTO tracking_jobs … ON CONFLICT (shipment_id) WHERE status is non-terminal DO NOTHING`; only if a row was inserted, insert `job_attempts` and the outbox row.
  A residual unique violation is retried once and never surfaces as a 500. `POST …/refresh` is an explicit user action: it runs the job insert of step 3 even for a DELIVERED or RETURNED shipment (automatic lookups for those stay disabled), never creates a second job while one is active, never changes the shipment status itself, and is rejected with 409 for an archived shipment.

### 1.7 Three separate timers

Waiting in a queue, running an adapter, and recovering from a lost worker are different conditions with different clocks, owners and error classes. None is derived from another.

| Timer | Measures | Starts | Budget | Enforced by | On expiry |
|---|---|---|---|---|---|
| **Queue-wait deadline** | Time a dispatched attempt waits before any worker starts it | When the Engine dispatches the attempt | `queue_wait_max_ms` per provider (default 15 min — sized for backlog, unrelated to `timeout_ms`) | Engine reaper, only while the job is `QUEUED` (`queue_deadline_at`) | `QUEUE_WAIT_EXCEEDED` — "no worker capacity", not a provider or adapter fault |
| **Adapter execution timeout** | Time one physical execution spends inside `adapter.track` | When the worker begins executing, after `job.started` | `timeout_ms` per provider, full budget for every execution | Worker (`asyncio.timeout`) | Worker reports FAILURE `ADAPTER_TIMEOUT` |
| **Execution recovery deadline** | Time the Engine waits for a result after it knows an execution started | When the Engine processes `job.started` | `timeout_ms + result_grace_ms` (grace covers cleanup, publish and broker latency; default 30 s) | Engine reaper, only while the job is `PROCESSING` (`execution_deadline_at`) | `WORKER_LOST` — the worker died or hung without reporting |

Rules:
- A job that sat in a busy queue for longer than `timeout_ms` and then ran normally completes on its first attempt with no error. Queue time can never produce `ADAPTER_TIMEOUT` or `WORKER_LOST`.
- `execution_deadline_at` is NULL until `job.started` is processed. A redelivery that starts a new execution of the same open attempt sends a new `job.started`, which moves the deadline forward; this is bounded by the queue's delivery limit.
- `job.track` carries `expires_at` (= `queue_deadline_at`). A worker receiving a **first** delivery after `expires_at` drops it without executing, because the Engine has closed or is about to close that attempt. Redeliveries are always executed. Comparison allows a small clock-skew tolerance; hosts are expected to run NTP.
- If the reaper closes an attempt at the same moment a worker starts it, the guarded close decides: the execution's result is recorded as `stale` and the new attempt proceeds. Work is wasted but state stays correct.
- All three error classes are retryable and bounded by `max_attempts`, but they are reported separately so capacity problems, slow providers and crashed workers are distinguishable.

---

## 2. Files and directories to create

```
courier-tracking-system/
├── .gitignore  .env.example  Makefile  README.md
├── .github/workflows/ci.yml
├── courier-contracts/
│   ├── pyproject.toml  .python-version  uv.lock
│   ├── schemas/v1/{common,envelope,job.track,job.started,job.result}.schema.json
│   ├── fixtures/v1/valid/*.json      fixtures/v1/invalid/*.json
│   ├── topology.yaml
│   └── tests/test_fixtures.py
├── courier-engine/
│   ├── Cargo.toml  Cargo.lock  rust-toolchain.toml  Dockerfile  .sqlx/
│   ├── migrations/0001…0008_*.sql
│   ├── src/
│   │   ├── main.rs  lib.rs  config.rs  telemetry.rs  error.rs  state.rs  cli.rs
│   │   ├── api/{mod,auth,health,tracking_requests,providers,dto}.rs
│   │   ├── domain/{mod,execution_status,shipment_status,error_class,backoff}.rs
│   │   ├── db/{mod,users,providers,shipments,tracking_requests,jobs,attempts,events,outbox,inbox,dead_letters}.rs
│   │   ├── messaging/{mod,contracts,topology,connection,outbox_relay,event_consumer,dead_letter_consumer}.rs
│   │   └── services/{mod,dispatch,close_attempt,results,normalize,retry_scheduler,reaper}.rs
│   └── tests/{api,auth,concurrency,outbox,consumer,dead_letter,reaper,state_machine,contracts}.rs  tests/common/mod.rs
├── courier-adapters/
│   ├── pyproject.toml  .python-version  uv.lock  Dockerfile
│   ├── courier_adapter_sdk/{__init__,base,context,errors,config,logging,topology,worker,testing}.py
│   ├── courier_adapter_sdk/contracts/{__init__,envelope,job,result}.py
│   ├── adapters/dummy/{__init__,adapter}.py
│   └── tests/{test_contracts,test_errors,test_dummy_adapter,test_worker_concurrency,test_worker_integration}.py
├── infrastructure/
│   ├── docker-compose.yml
│   ├── rabbitmq/{rabbitmq.conf,definitions.template.json,entrypoint.sh}
│   ├── postgres/init.sh                 (creates the test database only)
│   └── verify-deps.sh
├── tests-e2e/
│   ├── pyproject.toml  .python-version  uv.lock
│   └── {conftest,test_happy_path,test_failures,test_idempotency,test_access_control,
│        test_concurrent_requests,test_worker_concurrency,test_queue_delay,test_recovery}.py
└── docs/{architecture.md,contracts.md,milestone-1.md}
```

`courier-ai-service/` and `courier-human-interface/` are **not** created; they appear when Phases 9 and 8 begin.

---

## 3. Dependencies to install

Nothing system-wide; no `sudo`, `apt` or global `pip`. Versions are the latest stable at implementation time, pinned by `Cargo.lock` / `uv.lock`.

- **Host, user-level:** `uv python install 3.13` (uv-managed interpreter in uv's own directory; system Python 3.14 untouched; each Python project has its own `.python-version` and `.venv`); `cargo install sqlx-cli --no-default-features --features rustls,postgres` (into `~/.cargo/bin`).
- **Engine crates:** tokio, axum, tower, tower-http (trace, timeout, request-id), serde, serde_json, serde_yaml, sqlx (postgres, runtime-tokio, tls-rustls, uuid, chrono, json, migrate), lapin, uuid (v7), chrono, thiserror, anyhow, tracing, tracing-subscriber (json), envy, dotenvy, validator, sha2, subtle, rand, hex, tokio-util, futures-util, clap. Dev: jsonschema, reqwest.
- **courier-adapters:** aio-pika, pydantic, pydantic-settings, structlog, pyyaml. Dev: pytest, pytest-asyncio, ruff, mypy, jsonschema, types-PyYAML.
- **courier-contracts:** jsonschema, pytest. **tests-e2e:** pytest, pytest-asyncio, httpx, aio-pika, psycopg (read-only assertions).
- **Images:** `postgres:17`, `rabbitmq:4-management`, `rust:1-slim` → `debian:stable-slim` (multi-stage), `python:3.13-slim`.

---

## 4. Database migrations

| File | Creates |
|---|---|
| `0001_enums.sql` | `execution_status` (ten values), `shipment_status` (eight), `job_outcome`, `error_class`, `job_trigger`, `tracking_method`, `attempt_closed_by`, `execution_disposition` |
| `0002_users_api_keys.sql` | `users` (email unique, status); `api_keys` (user_id, prefix unique, key_hash, last_used_at, revoked_at) |
| `0003_providers_adapters.sql` | `providers` (code unique, timeout_ms, queue_wait_max_ms, result_grace_ms, max_attempts, enabled); `adapters` (key unique, queue_name); `adapter_versions`; `provider_status_mappings` UNIQUE(provider_id, raw_status) |
| `0004_shipments_requests.sql` | `shipments` + partial unique `(provider_id, tracking_number, access_scope) WHERE archived_at IS NULL`; `tracking_requests` + partial unique `(user_id, shipment_id) WHERE status = 'active'` |
| `0005_jobs_attempts.sql` | `tracking_jobs` (status, attempt, max_attempts, next_attempt_at, queue_deadline_at, execution_deadline_at, dead_letter_count, correlation_id, outcome, last_error_class) + partial unique on `shipment_id` WHERE status non-terminal + index `(status, next_attempt_at)` + partial indexes on `queue_deadline_at` WHERE QUEUED and `execution_deadline_at` WHERE PROCESSING; `job_attempts` UNIQUE(job_id, attempt); `adapter_execution_logs` PK execution_id |
| `0006_responses_events.sql` | `raw_tracking_responses` (job_id, attempt, execution_id, JSONB body, sha256); `tracking_events` UNIQUE(shipment_id, dedupe_key) |
| `0007_messaging.sql` | `outbox_messages` (partial index on pending by available_at); `processed_messages` PK(consumer, message_id); `dead_letters` (message_id, source_queue, reason, job_id, attempt, applied, payload, headers) |
| `0008_seed_dummy.sql` | provider `dummy`, adapter `dummy` (queue `adapter.dummy.jobs`), dummy status mappings |
| `0009_integrity.sql` | Phase 2.1: job status/timestamp CHECKs, `attempt <= max_attempts`, deferred trigger requiring a `job_attempts` row for every job, triggers forbidding jobs on archived shipments and archiving with an active job, one enabled adapter per provider, cancellation / outbox / API-key / access-scope CHECKs |

IDs are UUIDv7 generated by the Engine. Users and keys are created with `courier-engine admin create-user`, `create-api-key`, `revoke-api-key`; no keys in migrations.

---

## 5. RabbitMQ configuration

- `definitions.template.json` (rendered at container start by `entrypoint.sh`, so passwords come from `.env`): vhost `courier`; an operator account for the management UI; user `engine` (full rights in the vhost); user `adapter_worker` (read `^adapter\.[a-z0-9_]+\.(jobs|resume|control)$`, write `^courier\.events$`, no configure). Passwords from `.env`.
- The Engine declares topology at startup from `courier-contracts/topology.yaml`; workers declare passively and wait for their queue.

| Object | Type / arguments | Binding | Consumer |
|---|---|---|---|
| `courier.jobs` | topic exchange, durable | — | — |
| `courier.events` | topic exchange, durable | — | — |
| `courier.dlx` | topic exchange, durable | — | — |
| `adapter.dummy.jobs` | quorum, `x-delivery-limit: 3`, DLX `courier.dlx`, DL key `adapter.dummy.jobs` | `courier.jobs` ← `job.track.v1.dummy` | dummy workers |
| `engine.events` | quorum, `x-delivery-limit: 5`, DLX `courier.dlx`, DL key `engine.events` | `courier.events` ← `job.started.v1`, `job.result.v1` | Engine event consumer |
| `adapter.dummy.jobs.dlq` | quorum | `courier.dlx` ← `adapter.dummy.jobs` | Engine dead-letter consumer |
| `engine.events.dlq` | quorum | `courier.dlx` ← `engine.events` | none (parked) |

- Publishing: persistent, publisher confirms, `mandatory`; `message_id`, `correlation_id`, `type`, `content_type` also set as AMQP properties.
- Consuming: manual ack. Schema-invalid or unknown-major messages are rejected without requeue (→ DLQ). Transient handler errors are nacked with requeue, bounded by the delivery limit.
- Compose runs the worker with `restart: unless-stopped` so crash scenarios recover on their own.

---

## 6. API endpoints (Engine, port 8080)

All `/v1` routes require `Authorization: Bearer <api-key>`. Every response carries `X-Correlation-Id`. Errors share one shape: `{error: {code, message, details}}`.

| Method & path | Behaviour |
|---|---|
| `GET /healthz` | Process alive |
| `GET /readyz` | Postgres and RabbitMQ reachable |
| `GET /v1/providers` | Enabled providers |
| `POST /v1/tracking-requests` | Body `{provider_code, tracking_number, reference?}`; transaction per 1.6. 201 for a new subscription, 200 if the caller already has one. 404 unknown provider, 422 invalid input |
| `GET /v1/tracking-requests` | Caller's requests, cursor-paginated |
| `GET /v1/tracking-requests/{id}` | Request + shipment status + latest job summary |
| `GET /v1/tracking-requests/{id}/events` | Event history: raw status, normalized status, time, location |
| `GET /v1/tracking-requests/{id}/jobs` | Jobs with status, attempt, error class, execution count |
| `POST /v1/tracking-requests/{id}/refresh` | Explicit lookup. 202 `result: job_created`; 200 `result: job_already_active` (that job is returned); 409 `shipment_archived`; 409 `provider_unavailable`. Body also carries `shipment_status` and `shipment_terminal`. Allowed for DELIVERED / RETURNED shipments |
| `DELETE /v1/tracking-requests/{id}` | Cancels the caller's subscription only; 204 |

Missing, malformed, unknown or revoked key → 401. Any `{id}` not owned by the caller → 404.

---

## 7. Message schemas (v1)

**Envelope** — required `message_id`, `type`, `schema_version` (`"1.0"`), `occurred_at` (RFC 3339 UTC), `correlation_id`, `producer`, `payload`; optional `causation_id`.

**`job.track`** (Engine → worker)
```json
{ "job_id": "uuid", "attempt": 1, "adapter_key": "dummy", "provider_code": "dummy",
  "tracking_number": "DUMMY-OK-1", "params": {}, "timeout_ms": 10000,
  "expires_at": "2026-11-24T10:15:00Z" }
```
`timeout_ms` is the adapter execution budget; `expires_at` is the queue-wait deadline. They are independent (1.7).

**`job.started`** (worker → Engine)
```json
{ "job_id": "uuid", "attempt": 1, "execution_id": "uuid", "delivery_count": 0,
  "worker_id": "dummy-7f3a", "adapter_key": "dummy", "adapter_version": "0.1.0", "started_at": "…" }
```

**`job.result`** (worker → Engine)
```json
{ "job_id": "uuid", "attempt": 1, "execution_id": "uuid", "delivery_count": 0,
  "worker_id": "dummy-7f3a", "adapter_key": "dummy", "adapter_version": "0.1.0",
  "started_at": "…", "finished_at": "…",
  "outcome": "SUCCESS | NOT_FOUND | FAILURE",
  "raw_response": { "content_type": "application/json", "body": {} },
  "events": [ { "raw_status": "PKG_DELIVERED", "raw_description": "Delivered",
                "occurred_at": "…", "location": { "city": "…", "country": "…" } } ],
  "error": { "class": "ADAPTER_TIMEOUT", "message": "…", "retryable": true, "retry_after_ms": null },
  "metrics": { "duration_ms": 120 } }
```

Schema rules: `raw_response` and `events` required when `outcome = SUCCESS`; `error` required when `FAILURE`, forbidden otherwise; message ≤ 256 KB; unknown fields ignored on both sides; unknown major version rejected.

`error.class` enum: `NETWORK_TIMEOUT, NETWORK_CONNECT, HTTP_5XX, RATE_LIMITED, BLOCKED, AUTH_INVALID, AUTH_EXPIRED, PARSE_ERROR, SCHEMA_DRIFT, ADAPTER_TIMEOUT, INTERNAL, UNKNOWN`. Assigned only by the Engine, never sent by workers: `DELIVERY_LIMIT_EXCEEDED`, `QUEUE_WAIT_EXCEEDED`, `WORKER_LOST`.

**Dummy adapter behaviours** (tracking-number prefix): `DUMMY-OK-` three events ending delivered · `DUMMY-TRANSIT-` two events, in transit · `DUMMY-ODD-` includes an unmapped raw status · `DUMMY-NOTFOUND-` outcome NOT_FOUND · `DUMMY-FAIL-` retryable failure every attempt · `DUMMY-FATAL-` non-retryable failure · `DUMMY-FLAKY-` fails attempt 1, then succeeds · `DUMMY-SLOW-` awaits far beyond `timeout_ms` · `DUMMY-DELAY-<ms>-` awaits the given time, then succeeds · `DUMMY-HANG-` publishes `job.started` then stops responding without exiting (worker-level hang, no result) · `DUMMY-CRASHONCE-` publishes `job.started` then exits the process on first delivery, succeeds on redelivery · `DUMMY-CRASH-` exits on every delivery · `DUMMY-DUP-` publishes the identical result message twice.

---

## 8. Build order

1. **Phase 1 — Foundation:** git init, layout, `docs/architecture.md`, Compose (Postgres + RabbitMQ), schemas, fixtures, topology, contract tests, CI skeleton.
2. **Phase 2 — Engine foundation:** skeleton, migrations, API-key auth + admin CLI, endpoints, transactional dispatch.
3. **Phase 3 — Messaging:** topology declaration, outbox relay, event consumer + inbox, `close_attempt`, result handler, normalization, retry scheduler, dead-letter consumer, reaper.
4. **Phase 4 — SDK + dummy adapter** (may overlap 2–3): contracts, worker runtime with the concurrency model, dummy adapter.
5. **Phase 5 — End-to-end:** Engine and worker containers, e2e suite, docs.

Work stops for review after each phase. Each phase ends with a change report: files created or modified, the important functions and methods added, why each is needed, how it works, and the test results.

---

## 9. Testing procedure

```
make setup            # uv sync per Python project (uv-managed 3.13), cargo fetch
make up-deps          # postgres + rabbitmq, wait for healthy
make migrate          # sqlx migrate run
make test-contracts   # fixtures vs schemas
make test-engine      # cargo fmt --check, clippy -D warnings, cargo test
make test-adapters    # ruff, mypy, pytest
make up               # full stack: + engine + adapter-dummy
make e2e              # pytest in tests-e2e against the running stack
make down
```

- Engine integration tests: `sqlx::test` (isolated database per test) and uniquely prefixed queues on the Compose RabbitMQ.
- Concurrency tests (Engine): N tasks released together by a barrier against the real database; assertions on row counts and response codes. Repeated at e2e level with `asyncio.gather` over HTTP.
- Worker concurrency tests (SDK): jobs blocked on an `asyncio.Event` give deterministic control without sleeps.
- Contract tests: same fixture files in Rust and Python; Rust-serialized messages validated against the schemas.
- Crash/restart scenarios: `docker compose kill/stop/start` from the e2e harness.

---

## 10. Exact acceptance criteria

Milestone 1 is accepted when all of the following pass in an automated run from a clean checkout.

**Foundation**
1. `make up-deps` brings Postgres and RabbitMQ to healthy; `make migrate` applies all migrations to an empty database and is a no-op when run again.
2. Python tooling runs on a uv-managed 3.13 interpreter in project `.venv`s; host `python3 --version` is unchanged; nothing is installed outside the project and the uv/cargo user directories.
3. All valid fixtures pass and all invalid fixtures fail in both Rust and Python.
4. The repository contains no AI-service or human-interface directory and no Redis, AI, provider-credential, intervention or real-courier code, service or dependency.

**Authentication and access control**
5. `admin create-user` / `create-api-key` print a key once; the database holds only prefix and hash; the key appears in no log line.
6. Missing, malformed, unknown and revoked keys each return 401; a valid key returns 200.
7. User B using user A's request ID on GET, events, jobs, refresh and DELETE gets 404 each time; B's list never contains A's requests.
8. Users A and B subscribed to the same shipment see identical events; after A deletes their request, A gets 404 and B is unaffected.
9. Malformed body, empty or over-long tracking number → 422; unknown provider → 404.

**Data model and concurrent requests**
10. A POST commits shipment, subscription, job, `job_attempts` row and outbox row together; an injected failure after the job insert leaves none of them.
11. Twenty simultaneous identical POSTs from **one user**: exactly one active shipment, one active subscription, one job, one outbox row; exactly one 201, nineteen 200, all returning the same request ID; no 5xx.
12. Simultaneous POSTs from **ten different users** for the same provider + number (two each): one shipment, ten subscriptions, one job, one outbox row; no 5xx.
13. Twenty simultaneous `refresh` calls on a shipment with no active job: exactly one new job.
14. With a shipment row marked archived, a new request for the same number creates a new shipment row; the archived row's events are not returned.

**Messaging: at-least-once and idempotency**
15. Every committed outbox row is published at least once; rows stay pending while RabbitMQ is stopped and are published after it returns.
16. Killing the relay after broker confirm and before mark-sent produces a second publish with the same `message_id`; the final state equals that of a single publish.
17. The identical `job.result` delivered twice (`DUMMY-DUP-`): one transition, one `job_attempts` row, one execution-log row, one raw response, no duplicate events.
18. The same `job.track` placed in the queue twice (two physical executions of one attempt): one `job_attempts` row with `execution_count = 2`; execution logs show one `accepted` and one `duplicate`; one raw response; no duplicate events.
19. A result for an attempt the reaper already closed (a newer attempt is running) is logged as `stale`; the job's status, current attempt, `accepted_execution_id` of every attempt, raw responses and events are unchanged.
20. `job.result` processed **before** `job.started` for the same `execution_id`: the result handler creates the execution-log row with `started_at`, `finished_at`, outcome and disposition `accepted`; the attempt's `accepted_execution_id` is that execution; the job is COMPLETED. When the late `job.started` is then processed, there is still exactly one execution-log row; its `finished_at`, outcome and disposition are unchanged; `execution_count` is still 1; the job is still COMPLETED (not moved back to PROCESSING).
20a. `job.result` processed with no `job.started` ever arriving gives the same final rows as the normal order.
20b. Two executions of one attempt with interleaved order — result(A), started(B), started(A), result(B): A is `accepted`, B is `duplicate`, `accepted_execution_id` = A, `execution_count` = 2, one raw response.
21. A schema-invalid event and an unknown-major-version event each land in `engine.events.dlq` and do not block later messages.

**Worker concurrency**
22. SDK test, concurrency 2 with five blocked jobs: the observed maximum of simultaneously running jobs is exactly 2.
23. SDK test, one job blocked forever with a 1 s timeout plus two instant jobs: both instant results are published before the blocked job's timeout result; the blocked job yields FAILURE / `ADAPTER_TIMEOUT`; its registered cleanup callback has run; afterwards no job task remains and all semaphore slots are free.
24. Cleanup callbacks run on success, adapter exception, timeout and shutdown cancellation.
25. End-to-end, worker concurrency 4: three `DUMMY-SLOW-` jobs and twenty `DUMMY-OK-` jobs submitted together — all twenty OK jobs reach COMPLETED while all three slow jobs are still PROCESSING on attempt 1. The slow jobs later reach FAILED with `ADAPTER_TIMEOUT` after `max_attempts`.
26. SIGTERM: the worker stops consuming, finishes its in-flight job and publishes the result before exiting.

**Failure, dead-letter and reaper**
27. `DUMMY-FLAKY-1`: QUEUED → PROCESSING → RETRYING → QUEUED → COMPLETED; two `job_attempts` rows.
28. `DUMMY-FAIL-1`: FAILED after exactly `max_attempts`, each retry delay longer than the previous.
29. `DUMMY-FATAL-1`: FAILED after one attempt.
30. `DUMMY-CRASHONCE-1`: worker exits and restarts; job COMPLETES on attempt 1; that attempt has `execution_count = 2` with dispositions `incomplete` and `accepted`.
31. `DUMMY-CRASH-1`: the message is dead-lettered; a `dead_letters` row identifies the job and attempt; the job goes to RETRYING once, is dead-lettered again and ends FAILED with `DELIVERY_LIMIT_EXCEEDED` and `dead_letter_count = 2`; `adapter.dummy.jobs.dlq` is empty afterwards.
32. A healthy job running in the same worker during the `DUMMY-CRASH-` cycle still reaches COMPLETED.
33. Queue-wait deadline: with the worker stopped for longer than `queue_wait_max_ms`, a QUEUED job is closed as `QUEUE_WAIT_EXCEEDED` (never `ADAPTER_TIMEOUT` or `WORKER_LOST`), retried, and finally FAILED; when the worker is started afterwards, the expired first-delivery messages are dropped without execution (no execution-log rows for them); no job remains QUEUED, PROCESSING or RETRYING.
33a. Queue delay, worker down: worker stopped; five `DUMMY-OK-` jobs submitted; wait longer than `timeout_ms + result_grace_ms` but less than `queue_wait_max_ms`; worker started → all five COMPLETED on attempt 1, one execution each, no error class recorded, no retry.
33b. Queue delay, busy queue: worker concurrency 1; six `DUMMY-DELAY-` jobs each taking about 60 % of `timeout_ms`, so the last waits several multiples of `timeout_ms` in the queue → all six COMPLETED on attempt 1 with no `ADAPTER_TIMEOUT`, `WORKER_LOST` or `QUEUE_WAIT_EXCEEDED`.
33c. Execution recovery deadline: `DUMMY-HANG-` (started, no result, process alive) → `execution_deadline_at` is NULL while QUEUED and set when `job.started` is processed; after it passes the attempt is closed as `WORKER_LOST`, not `QUEUE_WAIT_EXCEEDED`.
33d. Adapter timeout is measured from execution start: a `DUMMY-SLOW-` job that first waited in the queue for longer than `timeout_ms` still runs for the full `timeout_ms` before reporting `ADAPTER_TIMEOUT`.
34. Dead-letter and reaper both firing for the same attempt (either order) produce exactly one retry: one new `job_attempts` row and one new outbox row.
34a. Stale dead-letter: the reaper closes attempt N, attempt N+1 is opened and running, then the dead-letter for attempt N is processed → `dead_letter_count` is unchanged, the job's status and attempt N+1 are unchanged, attempt N+1 goes on to COMPLETED, and the `dead_letters` row has `applied = false`.
34b. Current dead-letter: a dead-letter for the open current attempt increments `dead_letter_count` by exactly one and closes the attempt in the same statement; delivering that dead-letter message a second time changes nothing.
34c. Concurrent dead-letter handler and reaper on the same open attempt (released together): exactly one wins; `dead_letter_count` is 1 if the dead-letter handler won and 0 if the reaper won — never incremented without also closing the attempt.
35. Two reaper instances and two retry-scheduler instances running concurrently never create two attempts with the same number or two outbox rows for one attempt.
36. Engine stopped while a worker finishes a job: after restart the result is consumed and the job COMPLETES.

**Happy path and normalization**
37. `DUMMY-OK-1`: job COMPLETED within 10 s; raw response stored; three events each with raw and normalized status; shipment DELIVERED.
38. `DUMMY-TRANSIT-1`: COMPLETED; shipment IN_TRANSIT.
39. `DUMMY-NOTFOUND-1`: COMPLETED with outcome NOT_FOUND; shipment UNKNOWN; no retry.
40. `DUMMY-ODD-1`: the unmapped raw status is stored with normalized status UNKNOWN and the raw value preserved.

**Cross-cutting**
41. One correlation ID links the API response header, Engine logs, the three messages and worker logs for a request; logs are structured JSON.
42. `cargo fmt --check`, `cargo clippy -D warnings`, `ruff`, `mypy` and every test suite pass locally and in CI.

---

On approval, implementation starts with **Phase 1 only** and stops for review at the end of each phase.

---

## Implementation notes (added as phases complete)

### Phase 3

- **RabbitMQ 4 does not count an explicit requeue toward `x-delivery-limit`.** Observed on RabbitMQ 4.4.1: a consumer that answers `basic.nack(requeue=true)` receives the same message indefinitely. Only a delivery that ends without any acknowledgement (the worker process or its channel dies) is counted, and the fourth such failure dead-letters the message. Consequence for the Phase 4 SDK: a worker must not use nack-with-requeue as its retry mechanism (section 1.5 says a result that cannot be published is "nacked for redelivery"); it should close its channel or exit instead, so the delivery limit still bounds the loop. The Engine's own event consumer requeues only on transient database errors and is bounded by the reaper, not by the limit.
- **Incoming messages are validated by hand-written Rust, not a JSON Schema engine.** `messaging::contracts::parse_incoming_event` mirrors the schemas; `tests/contracts.rs` runs every shared valid and invalid fixture through it.
- **Lock order.** Every handler locks the job row first; the result handler locks shipment then job, the same order as API dispatch.
- **A message for an unknown job or attempt is rejected** to `engine.events.dlq`, like a malformed one.
- **Broker tests use a unique name prefix** (`test-xxxxxxxxxxxx.`) and delete their queues afterwards. A test that panics leaves its queues behind; they can be deleted from the management UI.

### Phase 4

- **The worker never uses nack-with-requeue** (see the Phase 3 note). When a `job.result` cannot be published, the job message is left unacknowledged, the worker ends its broker session and reconnects; RabbitMQ returns the message and counts the delivery. With results permanently unroutable, a job is executed `x-delivery-limit + 1` times and then dead-lettered (covered by an integration test). This replaces "nacked for redelivery" in section 1.5.
- **`job.started` is advisory.** If it cannot be published the job still runs; `job.result` carries the full execution identity.
- **Fault injection is opt-in.** `SimulateCrash`, `SimulateHang` and `SimulateDuplicateResult` are honoured only with `WORKER_ALLOW_FAULT_INJECTION=true`; otherwise the job fails with `INTERNAL`. A production worker cannot be made to exit by a tracking number.
- **Unexpected adapter exceptions** are reported as `INTERNAL` (retryable) with only the exception type in the message. The exception text, which may contain provider content, goes to the worker log only.
- **A result larger than 256 KiB** is replaced by a non-retryable `INTERNAL` failure instead of being sent.
- **UUIDv7 is generated by the SDK** (`contracts.envelope.new_id`), because `uuid.uuid7` only exists from Python 3.14.
- **Worker tests on RabbitMQ use the Engine account** with a prefixed private topology: the worker account can read only queues named `adapter.<key>.(jobs|resume|control)` and cannot create anything. A separate test checks those restrictions against the real topology (skipped if the Engine has never declared it on the broker).
- **Queue message counts lag on quorum queues**, so tests look into a queue instead of reading its count.
- Additional settings: `WORKER_EXPIRY_SKEW_S` (5), `WORKER_ID`, `WORKER_ALLOW_FAULT_INJECTION`, `WORKER_TOPOLOGY_PREFIX`. The worker `Dockerfile` is added in Phase 5 with the Compose service.
