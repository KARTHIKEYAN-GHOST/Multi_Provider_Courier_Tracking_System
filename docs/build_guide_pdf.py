import sys
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table,
                                TableStyle, KeepTogether)

OUT = sys.argv[1]
INK = colors.HexColor("#1f2933"); ACCENT = colors.HexColor("#0b5c8a"); SOFT = colors.HexColor("#eef3f7")
LINE = colors.HexColor("#c5d0da")
ss = getSampleStyleSheet()
body = ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica", fontSize=9.6, leading=13.6, textColor=INK, spaceAfter=5)
h1 = ParagraphStyle("h1", parent=body, fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=ACCENT, spaceBefore=4, spaceAfter=8)
h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=INK, spaceBefore=9, spaceAfter=4)
h3 = ParagraphStyle("h3", parent=body, fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=ACCENT, spaceBefore=6, spaceAfter=2)
bul = ParagraphStyle("bul", parent=body, leftIndent=12, bulletIndent=2, spaceAfter=2)
cell = ParagraphStyle("cell", parent=body, fontSize=8.6, leading=11.4, spaceAfter=0)
cellb = ParagraphStyle("cellb", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
code = ParagraphStyle("code", parent=body, fontName="Courier", fontSize=8.2, leading=10.6, backColor=SOFT, borderPadding=5, leftIndent=4, spaceBefore=3, spaceAfter=8)
title = ParagraphStyle("title", parent=h1, fontSize=26, leading=31, alignment=TA_CENTER)
sub = ParagraphStyle("sub", parent=body, fontSize=12, leading=16, alignment=TA_CENTER, textColor=colors.HexColor("#52606d"))

S = []
def H1(t): S.append(PageBreak()); S.append(Paragraph(t, h1))
def H2(t): S.append(Paragraph(t, h2))
def H3(t): S.append(Paragraph(t, h3))
def P(t): S.append(Paragraph(t, body))
def B(*items):
    for i in items: S.append(Paragraph(i, bul, bulletText="•"))
    S.append(Spacer(1, 3))
def C(t): S.append(Preformatted(t.strip("\n"), code))
def T(head, rows, widths):
    data = [[Paragraph(h, cellb) for h in head]] + [[Paragraph(str(c), cell) for c in r] for r in rows]
    t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), ACCENT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    S.append(t); S.append(Spacer(1, 8))
def M(name, need, how):  # one method
    S.append(KeepTogether([Paragraph(f"<font face='Courier-Bold' size='9'>{name}</font>", h3),
        Paragraph(f"<b>Why it is needed:</b> {need}", bul), Paragraph(f"<b>How it works:</b> {how}", bul), Spacer(1, 3)]))

# ---------------------------------------------------------------- cover
S += [Spacer(1, 60 * mm), Paragraph("Courier Tracking System", title), Spacer(1, 6),
      Paragraph("Project guide: what has been built, how it works, and why", sub), Spacer(1, 14),
      Paragraph("Covers Phases 1 to 8: Rust Engine, PostgreSQL, RabbitMQ, Redis, Python Adapter SDK,<br/>dummy, DHL and demo-site adapters, credentials, polling, retention,<br/>the OTP / CAPTCHA workflow, the human interface, tests and containers", sub),
      Spacer(1, 30 * mm), Paragraph("State of the repository after Phase 8, 25 November 2026", sub)]

# ---------------------------------------------------------------- 1
H1("1. What this project is")
P("The Courier Tracking System tracks parcels across many courier companies through one API. A user asks "
  "\"where is tracking number X of courier Y?\"; the system asks the courier, stores the answer, translates the "
  "courier's own wording into one common set of statuses, and keeps the history.")
P("Couriers differ a lot: some have an official API, some only a website, some need a login or an OTP. The system "
  "is therefore split into two halves that never share code or a database:")
B("<b>The Engine (Rust)</b> owns the database and every business decision: who may see what, when a lookup runs, "
  "when it is retried, what a status means.",
  "<b>Adapters (Python)</b> know how to talk to exactly one courier. They fetch and extract, nothing else.",
  "<b>RabbitMQ</b> is the only channel between them. They exchange small JSON messages with a fixed, versioned format.")
H2("What exists today")
P("Milestone 1 is finished. It builds the whole pipeline with a <b>dummy courier</b> instead of a real one, so every "
  "path (success, failure, retry, timeout, crash, duplicate) could be built and tested before real couriers add their "
  "own problems.")
T(["Phase", "Delivered", "Commit"], [
  ["1", "Repository layout, Docker Compose (PostgreSQL + RabbitMQ), message schemas, fixtures, topology file, CI skeleton", "4c155b4"],
  ["2", "Engine: migrations, API keys, HTTP API, transactional dispatch (job + outbox in one transaction)", "4c155b4"],
  ["2.1", "Hardening: database integrity constraints, cross-language contract test, refresh rules", "4c155b4"],
  ["3", "Engine messaging: outbox relay, event consumer, dead-letter consumer, retry scheduler, reaper", "a1640e6"],
  ["4", "Python Adapter SDK (worker runtime) and the dummy adapter", "689606b"],
  ["5", "End-to-end test suite, Dockerfiles, full Compose stack, contracts documentation", "cd28ea1"]], [14, 128, 24])
H2("What is deliberately not built yet")
B("Notifications to users, pinned (browser) sessions",
  "AI diagnosis and sandboxed AI repair (Phases 9 and 10)",
  "Metrics, circuit breaker, production deployment (Phases 11 and 12)")

# ---------------------------------------------------------------- 2
H1("2. Architecture")
C("""
   User / client
        |  HTTPS + API key
        v
 +----------------------+        job.track         +---------------------------+
 |   Engine  (Rust)     | -----------------------> |  Adapter worker (Python)  |
 |  Axum API            |      RabbitMQ            |  SDK + one adapter        |
 |  business logic      | <----------------------- |  (today: dummy adapter)   |
 |  outbox / consumers  |  job.started, job.result +---------------------------+
 +----------+-----------+                                        |
            |  SQL (only the Engine)                             v
            v                                            courier API / website
      PostgreSQL                                          (none for the dummy)
""")
H2("Rules the whole design follows")
T(["Rule", "Why"], [
  ["Only the Engine touches the database", "One owner of the data means one place for every rule. Adapters cannot corrupt state."],
  ["Adapters and Engine talk only through messages", "They can be deployed, scaled and restarted independently, and an adapter crash cannot take the Engine down."],
  ["Delivery is at-least-once, consumers are idempotent", "No broker can promise exactly-once. Instead every message may arrive twice, late or out of order, and processing it again changes nothing."],
  ["No secrets in messages", "Messages are stored in queues and logs. Passwords and API keys never travel in them."],
  ["PostgreSQL's clock is the time authority", "Deadlines are computed and compared in the database, so clock differences between machines cannot break them."],
  ["Every external response is untrusted", "Text from a courier website may be written to manipulate software (or later an AI model). It is stored as data and never executed or logged into messages."]], [58, 108])
H2("Technology")
T(["Part", "Technology"], [
  ["Engine", "Rust 2024 edition, Tokio, Axum 0.8 (HTTP), SQLx 0.9 (PostgreSQL), Lapin 4 (RabbitMQ), tracing (JSON logs)"],
  ["Adapters", "Python 3.13 managed by uv (the system Python is never touched), aio-pika, Pydantic, structlog"],
  ["Database", "PostgreSQL 17"], ["Broker", "RabbitMQ 4 with quorum queues"],
  ["Session store", "Redis 7, used only by adapter workers (Phase 8)"],
  ["Local infrastructure", "Docker Compose only (no Kubernetes)"],
  ["Tests", "cargo test with a database per test, pytest, a process-level end-to-end suite"]], [40, 126])
