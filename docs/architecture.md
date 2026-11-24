# Courier Tracking System — Architecture

The approved system-wide architecture. Milestone 1 (Engine ↔ dummy adapter) is specified in detail in [milestone-1.md](milestone-1.md); where the two overlap, `milestone-1.md` is authoritative for Milestone 1.

## 1. Purpose

Track shipments across many courier providers that expose different mechanisms (official API, scraping with credentials, scraping without credentials) through one modular system.

- **Engine (Rust):** owns the database and all business logic.
- **Adapters (Python):** one per courier; talk to the provider and nothing else.
- **RabbitMQ:** the only channel between them, plus a narrow Engine internal HTTP API for secrets.
- **AI service (Python, OpenRouter):** diagnosis and adapter repair, advisory only.
- **Human interface (FastAPI):** minimal OTP/CAPTCHA entry.

## 2. Gaps found in the original specification and how they are resolved

| # | Gap | Resolution |
|---|---|---|
| 1 | How adapters obtain credentials (no secrets in messages, no DB access) | Messages carry an opaque `credential_ref`; the worker resolves it through an Engine internal API (separate port, per-worker token, bound to an in-flight job, audited), behind a `SecretStore` trait so Vault can replace it |
| 2 | Session resumption needs worker affinity | Two session classes, *portable* and *pinned* (section 9) |
| 3 | No session store | Redis, adapter-side only, introduced in Phase 8 |
| 4 | Tracking is not one-shot | Engine scheduler re-polls a shipment (`next_poll_at`) until a terminal status |
| 5 | No shipment entity | `shipments` own jobs and events; `tracking_requests` are per-user subscriptions; uniqueness is scoped to active shipments within an access scope (section 6.1) |
| 6 | No notification path for "OTP needed" | Interface lists pending interventions; optional per-user webhook; email/SMS later |
| 7 | API authentication unspecified | Hashed API keys; signed single-use tokens for intervention links; JWT/OIDC later |
| 8 | Stuck jobs | Ack only after the result is published; `job.started`; Engine reaper with separate queue-wait and execution-recovery deadlines |
| 9 | Duplicate, late, out-of-order results | Every message carries `job_id` + `attempt`; guarded state transitions; inbox table |
| 10 | Retry ownership ambiguous | The Engine owns business retries (delay stored in the database). Broker redelivery is only for crashes; DLQs only for poison messages |
| 11 | Large payloads | 256 KiB message limit; larger bodies go to an Engine artifact endpoint and travel as `artifact_ref` (Phase 6+) |
| 12 | AI receives raw pages | All external content is untrusted; redaction, no tools, schema-validated output (section 11) |
| 13 | Rust/Python contract drift | JSON Schemas are the source of truth; shared fixtures tested in both languages |
| 14 | Reliability scheduled last | Idempotency, outbox, DLQs, correlation IDs are built in Milestone 1 |
| 15 | Provider identification | `provider_code` is required in a request; auto-detection is optional later |
| 16 | Scraping legality and CAPTCHA | Per-provider rate limits and a `scraping_authorized` flag; CAPTCHAs are solved only by a human |

**Mandatory:** Engine, adapter SDK and workers, contracts, PostgreSQL, RabbitMQ, outbox/inbox, retry and reaper, DLQs, credential encryption and resolve API, shipment scheduler, status normalization, intervention workflow and minimal UI, Redis (from Phase 8), AI diagnosis with deterministic pre-classification, sandboxed repair ending in a pull request with human merge, structured logs and correlation IDs, Compose, tests, CI.

**Optional / later:** Vault or cloud KMS, object storage for artifacts, carrier auto-detection, email/SMS notifications, customer webhooks, JWT/OIDC, Prometheus and Grafana, OpenTelemetry tracing, auto-approval of AI status mappings, a full dashboard, Kubernetes.

## 3. Delivery guarantees

Exactly-once delivery is not assumed anywhere. The system is **at-least-once delivery with idempotent consumers**, and message order is not assumed.

| Hop | What can be duplicated | How it is made harmless |
|---|---|---|
| Outbox → RabbitMQ | Relay crashes after the broker confirm, before marking the row sent → the same message (same `message_id`) is published again | Consumers deduplicate on `message_id` |
| RabbitMQ → worker | Redelivery → one logical attempt is physically executed more than once | Lookups are read-only; every execution reports the same `(job_id, attempt)` with its own `execution_id` |
| Worker → Engine | The same result twice; two different results for one attempt; a late result for an old attempt | Inbox `(consumer, message_id)`; one guarded close per attempt (first wins); events unique on `(shipment_id, dedupe_key)` |
| Engine consumer | Crash after commit, before ack | The inbox row is written in the same transaction as the effects; ack only after commit |

