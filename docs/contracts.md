# Message contracts (v1)

The Engine (Rust) and the adapter workers (Python) only talk through RabbitMQ messages. This
page explains those messages. The authoritative definitions are the files in
`courier-contracts/`:

| File | Content |
|---|---|
| `schemas/v1/*.schema.json` | JSON Schemas of the envelope and the three messages |
| `fixtures/v1/valid`, `invalid` | Example messages both languages must accept / reject |
| `fixtures/v1/produced/job.track.engine.json` | A message written by the Engine's real builder |
| `topology.yaml` | Exchanges, queues, bindings, limits |

## Rules that apply to every message

- **Delivery is at-least-once.** Any message can arrive twice, late, or out of order. Every
  consumer is idempotent; nothing assumes exactly-once.
- **No secrets.** Messages never carry passwords, API keys, cookies or tokens.
- **Size limit:** 256 KiB per message.
- **Versioning:** `schema_version` is `"1.<minor>"`. Unknown fields are ignored by both sides
  (a minor version may add fields). An unknown major version is rejected. The major version
  is also part of the routing key (`job.track.v1.dummy`).
- **Timestamps** are UTC with a `Z` suffix. **IDs** are lowercase UUIDs.
- Messages are published persistent, with publisher confirms and the `mandatory` flag.

## Envelope

Every message is wrapped in the same envelope.

| Field | Meaning |
|---|---|
| `message_id` | Unique per message. Consumers deduplicate on it. |
| `type` | `job.track`, `job.started`, `job.result`, `job.intervention_required` or `job.resume` |
| `schema_version` | `"1.0"` |
| `occurred_at` | When the sender created the message |
| `correlation_id` | Created by the Engine for the API request; copied unchanged into every later message and log line |
| `causation_id` | `message_id` of the message this one answers (`null` for `job.track`) |
| `producer` | Who sent it, e.g. `engine@0.1.0`, `adapter-dummy@0.1.0` |
| `payload` | The message itself |

## The messages

| Message | Direction | Exchange → queue | Meaning |
|---|---|---|---|
| `job.track` | Engine → worker | `courier.jobs` → `adapter.<key>.jobs` | Look this shipment up. One message per logical attempt. |
| `job.started` | worker → Engine | `courier.events` → `engine.events` | One physical execution began. Advisory. |
| `job.result` | worker → Engine | `courier.events` → `engine.events` | The outcome of one physical execution. |
| `job.intervention_required` | worker → Engine | `courier.events` → `engine.events` | One execution stopped: the provider wants a one-time code or CAPTCHA answer from a person. |
| `job.resume` | Engine → worker | `courier.jobs` → `adapter.<key>.jobs` | The person answered: continue the same attempt. |

### `job.track`

| Field | Meaning |
|---|---|
| `job_id`, `attempt` | The logical attempt. Echoed back in every answer. |
| `adapter_key`, `provider_code` | Which adapter and courier |
| `tracking_number`, `params` | What to look up (`params` is non-secret) |
| `timeout_ms` | Adapter execution budget, counted from the moment the worker starts executing |
| `expires_at` | Queue-wait deadline. A worker drops a *first* delivery that arrives later than this. |
| `credential_ref` | Optional. Opaque ID the worker exchanges for the provider's secret over the Engine's internal API (see [phase-6.md](phase-6.md)). Never the secret itself; absent when the provider has no stored credential. |

### `job.started` and `job.result`

Both carry the execution identity: `job_id`, `attempt`, `execution_id` (new for every delivery
the worker executes), `delivery_count` (0 on the first delivery), `worker_id`, `adapter_key`,
`adapter_version`, `started_at`.

`job.result` adds `finished_at`, `outcome`, and depending on the outcome:

| `outcome` | Required | Forbidden |
|---|---|---|
| `SUCCESS` | `raw_response`, `events` | `error` |
| `NOT_FOUND` | — | `error` |
| `FAILURE` | `error` (`class`, `message`, `retryable`, optional `retry_after_ms`) | — |

`events` contain the provider's own wording (`raw_status`); the Engine maps it to a normalized
status. Adapters never normalize.

### `job.intervention_required` and `job.resume`

See [phase-8.md](phase-8.md) for the workflow.

`job.intervention_required` carries the execution identity and `finished_at`, plus:

| Field | Meaning |
|---|---|
| `kind` | `otp` or `captcha` |
| `prompt` | Text for the user, written by the adapter (1–500 characters) |
| `session_ref` | Opaque ID of the session the worker saved. Not the session, and useless without the workers' session key. |
| `expires_in_s` | Optional, 30–3600: how long the provider accepts an answer |
| `response_format` | Optional `{min_length, max_length, charset}` with charset `digits`, `alphanumeric` or `any`; the Engine refuses answers of another form |
| `image` | Optional `{content_type, data}`: PNG, JPEG, GIF or WebP, base64. Optional fields are omitted, not `null`. |

`job.resume` is `job.track` plus two required fields: `intervention_id` (what the worker
exchanges for the user's answer over the internal API) and `session_ref`. It keeps the
attempt number: resuming is not a retry. Neither message ever contains the answer or the session.

## Error classes

| Reported by workers | Retryable |
|---|---|
| `NETWORK_TIMEOUT`, `NETWORK_CONNECT`, `HTTP_5XX`, `RATE_LIMITED`, `ADAPTER_TIMEOUT`, `INTERNAL`, `SESSION_LOST` | yes |
| `BLOCKED`, `AUTH_INVALID`, `AUTH_EXPIRED`, `PARSE_ERROR`, `SCHEMA_DRIFT`, `UNKNOWN` | no |

The worker states `retryable` in the message; the Engine decides whether attempts are left.

Assigned only by the Engine, never accepted from a worker:

| Class | Meaning |
|---|---|
| `QUEUE_WAIT_EXCEEDED` | No worker started the attempt before its queue deadline |
| `WORKER_LOST` | An execution started but no result arrived in time |
| `DELIVERY_LIMIT_EXCEEDED` | RabbitMQ dead-lettered the job message after repeated failed deliveries |
| `INTERVENTION_EXPIRED` | Nobody answered a one-time-code or CAPTCHA challenge in time (job `EXPIRED`) |
| `CHALLENGE_LIMIT_EXCEEDED` | The provider kept asking; the per-job limit of challenges was reached |

## What happens to a bad message

| Situation | Result |
|---|---|
| Worker receives an invalid, oversized or foreign `job.track` or `job.resume` | Rejected → `adapter.<key>.jobs.dlq` → the Engine closes the attempt |
| Engine receives an invalid event or one for an unknown job | Rejected → `engine.events.dlq` (parked for operators) |
| Worker cannot publish a result | Job message stays unacknowledged, the worker reconnects, RabbitMQ redelivers; after the delivery limit the message is dead-lettered |

## Changing a contract

1. Change the schema and the fixtures in `courier-contracts/`.
2. Change the Rust types (`courier-engine/src/messaging/contracts.rs`) and the Python models
   (`courier-adapters/courier_adapter_sdk/contracts/`).
3. If a message the Engine sends changed, run `make update-contract-fixtures` and review the diff.
4. `make test` — both languages run the same fixtures.

Adding an optional field is a minor version. Removing or renaming a field, or changing its
meaning, needs a new major version with new routing keys, so old and new workers can coexist.