H2("Repository layout")
T(["Path", "Contents"], [
  ["courier-contracts/", "JSON Schemas, example messages (fixtures), RabbitMQ topology. Shared by Rust and Python; the source of truth for the message format."],
  ["courier-engine/", "The Rust Engine: src/api (HTTP), src/db (queries), src/services (business logic), src/messaging (RabbitMQ), migrations/, tests/"],
  ["courier-adapters/", "courier_adapter_sdk/ (worker runtime and contracts), adapters/dummy/, tests/"],
  ["courier-human-interface/", "Small FastAPI website where users answer one-time-code and CAPTCHA questions (Phase 8)"],
  ["courier-ai-repair/", "The AI repair service: asks a model to repair or write an adapter, checks and tests the result, pushes it to GitHub (Phase 9)"],
  ["tests-e2e/", "End-to-end tests that run the real Engine and worker"],
  ["infrastructure/", "docker-compose.yml, PostgreSQL and RabbitMQ setup scripts, smoke.sh"],
  ["docs/", "architecture.md (whole system), milestone-1.md (detailed plan + notes), contracts.md (messages)"],
  ["Makefile", "Every developer command (see chapter 10)"]], [40, 126])

# ---------------------------------------------------------------- 3
H1("3. The life of one tracking request")
P("This is the most important chapter: once this flow is clear, every table and function has an obvious place.")
T(["#", "Where", "What happens"], [
  ["1", "API", "The user sends POST /v1/tracking-requests with provider_code and tracking_number. The API key is checked."],
  ["2", "Engine, one transaction", "Four things are written together or not at all: the shipment (or the existing one is reused), the user's subscription, a tracking job in status QUEUED with its first attempt, and an outbox row containing the job.track message."],
  ["3", "Outbox relay", "A background task reads pending outbox rows and publishes them to RabbitMQ. It marks a row sent only after the broker confirmed it."],
  ["4", "Worker", "The worker receives job.track, checks it, publishes job.started, runs the adapter under a timeout, publishes job.result and only then acknowledges the job message."],
  ["5", "Engine event consumer", "job.started moves the job to PROCESSING and sets a deadline for the result. job.result closes the attempt."],
  ["6", "Engine result handling", "On success the raw response is stored, events are saved (duplicates ignored), each raw status is mapped to a normalized status, and the shipment status is updated. The job becomes COMPLETED."],
  ["7", "API", "The user reads GET /v1/tracking-requests/{id} and /events."]], [8, 34, 124])
H2("Job states")
C("""
 QUEUED --> PROCESSING --> COMPLETED            (answer received: SUCCESS or NOT_FOUND)
    |            |
    |            +--> RETRYING --> QUEUED       (attempt + 1, after a growing delay)
    |            |
    +------------+--> FAILED                    (not retryable, or attempts used up)
""")
P("Shipment status is separate from job status. The job says how the <i>lookup</i> is going; the shipment says where "
  "the <i>parcel</i> is: CREATED, PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, RETURNED, EXCEPTION, UNKNOWN.")
H2("Why an outbox?")
P("If the Engine saved the job and then published to RabbitMQ directly, a crash between the two steps would leave a job "
  "that nobody ever works on (or a message for a job that was never saved). Writing the message into the database in "
  "the same transaction as the job removes that gap: the message exists exactly when the job exists, and the relay "
  "publishes it later, as often as needed.")
H2("Logical attempt and physical execution")
B("A <b>logical attempt</b> is (job_id, attempt). The Engine creates it, it has one message, and it is closed exactly once. Table: job_attempts.",
  "A <b>physical execution</b> is one actual run of that message by a worker. A redelivery after a crash gives a second execution of the same attempt. Table: adapter_execution_logs, keyed by an execution_id the worker generates.")
P("Each execution ends with one disposition: <b>accepted</b> (its result closed the attempt), <b>duplicate</b> (another "
  "execution's result came first), <b>stale</b> (the attempt had already been closed by the reaper or dead-letter "
  "handler) or <b>incomplete</b> (started, no result: crashed or hung). The first result wins; nothing later can change it.")

# ---------------------------------------------------------------- 4
H1("4. Reliability: what happens when things go wrong")
H2("Three separate timers")
P("Waiting in a queue, running an adapter and recovering from a lost worker are different problems. Each has its own "
  "clock, owner and error class, so a busy queue is never mistaken for a slow courier.")
T(["Timer", "Measures", "Enforced by", "Error on expiry"], [
  ["Queue-wait deadline", "Time from dispatch until some worker starts the attempt (default 15 min)", "Engine reaper, while the job is QUEUED", "QUEUE_WAIT_EXCEEDED"],
  ["Adapter timeout", "Time one execution spends inside the adapter (timeout_ms). Starts when execution starts, not when the message was sent.", "Worker (asyncio.timeout)", "ADAPTER_TIMEOUT"],
  ["Execution recovery deadline", "Time the Engine waits for a result after job.started (timeout_ms + grace)", "Engine reaper, while the job is PROCESSING", "WORKER_LOST"]], [30, 56, 36, 44])
H2("One way to close an attempt")
P("Three actors can end an attempt: the result handler, the dead-letter handler and the reaper. All call one function, "
  "close_attempt, whose heart is a single guarded UPDATE:")
C("UPDATE tracking_jobs SET status = ... \nWHERE id = $job AND attempt = $attempt AND status IN ('QUEUED','PROCESSING')")
P("Whoever commits first wins; the others match zero rows and do nothing. The decision about what comes next is made "
  "in that same place: success becomes COMPLETED; a retryable failure with attempts left becomes RETRYING with a delay "
  "of base x 2^(attempt-1), plus or minus 20 % jitter, capped; everything else becomes FAILED. Only the retry scheduler "
  "ever opens a new attempt, so two competing retries are impossible.")
H2("Failure cases and their outcome")
T(["What goes wrong", "What the system does"], [
  ["Courier is down or times out", "The adapter raises a typed error; the worker reports FAILURE with a retryable class; the Engine retries with growing delays up to max_attempts."],
  ["Courier refuses permanently (blocked, bad credentials, unparseable page)", "Non-retryable class: the job fails after one attempt."],
  ["Tracking number unknown", "Outcome NOT_FOUND. This is an answer, not a failure: job COMPLETED, shipment UNKNOWN, no retry."],
  ["Adapter runs too long", "The worker cancels it at timeout_ms, cleans up, reports ADAPTER_TIMEOUT. Other jobs in the worker are unaffected."],
  ["Worker process crashes", "Its job messages were not acknowledged, so RabbitMQ delivers them again. Same attempt, new execution."],
  ["A job crashes the worker every time", "After 4 failed deliveries RabbitMQ moves the message to a dead-letter queue. The Engine consumes that queue, closes the attempt as DELIVERY_LIMIT_EXCEEDED, retries once, and fails the job the second time."],
  ["Worker hangs without dying", "No result arrives; the reaper closes the attempt as WORKER_LOST after the recovery deadline."],
  ["No worker is running", "The job waits. If the queue-wait deadline passes, the reaper closes it as QUEUE_WAIT_EXCEEDED. Old messages are dropped by the worker when it returns."],
  ["The same result arrives twice", "The Engine records every processed message_id (inbox). The second copy is recognized and ignored."],
  ["A late result for an old attempt arrives", "Logged as a stale execution. Job state, events and stored responses are untouched."],
  ["Engine is down while a worker finishes", "The result waits in the durable queue and is processed after restart."],
  ["Worker cannot publish its result", "It does not acknowledge the job, drops its broker connection and reconnects. RabbitMQ redelivers and counts the failure."],
  ["A malformed message arrives", "Rejected to a dead-letter queue. It never blocks the messages behind it."]], [56, 110])
H2("Two lessons learned from RabbitMQ 4")
B("An explicit \"requeue this message\" (nack with requeue) is <b>not</b> counted toward the queue's delivery limit; it would loop forever. Only a delivery that ends without acknowledgement (connection or process gone) is counted. The worker therefore never requeues explicitly.",
  "The message count RabbitMQ reports for a quorum queue lags behind reality, so tests look into a queue instead of trusting its count.")
H2("Known limitation")
P("When a worker crashes, every job that process was running is lost with it. A healthy job that is delivered in step "
  "with a job that always crashes the worker, through both dead-letter cycles, also ends FAILED with "
  "DELIVERY_LIMIT_EXCEEDED, because RabbitMQ cannot tell which job caused the crash. A healthy job that arrives after "
  "the first crash, or finishes between crashes, completes. A possible later fix is to retry dead-lettered jobs one at "
  "a time so the harmful one identifies itself.")