## 4. Components

```
 Client ──HTTP──▶ ┌──────────────── Engine (Rust) ────────────────┐
                  │ public API :8080      internal API :8081      │
 Human UI ─HTTP─▶ │ dispatcher · outbox relay · event consumer    │◀─HTTP─ workers
 (FastAPI)        │ dead-letter consumer · retry scheduler        │  (credential / artifact /
                  │ reaper · normalizer · breaker                 │   intervention-response resolve)
                  └──────┬───────────────┬──────────────┬─────────┘
                         │ SQLx          │ lapin        │ reqwest
                    PostgreSQL       RabbitMQ       AI service (FastAPI) ──▶ OpenRouter
                                  ▲          │
                        events    │          ▼  jobs / resume
                         ┌────────┴──────────────────┐
                         │ Adapter workers (Python)  │──▶ courier APIs / sites
                         │ SDK + one adapter each    │──▶ Redis (sessions, Phase 8)
                         └───────────────────────────┘
 Repair orchestrator (Python) ──HTTP pull──▶ Engine;  ──▶ OpenRouter;  ──▶ GitHub PR
        └─ runs generated code only inside a sandbox container: no network, no secrets
```

Rules:
- Only the Engine reads or writes PostgreSQL.
- Workers reach the Engine only through RabbitMQ and the narrow internal API.
- The AI service is stateless, holds no database or broker credentials, and receives only redacted input.
- The human interface is a thin client of the Engine public API.

### 4.1 Engine modules

Single crate, single binary, several Tokio tasks; a `--role` flag allows splitting API and background work later.

| Module | Responsibility |
|---|---|
| `config`, `telemetry`, `error` | Environment config, JSON logs with correlation IDs, one error → HTTP mapping |
| `api` | Axum routes, API-key authentication, validation, timeouts; later the internal worker API and rate limiting |
| `domain` | Status enums, state machines, error taxonomy, backoff — pure and unit-tested |
| `db` | SQLx repositories, embedded migrations |
| `messaging::topology` | Declares exchanges and queues from `courier-contracts/topology.yaml` |
| `messaging::outbox_relay` | Publishes pending outbox rows with broker confirms |
| `messaging::event_consumer` | Consumes `engine.events`, validates, deduplicates, dispatches to handlers |
| `messaging::dead_letter_consumer` | Consumes job DLQs and closes the affected attempt |
| `services::dispatch` | Creates shipment, subscription, job, attempt and outbox row atomically |
| `services::close_attempt` | The single guarded path that ends an attempt |
| `services::results`, `normalize` | Applies results; maps raw statuses to normalized ones |
| `services::retry_scheduler`, `reaper` | Opens new attempts; closes attempts that passed a deadline |
| later: `scheduler` (polling), `interventions`, `credentials`, `breaker`, `ai` | Phases 6–9 |

### 4.2 Adapter SDK

```python
class Adapter(ABC):
    key: ClassVar[str]; version: ClassVar[str]; method: ClassVar[Literal["api", "scrape", "browser"]]
    async def track(self, ctx: JobContext, req: TrackRequest) -> TrackResult | InterventionRequired
    async def resume(self, ctx, session, response) -> TrackResult | InterventionRequired   # Phase 8
    async def health_check(self) -> HealthStatus
```

- Adapters return the raw response and events with **raw** statuses. Normalization is the Engine's job.
- Fetching and parsing are separate functions, so parsers are testable (and repairable) against saved fixtures without network access.
- Failures are raised as typed errors; the SDK maps them to the error taxonomy, so classification is deterministic.
- One worker process serves one adapter and runs several jobs concurrently (semaphore-bounded asyncio tasks, per-job timeout, guaranteed cleanup).
- Each Python component has its own `pyproject.toml`, `.python-version` (3.13), `uv.lock` and `.venv`. The system Python is never used or modified.

**Error taxonomy.** Reported by workers: `NETWORK_TIMEOUT, NETWORK_CONNECT, HTTP_5XX, RATE_LIMITED, BLOCKED, AUTH_INVALID, AUTH_EXPIRED, PARSE_ERROR, SCHEMA_DRIFT, ADAPTER_TIMEOUT, INTERNAL, UNKNOWN`. Assigned only by the Engine: `DELIVERY_LIMIT_EXCEEDED, QUEUE_WAIT_EXCEEDED, WORKER_LOST`. Later phases add `SESSION_LOST`.