# ---------------------------------------------------------------- 5
H1("5. Data model")
P("PostgreSQL, owned by the Engine. All IDs are UUID version 7 (time-ordered). Nine migrations in "
  "courier-engine/migrations create everything.")
T(["Table", "Purpose"], [
  ["users, api_keys", "Callers. Only the SHA-256 hash and a 12-character prefix of each API key are stored."],
  ["providers", "Courier definitions: timeout_ms, queue_wait_max_ms, result_grace_ms, max_attempts, retry delays."],
  ["adapters, adapter_versions", "Which adapter (and queue) serves a provider."],
  ["provider_status_mappings", "Courier wording -> normalized status, e.g. PKG_DELIVERED -> DELIVERED."],
  ["shipments", "One \"generation\" of a tracking number with its current status. Couriers reuse numbers, so uniqueness applies only to non-archived rows."],
  ["tracking_requests", "A user's subscription to a shipment. Users own subscriptions, never shipments, so two users tracking the same parcel share one shipment and one lookup."],
  ["tracking_jobs", "One lookup of a shipment: status, attempt counter, the three deadlines, dead_letter_count, correlation_id. At most one active job per shipment (unique index)."],
  ["job_attempts", "One row per logical attempt: outcome, error class, who closed it, the accepted execution, execution count."],
  ["adapter_execution_logs", "One row per physical execution: worker, delivery count, times, reported outcome, disposition."],
  ["raw_tracking_responses", "The courier's answer exactly as received, for debugging and later AI analysis."],
  ["tracking_events", "Event history with raw and normalized status. Unique per shipment and content, so repeated lookups add no duplicates."],
  ["outbox_messages", "Messages waiting to be published (transactional outbox)."],
  ["processed_messages", "The inbox: (consumer, message_id) of every handled message, for deduplication."],
  ["dead_letters", "A record of every dead-lettered job message and whether it changed a job."]], [44, 122])
H2("Rules enforced by the database itself (migration 0009)")
P("Application code can have bugs, so the most important invariants are also constraints. A violation names the rule.")
B("Each job status requires its timestamp: QUEUED needs a queue deadline, PROCESSING an execution deadline, RETRYING a next-attempt time.",
  "A job is COMPLETED only with an outcome; completed_at is set exactly for terminal jobs; attempt never exceeds max_attempts.",
  "Every job has a job_attempts row for its current attempt (a deferred trigger that checks at commit).",
  "No job can be created for an archived shipment, and a shipment with an active job cannot be archived (triggers that lock the shipment row first).",
  "An outbox row and the message inside it must carry the same message_id and type.",
  "At most one enabled adapter per provider; API key hashes must be 32 bytes.")
H2("Concurrent requests")
P("Twenty identical requests arriving at the same instant must not create twenty shipments or jobs. The create "
  "transaction uses INSERT ... ON CONFLICT on the unique indexes: the first insert wins and locks the row, the others "
  "wait and then reuse it. The result, verified by tests: one shipment, one subscription per user, one job, one message.")

# ---------------------------------------------------------------- 6
H1("6. HTTP API and security")
P("The Engine listens on port 8080. Every /v1 route needs <font face='Courier'>Authorization: Bearer &lt;api-key&gt;</font>. "
  "Every response carries X-Correlation-Id. Errors always have the shape {\"error\": {code, message, details}}.")
T(["Method and path", "Behaviour"], [
  ["GET /healthz", "The process is alive."], ["GET /readyz", "PostgreSQL and RabbitMQ are reachable."],
  ["GET /v1/providers", "Enabled couriers."],
  ["POST /v1/tracking-requests", "Start tracking. 201 for a new subscription, 200 if the caller already has it. 404 unknown provider, 422 invalid input."],
  ["GET /v1/tracking-requests", "The caller's requests, paginated."],
  ["GET /v1/tracking-requests/{id}", "Request, shipment status and latest job."],
  ["GET .../{id}/events", "Event history with raw and normalized status."],
  ["GET .../{id}/jobs", "Lookup jobs with status, attempt, error class."],
  ["POST .../{id}/refresh", "Ask for a fresh lookup: 202 job_created, or 200 job_already_active. 409 if the shipment is archived."],
  ["DELETE .../{id}", "Cancel the caller's own subscription. 204."]], [52, 114])
H2("API keys")
B("Format: ctk_&lt;12-character prefix&gt;_&lt;256-bit random secret&gt;.",
  "Created with the command line (admin create-user / create-api-key) and printed exactly once.",
  "Only the prefix and the SHA-256 hash are stored. Lookup is by prefix; comparison is constant-time, so timing reveals nothing.",
  "Keys can be revoked. They never appear in logs.")
H2("Access control")
P("Every query is filtered by the caller's user ID. Using another user's request ID returns 404, exactly like an ID "
  "that does not exist, so the API never confirms that someone else's request exists. Subscribers of the same shipment "
  "see the same events but never each other.")
H2("Other security measures in place")
B("Two RabbitMQ accounts: the Engine account manages the topology; the worker account can only read adapter queues and publish to the events exchange. It cannot create anything and has no database access.",
  "Configuration objects print [redacted] instead of connection strings.",
  "Unexpected adapter errors are reported by type only; their text (possibly courier content) stays in the worker log.",
  "Containers run as non-root users and have no secrets baked into the images.")

# ---------------------------------------------------------------- 7
H1("7. Messages between Engine and workers")
P("All messages are JSON inside one common envelope. The exact definitions are the JSON Schemas in "
  "courier-contracts/schemas/v1; docs/contracts.md explains them in prose.")
T(["Envelope field", "Meaning"], [
  ["message_id", "Unique per message. Consumers deduplicate on it."],
  ["type, schema_version", "job.track / job.started / job.result, version \"1.0\". Unknown fields are ignored; an unknown major version is rejected."],
  ["correlation_id", "Created for the API request and copied into every message and log line, so one ID traces a request through both programs."],
  ["causation_id", "The message this one answers."], ["producer, occurred_at", "Who sent it and when (UTC)."]], [44, 122])
T(["Message", "Direction", "Content"], [
  ["job.track", "Engine -> worker", "job_id, attempt, adapter_key, provider_code, tracking_number, params, timeout_ms, expires_at"],
  ["job.started", "worker -> Engine", "job_id, attempt, execution_id, delivery_count, worker_id, adapter version, started_at"],
  ["job.result", "worker -> Engine", "The same identity plus finished_at, outcome (SUCCESS / NOT_FOUND / FAILURE), raw_response and events on success, error (class, message, retryable) on failure"]], [26, 30, 110])
H2("RabbitMQ topology (courier-contracts/topology.yaml)")
T(["Queue", "Receives", "Consumer"], [
  ["adapter.dummy.jobs", "job.track for the dummy adapter (delivery limit 3, then dead-lettered)", "Dummy workers"],
  ["engine.events", "job.started and job.result", "Engine event consumer"],
  ["adapter.dummy.jobs.dlq", "Dead-lettered job messages", "Engine dead-letter consumer"],
  ["engine.events.dlq", "Events the Engine rejected as malformed", "None (kept for operators)"]], [44, 86, 36])
H2("How the two languages are kept in agreement")
B("The same fixture files (valid and invalid examples) are run through the Rust parser and the Python models; both must accept and reject the same ones.",
  "A \"golden file\" test: the Engine's real message builder writes a job.track message to a committed file, and the Python side validates that file. If the Rust output changes, the test fails until the file is regenerated on purpose (make update-contract-fixtures; refused in CI).")

# ---------------------------------------------------------------- 8
H1("8. Important functions: the Engine (Rust)")
P("Paths are relative to courier-engine/src.")
H2("Dispatch and API keys")
M("services/dispatch.rs: create request / refresh", "Starting a lookup must be all-or-nothing and safe under simultaneous requests.",
  "One transaction: upsert the shipment (ON CONFLICT locks the row), insert the subscription, insert the job only if no active job exists, then the attempt row and the outbox row. Deadlines use the database clock.")
M("dispatch::job_track_message(...)", "The message format must be testable without a database.",
  "A pure function that builds the job.track envelope from its inputs. The golden-file contract test calls it directly.")
M("services/api_keys.rs: issue / issue_with", "Keys must be unguessable and never stored in readable form.",
  "Generates a random prefix and secret from the operating system, stores only prefix and SHA-256 hash, returns the full key once. Retries if a prefix collides.")
M("api/auth.rs: authentication extractor", "Every /v1 route needs an identified caller.",
  "Parses the Bearer header, finds the key by prefix, compares hashes in constant time, rejects revoked keys with 401, records last_used_at.")
H2("Messaging")
M("messaging/relay.rs: relay_once(pool, publisher, batch)", "Outbox rows must reach RabbitMQ at least once, even across crashes.",
  "Selects pending rows with FOR UPDATE SKIP LOCKED (so several relays never take the same row), publishes each, and marks it sent only after the broker confirmed it. A crash in between just causes a second publish with the same message_id.")
M("messaging/broker.rs: run / declare_topology / BrokerPublisher", "The Engine must survive broker outages and own the queue layout.",
  "run is a reconnect loop. On each connection it declares exchanges and queues from topology.yaml, then starts the relay and the two consumers. Publishing uses confirms, the mandatory flag and persistent messages.")
M("messaging/contracts.rs: parse_incoming_event(body)", "Nothing from outside may be trusted, including messages.",
  "Checks size, JSON shape, version and every field rule (mirroring the JSON Schemas) and returns a typed Started or Result event, or a reason for rejection.")
M("messaging/topology.rs: Topology::load / with_prefix", "Queue names must have one source, and tests need private copies.",
  "Reads topology.yaml (embedded at compile time). with_prefix puts a prefix before every exchange and queue name; ENGINE_TOPOLOGY_PREFIX uses it for end-to-end tests.")
H2("Business logic")
M("services/events.rs: handle_event -> on_started / on_result", "Worker events must be applied exactly once in effect, in any order.",
  "One transaction: record the message_id in the inbox (a duplicate stops here), lock the job, upsert the execution row by execution_id. on_started moves QUEUED to PROCESSING and sets the recovery deadline. on_result calls close_attempt and, if it won, stores the answer. The message is acknowledged only after commit.")
M("services/events.rs: store_answer", "A successful result must become stored history and a shipment status.",
  "Saves the raw response, inserts events (the unique key drops duplicates), maps each raw status through provider_status_mappings (unknown wording becomes UNKNOWN, the raw value is kept) and updates the shipment.")
M("services/close_attempt.rs: close_attempt", "Three actors may try to end the same attempt at the same moment.",
  "The single guarded UPDATE described in chapter 4. Decides COMPLETED, RETRYING (with the back-off delay) or FAILED, and counts dead-letters inside the same statement.")
M("services/dead_letter.rs: handle_dead_letter", "A job whose message was dead-lettered would otherwise stay open until a deadline.",
  "Reads job_id and attempt from the dead-lettered message, calls close_attempt with DELIVERY_LIMIT_EXCEEDED, and always writes a dead_letters row saying whether it applied. A dead-letter for an already closed attempt changes nothing.")
M("services/reaper.rs: reap_expired", "Safety net: no job may stay QUEUED or PROCESSING forever.",
  "Every second, selects jobs past their queue or execution deadline (SKIP LOCKED) and closes them as QUEUE_WAIT_EXCEEDED or WORKER_LOST.")
M("services/retry_scheduler.rs: open_due_attempts", "Exactly one actor may start a new attempt.",
  "Selects RETRYING jobs whose time has come (SKIP LOCKED) and, in one transaction, sets QUEUED, increments attempt, sets a new deadline and inserts the attempt and outbox rows.")
M("cli.rs: serve / migrate / admin", "One binary for running, migrating and administering.",
  "serve starts the API plus the broker session, scheduler and reaper, and shuts them down in order on SIGTERM (requests first, then background tasks).")

# ---------------------------------------------------------------- 9
H1("9. Important functions: Adapter SDK and dummy adapter (Python)")
P("Paths are relative to courier-adapters. An adapter author writes one class with one method; the SDK does the rest.")
C("""
class MyCourier(Adapter):
    key = "mycourier"; version = "1.0.0"

    async def track(self, request, ctx) -> TrackingResult:
        client = await ctx.enter(httpx.AsyncClient())      # closed automatically
        ...
        raise ShipmentNotFound(...)   # or RateLimited(...), ParseError(...), ...
        return TrackingResult(raw_response, events)

run(MyCourier())
""")
M("courier_adapter_sdk/worker.py: Worker.submit", "A slow job must not block the others, and the number of parallel jobs must be bounded.",
  "Takes a slot from a semaphore of size WORKER_CONCURRENCY and starts the job as its own asyncio task. It returns when the job has started, not when it finishes.")
M("Worker._run", "The job message may be acknowledged only when its result is safely stored by the broker.",
  "validate -> drop if expired -> publish job.started -> run adapter -> publish job.result (confirmed) -> acknowledge. Cleanup always runs at the end.")
M("Worker._accept", "Bad or outdated messages must never reach an adapter.",
  "Invalid, oversized or foreign messages are rejected to the dead-letter queue. A first delivery that arrives after expires_at (plus 5 s tolerance) is dropped; a redelivery is always executed.")
M("Worker._execute", "Every way an adapter can end needs one consistent classification.",
  "Runs adapter.track inside asyncio.timeout(timeout_ms). Timeout -> ADAPTER_TIMEOUT, ShipmentNotFound -> NOT_FOUND, typed errors -> their class, anything else -> INTERNAL.")
M("Worker._publish_result", "A result the Engine never receives must not be lost silently.",
  "Builds and validates job.result, replaces an oversized result with a failure, publishes with confirmation. If publishing fails, the job is left unacknowledged and the session is dropped so RabbitMQ redelivers.")
M("Worker.drain / WorkerProcess.run", "Deployments and outages must not lose or duplicate work unnecessarily.",
  "On SIGTERM the worker stops consuming, lets running jobs finish within WORKER_SHUTDOWN_GRACE_S, then cancels the rest. run reconnects with a capped back-off and waits for its queue if the Engine has not created it yet.")
M("context.py: JobContext.enter / on_cleanup", "A failed, timed-out or cancelled job must not leak HTTP clients (later: browsers).",
  "Resources are registered on an exit stack. The SDK closes it after every job, protected from cancellation and limited by WORKER_CLEANUP_TIMEOUT_S.")
M("errors.py: AdapterError classes and describe()", "The Engine decides about retries from the error class, so classification must be deterministic.",
  "Each error class fixes its error_class and whether it is retryable (NetworkTimeout, RateLimited: yes; Blocked, ParseError: no). describe() turns any exception into the error section of a result.")
M("contracts/*.py: Pydantic models; envelope.new_id()", "Python must read and write exactly the agreed message format.",
  "Models mirror the JSON Schemas (lowercase UUIDs, UTC timestamps with Z, outcome rules). new_id builds a UUID version 7 by hand because Python 3.13 has none built in.")
M("topology.py: routes_for(adapter_key)", "Queue names must come from the shared topology file, not be typed twice.", "Finds the queue bound to job.track.v1.&lt;adapter_key&gt; and the events exchange.")
H2("The dummy adapter (adapters/dummy/adapter.py)")
P("It calls nothing external. The tracking-number prefix selects a behaviour, which is how every path can be tested on demand.")
T(["Prefix", "Behaviour"], [
  ["DUMMY-OK-", "Three events ending in delivered"], ["DUMMY-TRANSIT-", "Two events, in transit"],
  ["DUMMY-ODD-", "Includes a status the Engine has no mapping for"], ["DUMMY-NOTFOUND-", "Number unknown"],
  ["DUMMY-FAIL- / DUMMY-FATAL-", "Retryable failure every time / permanent failure"],
  ["DUMMY-FLAKY-", "Fails the first attempt, then succeeds"],
  ["DUMMY-SLOW- / DUMMY-DELAY-&lt;ms&gt;-", "Never finishes (timeout) / waits the given time, then succeeds"],
  ["DUMMY-HANG-, -CRASHONCE-, -CRASH-, -DUP-", "Simulated worker hang, crash once, crash always, duplicated result. These act only with WORKER_ALLOW_FAULT_INJECTION=true; otherwise they are an ordinary INTERNAL failure, so a production worker cannot be made to crash by a tracking number."]], [56, 110])