## 5. Status models

Shipment status and execution status are separate.

- **Shipment:** `CREATED, PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, RETURNED, EXCEPTION, UNKNOWN`
- **Execution:** `PENDING, QUEUED, PROCESSING, WAITING_FOR_OTP, WAITING_FOR_CAPTCHA, RETRYING, COMPLETED, FAILED, CANCELLED, EXPIRED`

```
QUEUED → PROCESSING → COMPLETED
            ├→ WAITING_FOR_OTP / WAITING_FOR_CAPTCHA → (resume) PROCESSING | EXPIRED
            ├→ RETRYING → QUEUED      (attempt + 1, exponential backoff with jitter)
            └→ FAILED
any non-terminal → CANCELLED
```

Raw provider statuses are always stored next to the normalized status. An unmapped raw status is stored as `UNKNOWN` with the raw value preserved.

## 6. Database

PostgreSQL, owned by the Engine. IDs are UUIDv7.

| Table | Purpose |
|---|---|
| `users`, `api_keys` | Callers; only key hashes are stored |
| `providers` | Courier definitions, timeouts, attempt limits, poll policy, `scraping_authorized` |
| `provider_credentials` (Phase 6) | Envelope-encrypted secrets or references to an external secrets manager |
| `adapters`, `adapter_versions` | Which adapter serves a provider; versions seen from workers |
| `shipments` | One generation of a tracking number; current normalized status |
| `tracking_requests` | A user's subscription to a shipment |
| `tracking_jobs` | One lookup of a shipment; execution status, attempt counter, deadlines |
| `job_attempts` | One row per **logical** attempt, `UNIQUE(job_id, attempt)`; authoritative outcome |
| `adapter_execution_logs` | One row per **physical** execution, keyed by `execution_id` |
| `raw_tracking_responses` | Original provider responses (JSONB) |
| `tracking_events` | Append-only event history, raw and normalized, deduplicated |
| `provider_status_mappings` | Raw status → normalized status |
| `human_interventions` (Phase 8) | OTP/CAPTCHA challenges |
| `ai_diagnoses`, `ai_repairs` (Phases 9–10) | AI history |
| `artifacts` (Phase 6+) | Large bodies and images |
| `provider_health` (Phase 11) | Circuit-breaker state |
| `outbox_messages`, `processed_messages`, `dead_letters` | Reliable messaging |
| `audit_log` (Phase 6) | Credential resolves, mapping approvals |

### 6.1 Shipment identity, reuse, retention and access control

A plain `UNIQUE(provider_id, tracking_number)` is wrong for two reasons: couriers recycle tracking numbers, and some tracking data is visible only with a particular account or verifier.

- **Generations.** A shipment row is one generation of a number. Uniqueness is a partial index on `(provider_id, tracking_number, access_scope) WHERE archived_at IS NULL`. A shipment is archived after it has been terminal longer than the provider's reuse window (default 90 days), or has had no subscriber and no event for that long. A later request creates a new row.
- **Access scope.** `public` (anyone with the number may see it; all users share one row), `cred:<credential_id>` (shared only among requests using that credential), `verifier:<hash>` (shared only among requests that supplied the same verifier). Sharing never reveals more than the user could obtain from the provider with what they supplied.
- **Ownership.** Shipments have no owner. Users own `tracking_requests`. Polling continues while at least one active subscription exists.
- **Access control.** The public API is request-centric; there is no user-facing shipment endpoint. Every query is filtered by the caller's `user_id`. Another user's request ID returns 404, not 403. A subscriber sees status, events and job summaries — never other subscribers, raw provider responses or credentials.
- **Interventions.** A challenge is assigned to one user and only that user can see or answer it.
- **Retention.** Archived shipments, events and raw responses are purged after a configurable period; subscriptions are kept as stubs for audit.

## 7. Messaging

Topology lives in `courier-contracts/topology.yaml`. The Engine declares it; workers look their queue up passively. All durable queues are quorum queues with a delivery limit and a dead-letter route.