# ---------------------------------------------------------------- 10
H1("10. Running, testing and operating")
H2("Commands (Makefile)")
T(["Command", "What it does"], [
  ["make setup", "Creates the project-local Python environments with uv (Python 3.13). The system Python is untouched."],
  ["make up-deps / verify-deps", "Starts PostgreSQL (host port 55432) and RabbitMQ (5672, management UI 15672) and checks users and permissions."],
  ["make migrate", "Applies database migrations."],
  ["make run-engine / run-worker", "Runs the Engine and the dummy worker on the host."],
  ["make test-contracts", "Schemas, fixtures, topology (76 tests)."],
  ["make test-engine", "cargo fmt, clippy with warnings as errors, 120 unit and integration tests."],
  ["make test-adapters", "ruff, mypy (strict), 136 tests including real RabbitMQ."],
  ["make test-e2e", "26 end-to-end tests, about 80 seconds."],
  ["make test", "All of the above."],
  ["make up / make smoke", "Builds and starts the full container stack; sends one tracking request through it."],
  ["make down / destroy", "Stops containers (destroy also deletes the data volumes)."]], [50, 116])
H2("Trying it by hand")
C("""
make up-deps && make migrate
cd courier-engine && cargo run -- admin create-user --email you@example.com   # prints the key once
make run-engine          # terminal 1
make run-worker          # terminal 2
curl -H "Authorization: Bearer <key>" -H 'content-type: application/json' \\
     -d '{"provider_code":"dummy","tracking_number":"DUMMY-OK-1"}' \\
     http://127.0.0.1:8080/v1/tracking-requests
""")
H2("How the tests are organized")
T(["Suite", "Technique"], [
  ["Engine", "Each test gets its own empty database. Handlers are called directly so message order can be controlled exactly (result before started, stale dead-letter, reaper racing the dead-letter handler). Broker tests use a randomly prefixed topology that is deleted afterwards."],
  ["Adapter SDK", "Jobs wait on gates the test opens, so \"exactly 2 jobs run at once\" is proven without sleeps. Integration tests start the real worker process and send it real signals."],
  ["End-to-end", "tests-e2e/harness.py starts the real Engine binary and worker as supervised processes on a private database (courier_e2e) and prefixed queues, restarts the worker when it dies, and can stop or kill either one. Provider timings are shortened so failures play out in seconds."]], [30, 136])
H2("Configuration (.env, created from .env.example)")
T(["Setting", "Meaning"], [
  ["DATABASE_URL, ENGINE_AMQP_URL, WORKER_AMQP_URL", "Connection strings. Secrets: never logged."],
  ["ENGINE_HTTP_ADDR", "API address, default 127.0.0.1:8080."],
  ["OUTBOX_POLL_MS, RETRY_SCHEDULER_INTERVAL_MS, REAPER_INTERVAL_MS", "How often the background tasks look for work (250 / 500 / 1000 ms)."],
  ["MAX_DEAD_LETTER_RETRIES", "Retries after a dead-letter (1)."],
  ["WORKER_CONCURRENCY, WORKER_PREFETCH", "Parallel jobs per worker (4)."],
  ["WORKER_SHUTDOWN_GRACE_S, WORKER_CLEANUP_TIMEOUT_S", "Shutdown and cleanup limits (30 s / 5 s)."],
  ["WORKER_ALLOW_FAULT_INJECTION", "Test environments only."],
  ["Per provider, in the providers table", "timeout_ms, queue_wait_max_ms, result_grace_ms, max_attempts, retry_base_ms, retry_max_ms"]], [70, 96])

# ---------------------------------------------------------------- 11
H1("11. Phase 6: credentials, real adapters, rate limiting")
P("Phase 6 lets the system talk to real couriers. That needs three things the dummy courier never did: secrets (API keys), "
  "politeness towards the courier (rate limits) and permission (for scraping). Full details are in docs/phase-6.md.")
H2("How a secret travels")
B("An operator stores the secret with the admin command set-credential. The Engine encrypts it; the database holds only ciphertext.",
  "The job message carries only credential_ref, an ID that is useless on its own. No secret ever enters RabbitMQ.",
  "The worker exchanges the reference for the secret over the Engine's internal HTTP API, proving who it is with a worker token.",
  "The Engine answers only for a job of that worker's adapter that is in flight right now, and records every request in an append-only audit log.",
  "The adapter holds the secret in memory for one job. The Secret type prints only its field names, so it cannot be logged by accident.")
H2("Important functions")
M("MasterKey::seal / open (services/crypto.rs)",
  "Secrets must not be stored in plain text.",
  "Envelope encryption with AES-256-GCM: a random data key encrypts the secret, the master key (CREDENTIAL_MASTER_KEY) encrypts the data key. Both are bound to the credential and provider IDs, so ciphertext copied to another row cannot be opened.")
M("credentials::resolve (services/credentials.rs)",
  "Decides whether a worker may have a secret; the one place that hands secrets out.",
  "Checks, in one transaction: the token's adapter serves the job, the attempt is the job's current one and is QUEUED or PROCESSING, the reference belongs to the job's provider, a credential is in use. Writes an audit row for every outcome. All refusals look identical to the caller (403).")
M("worker_tokens::issue / authenticate",
  "Workers need an identity that is separate from user API keys and tied to one adapter.",
  "Token cwt_<prefix>_<secret>; only the prefix and the SHA-256 hash are stored; compared in constant time; revocable.")
M("DispatchTarget::blocked_reason (db/providers.rs)",
  "A job that cannot succeed, or must not run, should never be created.",
  "Refuses providers that need a credential but have none, and scraping providers whose site owner's permission has not been recorded (scraping_authorized). The API answers 409 provider_unavailable.")
M("reserve_dispatch_slot (db/providers.rs)",
  "A courier's API must not be called faster than it allows.",
  "Each provider has min_dispatch_interval_ms. Inside the dispatch transaction the next free time slot is taken from the provider row (row lock = no two jobs share a slot). The outbox message becomes publishable at its slot and the queue-wait deadline starts there.")
M("set_test_providers_enabled (ENABLE_DUMMY_PROVIDER)",
  "Test couriers must not be offered in production.",
  "At start-up the Engine enables or disables every provider flagged is_test (dummy, demosite).")
M("JobContext.credentials() and JobContext.http() (SDK)",
  "Adapters need the secret and an HTTP client without handling tokens, cleanup or error mapping themselves.",
  "credentials() calls the Engine with the job's own identity. http() returns a client that is closed when the job ends. check_status() and describe() turn HTTP statuses and network failures into the right error class (429 with Retry-After, 5xx, 401, 403, timeouts).")
M("DhlAdapter.track and parse_shipments (adapters/dhl)",
  "The first official-API courier.",
  "Calls DHL's Shipment Tracking - Unified API with the key in a header, then turns the JSON into events. Written from DHL's published description and tested against a fake DHL server; not yet tried against the real API.")
M("DemoSiteAdapter.track and parse_tracking_page (adapters/demosite)",
  "The first scraping adapter, built without touching any real website.",
  "Fetches a page of the demo courier website shipped in demo_site/ and parses its history table with BeautifulSoup. A redesigned page is reported as SCHEMA_DRIFT, an unreadable value as PARSE_ERROR; page text is treated as data only.")
H2("Operating it")
C("""
make master-key        # put the output in .env as CREDENTIAL_MASTER_KEY
cd courier-engine
cargo run -- admin set-credential --provider dhl < dhl.json     # {"api_key": "..."}
cargo run -- admin create-worker-token --adapter dhl            # printed once
cargo run -- admin set-scraping-authorized --provider X --authorized true
""")
P("Losing the master key makes the stored credentials unreadable: back it up separately from the database.")

# ---------------------------------------------------------------- 12
H1("12. Phase 7: re-polling, status mappings, archiving")
P("Until Phase 7 a shipment was looked up once, an unknown courier wording stayed unknown for ever, and the database only grew. "
  "Phase 7 fixes those three things. No message changed and the adapters were not touched. Full details are in docs/phase-7.md.")
H2("Automatic re-polling")
B("The Engine looks a shipment up again and again until it is DELIVERED or RETURNED. A poll is an ordinary job (trigger poll), so retries, timers, rate limits and credentials all apply.",
  "Each provider has a policy: poll_interval_ms (0 = never polled), poll_max_interval_ms and poll_give_up_days. DHL: every 30 minutes, slowing down to every 6 hours.",
  "While nothing new appears the interval doubles up to the maximum; a new event resets it.",
  "Polling stops when the shipment is finished, when nobody is subscribed any more, or after poll_give_up_days without news. It pauses while the provider is unavailable.",
  "The API shows the next lookup as next_check_at.")
H2("Status-mapping management")
B("admin list-unmapped-statuses shows courier wordings that have no mapping, most frequent first.",
  "admin set-status-mapping maps a wording. The change is also applied to the past: stored events are re-normalized and every affected shipment's status is recomputed, in one transaction.",
  "Every change is written to the audit log with the value it replaced.")
H2("Archiving and retention")
B("Archive: a shipment is closed 90 days (reuse_window_days) after it finished, or after 90 days without subscriber and news. The same tracking number then starts a new shipment.",
  "Purge: RETENTION_DAYS (180) after archiving, its events and raw responses are deleted. The shipment, its jobs and the users' requests stay. This cannot be undone; 0 switches it off.",
  "Messages: sent outbox rows are deleted after 7 days, dedupe (inbox) rows after 30.",
  "Runs every 5 minutes and on demand: make maintenance.")
H2("Important functions")
M("polling::poll_due (services/polling.rs)",
  "Something must start the next lookup of every open shipment, exactly once, even with several Engine instances.",
  "Locks shipments whose next_poll_at has passed (FOR UPDATE SKIP LOCKED) and, in the same transaction, creates their jobs with the same ensure_job the API uses. Shipments that are finished, unwatched or stale are taken off the schedule instead.")
M("polling::schedule_next_poll",
  "One place decides when a shipment is looked up next.",
  "next_poll_at = now + min(maximum, base interval x 2^(lookups without news)); cleared for finished or archived shipments and for providers without a policy. Called whenever a job is created and whenever a result brings news.")
M("events::recompute_shipment_status",
  "The shipment status must always follow from its event history, whether a result arrives or a mapping changes.",
  "Sets the status to that of the newest event with a known status, sets or clears terminal_at, and takes a finished shipment off the polling schedule. Reports whether the status changed.")
M("mappings::set_mapping / remove_mapping / list_unmapped (services/mappings.rs)",
  "Operators must be able to teach the system a courier's wording without losing or falsifying history.",
  "Stores the mapping, locks the affected shipments in a fixed order, rewrites the normalized status of matching events, recomputes each shipment, reschedules polling where needed and writes an audit row - all or nothing.")
M("retention::archive_due / purge_due / clean_messages / run_maintenance (services/retention.rs)",
  "Couriers reuse tracking numbers, and old data must not accumulate for ever.",
  "Each step selects a bounded batch of due rows, skipping rows another instance holds, and repeats until nothing is left. A shipment with a running lookup is never archived.")

# ---------------------------------------------------------------- 13
H1("13. Phase 8: one-time codes, CAPTCHAs and the human interface")
P("Some courier websites show the tracking page only after a step that needs a person: a code texted to the consignee, or a CAPTCHA. "
  "Phase 8 lets a lookup stop at that step, ask the right user and continue with the answer. Full details are in docs/phase-8.md.")
H2("The workflow in seven steps")
B("The adapter meets the step and raises otp_required(...) or captcha_required(...), handing over its <b>session</b>: the plain data it needs to continue (a transaction ID, cookies).",
  "The worker encrypts the session, stores it in <b>Redis</b> with an expiry, sends job.intervention_required to the Engine and acknowledges the job. It does not wait; it takes the next job.",
  "The Engine sets the job to WAITING_FOR_OTP or WAITING_FOR_CAPTCHA and creates a human_interventions row assigned to one user: the one who started the lookup.",
  "The user sees the question (prompt, and the picture for a CAPTCHA) through GET /v1/interventions or the human interface, and posts the answer.",
  "The Engine checks the answer's form, stores it encrypted, puts the job back to QUEUED in the <b>same attempt</b> and sends job.resume. That message holds only two IDs, never the answer.",
  "Any worker of the adapter loads the session from Redis, fetches the answer from the Engine's internal API (worker token, audited) and calls adapter.resume(session, answer).",
  "The result closes the attempt as usual. The answer, the picture and the session are deleted.")
H2("How it can end")
T(["Situation", "Result"], [
  ["Right answer", "Job COMPLETED on the same attempt; two executions: suspended, then accepted."],
  ["The provider refuses the answer", "The old challenge becomes rejected; a new one is put to the user."],
  ["More than max_challenges (3) in one job", "Job FAILED with CHALLENGE_LIMIT_EXCEEDED, no retry."],
  ["Nobody answers in time (default 10 minutes)", "Job EXPIRED with INTERVENTION_EXPIRED. A poll or manual refresh starts over."],
  ["The session is gone (Redis restarted, expired)", "The worker reports SESSION_LOST; a new attempt runs and asks again."],
  ["Answer of the wrong length or characters", "Refused at once (422); the challenge stays open. Five such answers close it."]], [62, 104])
H2("Why it is safe")
B("The answer is never in a RabbitMQ message and never in a log. It is stored encrypted with the master key and deleted when the challenge closes.",
  "The session never reaches the Engine. In Redis it is ciphertext (AES-256-GCM, key WORKER_SESSION_KEY), bound to its reference, and it expires.",
  "A challenge is visible to one user only; everybody else gets 404. Only a worker of the job's adapter can fetch the answer, only while that attempt runs, and only by naming the session the challenge was raised with.",
  "Only one worker at a time may continue a session. A lease in Redis (taken atomically, owned by a random token, expiring by itself) stops two deliveries of the same job.resume from both submitting the answer. A copy that arrives after the outcome was reported is simply acknowledged.",
  "The lease does not make provider calls exactly-once: a worker can submit the code and die before reporting. The resume then runs again after the lease expires, and the user may be asked once more.",
  "CAPTCHAs are never solved by software. The picture is passed to the user unchanged; only PNG, JPEG, GIF and WebP are accepted (no SVG, which can contain scripts).",
  "The prompt is written by the adapter. Text from the courier's page is untrusted and is never shown or followed.")
H2("The human interface")
P("A small FastAPI website (courier-human-interface/, http://localhost:8088). The user signs in with their API key, sees what is waiting, "
  "and answers. It is only a client of the Engine's public API: it has no database, RabbitMQ or Redis access and no secrets, so it can "
  "never show more than the signed-in user's key allows. The browser gets a random cookie, not the key; every form carries an "
  "anti-forgery token; the pages allow no scripts at all.")
H2("Trying it with the demo site")
B("Put CREDENTIAL_MASTER_KEY (make master-key), WORKER_SESSION_KEY (make session-key) and DEMOSITE_WORKER_TOKEN (admin create-worker-token --adapter demosite) into .env, then make up.",
  "Track DEMO-OTP-1 or DEMO-CAPTCHA-1 with provider demosite.",
  "Open http://localhost:8088 and sign in. The code for the OTP is shown by the site's pretend phone: http://localhost:8090/inbox?number=DEMO-OTP-1. The CAPTCHA is read from the picture.",
  "After the answer the request completes with status IN_TRANSIT.")