| Exchange (topic) | Routing key | Queue | Consumer |
|---|---|---|---|
| `courier.jobs` | `job.track.v1.<adapter_key>` | `adapter.<adapter_key>.jobs` | that adapter's workers |
| `courier.jobs` | `job.resume.v1.<adapter_key>.any` (Phase 8) | `adapter.<adapter_key>.resume` | any worker (portable session) |
| `courier.jobs` | `job.resume.v1.<adapter_key>.<worker_id>` (Phase 8) | `worker.<worker_id>.resume` (exclusive) | the pinned worker |
| `courier.events` | `job.started.v1`, `job.result.v1`, later `job.intervention_required.v1` | `engine.events` | Engine |
| `courier.dlx` | source queue name | `<queue>.dlq` | Engine for job DLQs; `engine.events.dlq` is parked |

Every message uses one envelope: `message_id`, `type`, `schema_version`, `occurred_at`, `correlation_id`, `causation_id`, `producer`, `payload`. The major version is in the routing key and in `schema_version`; an unknown major version is rejected to the DLQ; minor versions only add fields, and unknown fields are ignored on both sides. Secrets, OTP values and cookies never appear in a message — only opaque references.

## 8. Core workflows

**Tracking.** Request → shipment/subscription/job/outbox row in one transaction → relay publishes → worker publishes `job.started`, runs the adapter under a timeout, publishes `job.result`, acks → Engine deduplicates, closes the attempt, stores the raw response and events, normalizes, updates the shipment.

**Failure.** Retryable error → `RETRYING` with exponential backoff → the retry scheduler opens the next attempt. Non-retryable or attempts exhausted → `FAILED`. A message that exceeds the delivery limit is dead-lettered and handled by the dead-letter consumer; the reaper is the backstop. Details and the three separate timers (queue wait, adapter timeout, execution recovery) are in `milestone-1.md` sections 1.2–1.7.

**OTP / CAPTCHA (Phase 8).**
1. The adapter detects the challenge, saves the session, and returns `InterventionRequired`. The job message is acked; nothing waits.
2. Engine: job → `WAITING_FOR_*`; a `human_interventions` row with expiry and a hashed single-use token.
3. The interface shows the prompt or CAPTCHA image to the assigned user.
4. On submit the Engine validates token, expiry, attempt count and format, stores the response encrypted, and publishes `job.resume`.
5. The worker resolves `response_ref` (valid only for that intervention and attempt, repeatable until consumed so a redelivered resume still works), calls `adapter.resume`, and publishes the result. A wrong OTP produces a new challenge up to `max_attempts`.
6. Overdue challenges expire → job `EXPIRED`, session deleted.

**AI diagnosis (Phase 9).** Deterministic classification always runs first; network, rate-limit, auth and not-found classes never reach the AI. Eligible classes (`PARSE_ERROR`, `SCHEMA_DRIFT`, `UNKNOWN`, unmapped status) are sent only when a fingerprint recurs and has no open diagnosis. The AI returns a schema-validated JSON verdict. Status mappings are proposals that need human approval.

**AI repair (Phase 10).** The orchestrator pulls a repair request from the Engine over HTTP, adds the redacted failing fixture as a test, asks the model for a patch, checks it against the allow-list and static rules, runs the tests in the sandbox, and — only if they pass — pushes a branch and opens a pull request. Merge and deployment stay manual.

## 9. Sessions

| Portable — encrypted in Redis, any worker can resume | Pinned — exists only inside one worker process |
|---|---|
| Cookies and session IDs | A live Playwright page and its DOM |
| Bearer / API session tokens | In-memory JavaScript state of that page |
| Playwright `storage_state` | A CAPTCHA instance bound to the rendered page |
| OTP transaction ID when verification is a plain HTTP call | A multi-step form position that has no URL |
| Re-fetchable CSRF tokens, last URL, form values | Open WebSocket or streaming connections |

Adapters use a portable session whenever the provider allows it and declare `affinity: pinned` only when the challenge cannot survive a page reload.

| When the worker dies | Portable | Pinned |
|---|---|---|
| Before `intervention_required` is published | Message redelivered; `track` runs again; the provider may send a second OTP | Same |
| While waiting for the user | Nothing lost | Page gone. Detected by missed heartbeats or an unroutable resume; intervention marked `session_lost`; job re-queued; the user gets a fresh challenge |
| After consuming `job.resume`, before the result | Resume redelivered to another worker, which reloads the session | The private resume queue dies with the worker; the recovery deadline fires; restart as above |
| Redis lost | `SESSION_LOST` → restart | Not affected |

Each restart consumes a job attempt, and `max_challenges` caps how many OTPs a user can be sent. No path leaves a job waiting forever.

## 10. Security