H2("Important functions")
M("interventions::on_intervention_required (Engine, services/interventions.rs)",
  "The Engine must turn a worker's question into a waiting job and a task for exactly one user, even if the message arrives twice or late.",
  "Locks the job's attempt, records the execution as suspended, marks an earlier answered challenge as rejected, enforces max_challenges, sets WAITING_FOR_* with a deadline and inserts the human_interventions row. A message for a closed attempt changes nothing.")
M("interventions::submit",
  "A user's answer must be accepted once, only from the assigned user, only while the challenge is open, and must lead to exactly one resume.",
  "In one transaction: locks shipment then job, checks owner, state, expiry and the answer's form, encrypts the answer, sets the job to QUEUED with a new queue deadline and writes the job.resume outbox row.")
M("interventions::resolve_response",
  "The worker needs the answer, but nobody else may get it.",
  "Authenticates the worker token, checks that the challenge belongs to that adapter's job and current running attempt, decrypts and returns the answer, and writes an audit row. Repeatable, so a redelivered resume still works.")
M("interventions::expire_due",
  "A job must not wait for ever for a person.",
  "Called by the reaper: jobs whose intervention deadline passed become EXPIRED (INTERVENTION_EXPIRED); challenges of attempts that ended another way are closed and wiped.")
M("otp_required / captcha_required and Adapter.resume (SDK, interventions.py, base.py)",
  "Adapter authors need one simple way to say that a person is needed and to continue afterwards.",
  "Both build a NeedsHuman signal with kind, prompt, answer format, optional picture and the session. resume(request, ctx, session, response) continues, possibly in another process, and may raise a new challenge.")
M("Worker._pause / _call_adapter / _publish_intervention (SDK, worker.py)",
  "The worker must park a lookup without holding a slot, and continue it without ever exposing the answer.",
  "_pause stores the session under a new reference before anything is reported. _call_adapter, for job.resume, loads the session (missing means SESSION_LOST), fetches the answer and calls resume. The session is deleted only after the result is confirmed and acknowledged, so a redelivery can run again.")
M("Worker._take_lease / _release_lease and the store's acquire_lease / release_lease (SDK)",
  "RabbitMQ can hand the same job.resume to two workers at once; both must not submit the user's answer.",
  "SET NX PX creates the lease only if absent, with a lifetime of the job's timeout plus a margin. A worker that finds it taken keeps its message and waits: it acknowledges if the other execution reports, takes over if the lease is released or expires, and hands the message back to the broker if the wait runs out. Release is compare-and-delete on the owner token, in a finally block.")
M("SessionCipher and RedisSessionStore (SDK, sessions.py)",
  "Sessions can contain cookies and must survive a worker restart without being readable by anyone who reads Redis.",
  "seal() encrypts with AES-256-GCM using the session reference as authenticated data, so a value copied under another reference does not open. The store saves it with an expiry; an unreadable or missing value is treated as lost.")
M("create_app (human interface, app.py) with image_src and answer_field",
  "Users need a page to answer on, and that page must not become a way to see other users' data or to run injected content.",
  "Every page calls the Engine with the signed-in user's key. image_src embeds a picture only if it is a plain image of an allowed type; answer_field derives input hints from the answer format; the Engine still validates.")
H2("Deliberate differences from the original design")
B("job.resume is delivered to the adapter's normal jobs queue instead of a separate resume queue: one consumer, one delivery limit, one dead-letter path.",
  "Only portable sessions are built. Pinned sessions (a live browser page that only one worker holds) come with the first browser adapter.",
  "Users answer with their API key (through the API or the human interface) instead of a one-time link, because the system cannot send links to users yet.",
  "Challenges have their own limit (max_challenges) instead of using up retry attempts.")

# ---------------------------------------------------------------- 14
H1("14. Five Indian couriers")
P("Between Phase 8 and Phase 9 five adapters were added. Each was written from what the courier publishes or from what its public tracking page does. "
  "No consignment was looked up on any courier's system, so none has been tried against the real service. Full details are in docs/indian-couriers.md.")
T(["Courier", "How it is tracked", "Needs before use"], [
  ["Delhivery", "Official tracking API with an account token", "API token (stored as a credential)"],
  ["Safexpress", "Customer API: OAuth2 client credentials and an API key. The public website demands an OTP and a CAPTCHA.", "Client id, client secret, API key; endpoint address to be confirmed"],
  ["SkyKing", "The JSON service behind its tracking page (two calls)", "SkyKing's permission"],
  ["ABT Parcel Service", "The tracking form on its website. Tracking was switched off for maintenance in November 2026.", "ABT's permission; a saved result page"],
  ["Balaji Roadlines", "The LR tracking page of Sri Balaji Transport Lines", "The company's permission; a saved result page"],
], [32, 76, 56])
H2("Safe by default")
B("All five are in the database, but the Engine refuses a job for any of them until its requirement is met: a stored credential for the two APIs, a recorded permission for the three websites.",
  "The ABT and Balaji result pages could not be examined. Their adapters read a page only if it has a table with a date column and a status column; anything else is reported as SCHEMA_DRIFT, never guessed.",
  "Access data found inside Safexpress's website script belongs to Safexpress and is not used.")
H2("Important functions")
M("status_code (adapters/india.py)",
  "Four of the couriers describe a status in a sentence. The Engine maps raw statuses one by one, which sentences would make impossible.",
  "Recognized wording becomes one of seven codes (booked, picked-up, in-transit, out-for-delivery, delivered, undelivered, returned). Unrecognized wording is passed on unchanged and appears in the list of unmapped statuses.")
M("local_time (adapters/india.py)",
  "These couriers give times as Indian local time without saying so, in several notations.",
  "It tries ISO first, then the day-first notations, takes a time without an offset as IST and returns UTC. An unreadable date is a PARSE_ERROR that does not quote the value.")
M("history_events (adapters/tables.py)",
  "A result page whose layout is unknown must still be read safely.",
  "It finds the table whose header names a date and a status column and turns its rows into events; a 'no record found' page means not found; any other page is SCHEMA_DRIFT.")
M("parse_packages (adapters/delhivery/parser.py)",
  "Delhivery's answer nests scans inside shipments, and 'In Transit' means opposite things on the way out and on the way back.",
  "It picks the shipment with the requested waybill and reports each scan as direction:status, for example UD:In Transit or RT:In Transit.")

# ---------------------------------------------------------------- 15
H1("15. Phase 9: the AI repair service")
P("When an adapter can no longer read its courier's answers, or a courier has no adapter yet, an AI model writes the code. "
  "A separate program, the repair service, checks that code, tests it in a sandbox and pushes it to the main branch on GitHub; other devices get it with git pull. "
  "Nobody reviews the code before it reaches main: that was chosen for this phase, and the automatic checks are the only gate. Full details are in docs/phase-9.md.")
H2("The path of a repair")
B("The adapter calls ctx.keep_evidence(response) on the courier's answer. If the lookup fails with PARSE_ERROR or SCHEMA_DRIFT, the worker sends that answer along with the failure.",
  "The Engine stores it. After three such lookups of one adapter version within 24 hours it opens a repair task.",
  "The repair service claims the task over the Engine's internal API, with its own token. It has no access to the database, RabbitMQ or Redis.",
  "It makes a fresh clone from GitHub, removes the tracking number, e-mail addresses and phone numbers from the answers, and asks the model (through OpenRouter) for whole files.",
  "The guard checks the proposal against fixed rules before a file is written.",
  "The linter, the type checker and the adapter's tests run in a sandbox without network and without secrets. A rejected or failing proposal goes back to the model, three rounds at most.",
  "Only the files of the task are committed and pushed (never forced). The Engine records the commit, or the reason for the failure.")
H2("What generated code may not do")
T(["Rule", "Detail"], [
  ["Files", "Only the adapter's own folder and its one test file. Not the SDK, another adapter, the Engine, CI or configuration."],
  ["Imports", "An exact list: os, re, typing, datetime, json, dataclasses, httpx, bs4, the SDK's public modules and the shared helpers."],
  ["Escape hatches", "No eval, exec, open, getattr, attributes starting with two underscores, file writing or starting programs."],
  ["Hosts", "Every address in the code must belong to the adapter's existing hosts, or to the hosts the operator named for a new provider. Endpoint addresses stay in the adapters."],
  ["Environment", "Only variables named after the adapter. A worker token or the session key cannot be read."],
  ["Shape", "Same key, higher version, keep_evidence still called, no fewer tests, and the recorded answers used as test fixtures."],
], [30, 136])
H2("New providers")
P("An operator runs admin request-adapter with a code, a name, the method (api or scrape), the hosts the adapter may contact, notes and, if available, an example answer. "
  "The model writes only the adapter and its tests. The queue and dead-letter queue, the database rows with the seven status mappings and the worker service are produced from templates, not by the model. "
  "The new provider stays unusable until its credential is stored or the site owner's permission is recorded, like every other provider.")
H2("Important functions")
M("JobContext.keep_evidence (SDK, context.py) and Worker._execute",
  "A repair needs the exact answer the adapter failed on; logs and error texts do not contain it.",
  "keep_evidence remembers the answer (at most 100,000 characters). The worker attaches it to the result only when the failure is PARSE_ERROR or SCHEMA_DRIFT; every other outcome sends nothing extra.")
M("repairs::record_failure (Engine, services/repairs.rs)",
  "One odd shipment must not start a repair, and workers that still run the old version must not start a second one.",
  "It stores the answer once per execution, keeps the ten newest per adapter, and inserts a task only if three different lookups of that version failed within 24 hours and no task is open, published for that version, or failed in the last six hours.")
M("repairs::claim and repairs::finish (Engine)",
  "Two repair services, or one that crashed, must not both work on a task or leave it stuck.",
  "claim marks the oldest waiting task as running under a row lock that skips locked rows, and hands out a task again if its claim is older than two hours. finish accepts an outcome only for a running task.")
M("require_repair_token (Engine, api/internal.rs)",
  "The repair service must reach its two routes and nothing else, and a worker token must not open them.",
  "It compares the presented token with ENGINE_REPAIR_TOKEN in constant time. With no token configured, or one shorter than 32 characters, everybody is refused.")
M("check_proposal and check_python (repair service, guard.py)",
  "The model's answer is untrusted; a courier's page it was shown may contain text written to mislead it.",
  "The files are parsed, not run. Every import, name, attribute and text constant is compared with the rules above; then the proposal as a whole is checked for key, version, evidence call, test count and fixtures.")
M("Bubblewrap.run (repair service, sandbox.py)",
  "Rules on code can be circumvented; running generated tests must be harmless even then.",
  "It starts the command in new namespaces with no network, an empty environment and a file system that holds only system libraries, the Python environment and the clone (read-only while tests run). Without bubblewrap the service refuses to start.")
M("scaffold (repair service, scaffold.py)",
  "A provider needs queues, database rows and a worker service; none of that may be left to a model.",
  "It validates the request again and inserts template blocks into the topology and Compose files and writes the next numbered migration. An existing adapter is never overwritten.")
M("Workspace.commit and Workspace.push (repair service, workspace.py)",
  "Only the task's files may be published, and the GitHub token must not leak.",
  "commit fails if any other file changed. The token reaches Git only through its environment for clone and push; hooks are switched off; the push is never forced, so it fails if main moved.")
H2("What can still go wrong")
B("The tests are written by the same model as the code. An adapter can pass them and still read a courier's page wrongly.",
  "The guard is not a proof: code could assemble an address from pieces at run time. Once on main, an adapter has what a worker has. Setting AI_REPAIR_PUSH_BRANCH to another branch restores a human review.",
  "The unreadable answers are sent to OpenRouter and committed as test fixtures. Names and street addresses are not recognized by the scrubber.",
  "Other devices must pull and rebuild: a repaired adapter needs its worker restarted, a new provider also needs the Engine rebuilt (migrations and queues are compiled into it).")

# ---------------------------------------------------------------- 16
H1("16. Current state, open points and what comes next")
H2("Verified")
B("922 automated tests pass: 99 contract, 189 Engine, 446 adapter, 29 human-interface, 102 AI repair service, 57 end-to-end.",
  "The real Engine binary and real worker process work together through real PostgreSQL and RabbitMQ, including crash, hang, restart and duplicate scenarios.",
  "The worker runs correctly with the restricted RabbitMQ account.")
H2("Not yet verified")
B("The Docker images and the full container stack (make up, make smoke) had not been built at the time of writing; the Compose file itself validates.",
  "The GitHub Actions workflow has been written but never run.",
  "The DHL adapter has never called the real DHL API; no real website has been scraped (that needs the site owner's permission).",
  "The AI repair service has never called OpenRouter or pushed to GitHub: its tests use scripted model answers and a local Git repository.",
  "The Redis and human-interface containers have not been started by the author either; the tests used a Redis on the development machine and the human interface as a local process.",
  "Master-key and session-key rotation tooling and an external secrets manager are not built.")
H2("Open decisions and leftovers")
B("The dummy courier is created by migration 0008 in every database. Since Phase 6 the ENABLE_DUMMY_PROVIDER setting can switch it off.",
  "Test leftovers in the development PostgreSQL: a few smoke-test users and shipments in database courier, the database courier_e2e (recreated by every end-to-end run) and one old _sqlx_test_... database. None affect the system.",
  "adapter_versions is not filled yet; there is no minimum interval between manual refreshes; status mappings are managed by command line only.",
  "The \"job in step with a crashing job\" limitation described in chapter 4.")
H2("Roadmap")
T(["Phase", "Deliverable"], [
  ["6 (done)", "Encrypted credential storage and an internal resolve API, the DHL adapter, a scraping adapter against a local demo site, per-provider rate limiting"],
  ["7 (done)", "Status-mapping management, automatic re-polling until a shipment is finished, archiving and retention"],
  ["8 (done)", "Redis, sessions, OTP / CAPTCHA intervention workflow, minimal human interface"],
  ["9 (done)", "AI repair service: repairs a broken adapter or writes one for a new courier, checks it in a sandbox and pushes it to GitHub"],
  ["10", "AI failure diagnosis and status interpretation (advisory only); optional human review of repairs"],
  ["11", "Metrics, circuit breaker, dead-letter tooling, chaos tests"],
  ["12", "Production images and deployment documentation"]], [16, 150])
H2("Glossary")
T(["Term", "Meaning"], [
  ["Adapter", "Python class that talks to one courier."], ["Worker", "A process running one adapter for many jobs at once."],
  ["Job", "One lookup of one shipment."], ["Attempt / execution", "A logical try created by the Engine / one actual run by a worker."],
  ["Outbox", "Table of messages to publish, written in the same transaction as the data they describe."],
  ["Inbox", "Table of message IDs already processed, used to ignore duplicates."],
  ["Dead-letter queue (DLQ)", "Where RabbitMQ puts messages that were rejected or failed too often."],
  ["Reaper", "Engine task that closes attempts whose deadline has passed."],
  ["Idempotent", "Doing it twice has the same effect as doing it once."],
  ["Correlation ID", "One ID that follows a request through API, messages and logs."],
  ["Intervention / challenge", "A question only a person can answer: a one-time code or a CAPTCHA."],
  ["Session", "What an adapter needs to continue a paused lookup; kept encrypted in Redis."],
  ["Quorum queue", "RabbitMQ's replicated, durable queue type with a delivery limit."]], [44, 122])

def footer(canvas, doc):
    canvas.saveState(); canvas.setFont("Helvetica", 8); canvas.setFillColor(colors.HexColor("#7b8794"))
    if doc.page > 1:
        canvas.drawString(20 * mm, 11 * mm, "Courier Tracking System - project guide")
        canvas.drawRightString(190 * mm, 11 * mm, f"Page {doc.page}")
    canvas.restoreState()

doc = SimpleDocTemplate(OUT, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                        title="Courier Tracking System - Project Guide", author="Courier Tracking System")
doc.build(S, onFirstPage=footer, onLaterPages=footer)