- **Credentials:** envelope encryption (AES-256-GCM, per-record data key wrapped by a master key from the environment, `key_version` for rotation); decrypted only inside the resolve handler; never logged.
- **Resolve API:** internal port only, per-worker token, checks the job is `PROCESSING` for that adapter and attempt, audited.
- **API keys:** `ctk_<prefix>_<secret>`; only the SHA-256 hash and prefix are stored; constant-time comparison.
- **Intervention tokens:** 256-bit random, hashed, single use, expiring; submission is rate-limited.
- **Broker accounts:** the worker account can only consume adapter queues and publish to `courier.events`.
- **Logs:** a redaction layer in both Rust and Python removes tokens, cookies, OTPs and phone numbers.
- **Input validation** at every boundary: Axum extractors on HTTP, schema validation on every consumed message.

## 11. AI security

**Threat model.** Everything that originates outside the system — scraped HTML, provider API bodies, error strings, tracking descriptions, HTTP headers — is untrusted and may contain text written to manipulate a model. The design assumes injection sometimes succeeds and makes a successful injection unable to do damage.

Diagnosis and status interpretation:
- The model has **no tools** and cannot act. Its only output is JSON validated against a strict schema. A hijacked model can at worst return a wrong diagnosis.
- Untrusted content is passed in a delimited data section and never concatenated into instructions.
- Before sending: scripts, styles, comments and hidden elements are stripped; long text is truncated; cookies, tokens, emails, phone numbers and addresses are redacted; total size is capped.
- Output is advisory. Nothing a model returns is executed or used as a routing key, SQL, URL or shell input.
- The AI service's only secret is the OpenRouter key. A daily spend cap and per-fingerprint deduplication limit abuse.

Repair:
- **Two trust zones.** The orchestrator (our code) talks to OpenRouter and GitHub. Model-generated code runs only in a sandbox container: no network, non-root, read-only root filesystem, dropped capabilities, resource limits, empty environment, no Docker socket. It is on no Compose network, so PostgreSQL, RabbitMQ, Redis and the Engine are unreachable and no production secret exists inside it.
- **Path allow-list enforced by the orchestrator:** only `adapters/<provider>/adapter.py`, `parser.py`, and new files under that adapter's `fixtures/` and `tests/`. Anything else rejects the whole patch.
- **Existing tests are immutable.** A patch may add tests, never modify or delete them.
- **Static checks before execution:** diff size cap, no new dependencies, AST scan rejecting `subprocess`, `socket`, `os.environ`, `eval`/`exec`, dynamic imports, and file access outside the adapter directory.
- **Publishing:** only after tests pass; token limited to branch push and PR creation; `main` is protected with required human review.

## 12. Infrastructure and testing

Docker Compose for local development; no Kubernetes. Services by phase: `postgres`, `rabbitmq` (Phase 1); `engine`, `adapter-dummy` (Phase 5); one service per real adapter (Phase 6); `redis`, `human-interface` (Phase 8); `ai-service` (Phase 9); `repair-orchestrator` and an on-demand network-less sandbox (Phase 10).

| Component | Tests |
|---|---|
| Contracts | Fixtures against schemas; the same fixtures in Rust and Python |
| Engine | Unit tests for state machines, backoff, normalization; integration tests against real PostgreSQL and RabbitMQ; API tests; concurrency tests |
| Adapter SDK | Error mapping, concurrency, timeout and cleanup; worker integration against RabbitMQ |
| Adapters | Parser tests against saved fixtures; mocked HTTP; opt-in live smoke tests |
| Human interface | Route tests with a mocked Engine; one browser flow |
| AI service | Mocked OpenRouter; redaction; injection cases |
| End-to-end | Compose stack driven by pytest, including crash and restart scenarios |

## 13. Roadmap

| Phase | Deliverable |
|---|---|
| 1–5 | **Milestone 1** — see `milestone-1.md` |
| 6 | Credential storage and resolve API; one official-API adapter; one authorized scraping adapter; provider rate limiting |
| 7 | Mapping management, event history, polling scheduler, archiving and retention |
| 8 | Redis, sessions, intervention workflow, minimal human interface |
| 9 | AI service: diagnosis, status interpretation, page-change analysis |
| 10 | Sandboxed adapter repair ending in a pull request |
| 11 | Metrics, circuit-breaker tuning, DLQ tooling, chaos tests |
| 12 | Production images, deployment documentation, independent deployment of Engine and workers |

Work proceeds one phase at a time, with a review after each.
