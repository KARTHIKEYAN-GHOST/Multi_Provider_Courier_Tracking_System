# Phase 6 — Credentials, first real adapters, rate limiting

What Phase 6 added, how to operate it, and where it deviates from `docs/architecture.md`.

## What exists now

| Piece | Where |
|---|---|
| Encrypted credential storage | `courier-engine/src/services/{crypto,credentials}.rs`, table `provider_credentials` |
| Internal resolve API for workers | `courier-engine/src/api/internal.rs`, separate listener `ENGINE_INTERNAL_ADDR` |
| Worker tokens | `courier-engine/src/services/worker_tokens.rs`, table `worker_tokens` |
| Audit log (append-only) | table `audit_log` |
| Provider rate limiting | `db::providers::reserve_dispatch_slot`, column `providers.min_dispatch_interval_ms` |
| Dispatch rules | `DispatchTarget::blocked_reason` (credential required, scraping authorized) |
| Test-provider switch | `ENABLE_DUMMY_PROVIDER`, column `providers.is_test` |
| SDK: credentials and HTTP | `courier_adapter_sdk/{credentials,http}.py`, `JobContext.credentials()` / `.http()` |
| DHL adapter (official API) | `courier-adapters/adapters/dhl/` |
| Scraping adapter + demo courier website | `courier-adapters/adapters/demosite/`, `courier-adapters/demo_site/` |

## How a credential travels

1. An operator stores the secret: `admin set-credential --provider dhl` (JSON on standard input).
   The Engine encrypts it and keeps only ciphertext.
2. When a job for that provider is dispatched, `job.track` carries `credential_ref` — the ID of
   the credential row. It is not a secret and is useless on its own.
3. The worker calls `POST /internal/v1/credentials/resolve` on the Engine's internal listener
   with `{credential_ref, job_id, attempt}` and its worker token.
4. The Engine returns the secret only if **all** of these hold, and writes an audit row either way:
   - the token is valid, not revoked, and belongs to the adapter that serves the job;
   - the job exists, `attempt` is its current attempt, and it is `QUEUED` or `PROCESSING`;
   - the reference names a credential of the job's provider;
   - the provider has a credential in use (the one returned is the *current* one, so a job
     dispatched before a rotation gets the new secret).
5. The adapter uses the secret for the duration of one job. It is never logged (the `Secret`
   type prints only its field names), never cached and never put into a message.

Every refusal answers `403 resolve_denied` with the same body; the reason is only in the audit log.

### Encryption

Envelope encryption with AES-256-GCM. Each credential has its own random data key, which
encrypts the secret; the data key is encrypted ("wrapped") by the master key from
`CREDENTIAL_MASTER_KEY`. Both encryptions are bound to the credential's ID and provider ID, so
ciphertext copied into another row cannot be decrypted. The master key exists only in the
Engine's environment; workers, RabbitMQ and the database never see it.

**Losing the master key makes stored credentials unreadable.** Back it up separately from the
database. `key_version` is stored with every credential; a tool that re-wraps data keys under a
new master key is not built yet.

## Operating it

```
make master-key                      # prints a key; put it in .env as CREDENTIAL_MASTER_KEY
cd courier-engine
echo '{"api_key":"<your DHL key>"}' | cargo run -- admin set-credential --provider dhl --label "my key"
cargo run -- admin list-credentials
cargo run -- admin create-worker-token --adapter dhl --name "dhl worker 1"   # printed once
cargo run -- admin revoke-worker-token --prefix <12 hex characters>
cargo run -- admin revoke-credential --provider dhl
cargo run -- admin set-scraping-authorized --provider <code> --authorized true|false
```

With containers, prefix the admin commands with
`docker compose ... exec -T engine courier-engine` instead of `cargo run --`, put the worker
token in `.env` as `DHL_WORKER_TOKEN`, and start the DHL worker with `make up-dhl`.

Typing the key inside `echo '...'` leaves it in the shell history; reading it from a file
(`< dhl.json`) that is deleted afterwards avoids that.

### Settings

| Setting | Meaning |
|---|---|
| `CREDENTIAL_MASTER_KEY` | 64 hex characters. Unset: no credential store, providers needing one are unavailable. |
| `CREDENTIAL_KEY_VERSION` | Number stored with each credential (default 1) |
| `ENGINE_INTERNAL_ADDR` | Address of the internal API. Unset: not started. Must not be reachable from outside. |
| `ENABLE_DUMMY_PROVIDER` | `true` (default): test providers `dummy` and `demosite` are offered. `false`: disabled at start-up. |
| `WORKER_ENGINE_INTERNAL_URL`, `WORKER_TOKEN` | Worker side of the internal API |
| `DHL_BASE_URL` | DHL API base, default `https://api-eu.dhl.com` |
| `DEMOSITE_BASE_URL` | Where the demo-site adapter finds the demo website |

## Dispatch rules

A tracking request (or refresh) is refused with `409 provider_unavailable` when

- the provider has `requires_credential` and no credential is stored, or
- the provider's `tracking_method` is `scrape` or `browser` and `scraping_authorized` is false.

Nothing is written in that case. `scraping_authorized` is set only by an operator
(`admin set-scraping-authorized`, audited) and records that the site owner permits automated
access. The only provider with it set is `demosite`, the website shipped with this project.
**No real courier website is contacted by any code in this repository.**

## Rate limiting

`providers.min_dispatch_interval_ms` is the minimum time between two jobs sent to a provider
(0 = unlimited; `dhl` is seeded with 5000 ms). At dispatch the Engine reserves the next free
slot in the provider row (`next_dispatch_at`), inside the dispatch transaction; the row lock
makes concurrent dispatches take consecutive slots. The outbox message becomes publishable at
its slot, and the queue-wait deadline starts at the slot, so waiting for a slot is not counted
as waiting for a worker. Retries reserve a slot the same way. A provider's own `Retry-After`
(HTTP 429) is honoured through the existing retry delay.

Limits: the spacing applies to dispatch, not to the moment the worker makes the call, so a
backlog in the queue can bunch calls; it is a floor on average rate, not a hard guarantee.
A burst of N jobs waits N × interval; there is no cap on how far ahead slots are reserved.

## The adapters

**DHL** (`adapters/dhl`). `GET {DHL_BASE_URL}/track/shipments?trackingNumber=…` with header
`DHL-API-Key`. `raw_status` is DHL's `statusCode` (`pre-transit`, `transit`, `delivered`,
`failure`, `unknown`), mapped by the seeded status mappings. 404 → `NOT_FOUND`.
*The response format was written from DHL's published API description and tested against
hand-written fixtures and a fake DHL server, never against the real API.* Before relying on it,
run one real shipment with your own key and compare. Timestamps DHL sends without a UTC offset
are taken as UTC.

**Demo site** (`adapters/demosite`). Fetches `/track?number=…` and parses the history table
with BeautifulSoup. Fetching and parsing are separate modules; the parser is tested against
saved pages. The demo website (`demo_site/`) answers by tracking-number prefix:
`DEMO-OK-`, `DEMO-TRANSIT-`, `DEMO-BOOKED-`, `DEMO-BUSY-` (503), `DEMO-LIMIT-` (429 with
Retry-After), `DEMO-BLOCKED-` (403), `DEMO-NEWLAYOUT-` (redesigned page → `SCHEMA_DRIFT`),
`DEMO-BADDATE-` (→ `PARSE_ERROR`), anything else → "not found" page.

Scraped pages and API answers are untrusted: they are parsed as data, their text is never
copied into error messages, and the raw body is stored only for operators.

## Deviations and open points

- **Resolve is allowed while the job is `QUEUED` or `PROCESSING`**, not `PROCESSING` only: the
  worker's `job.started` may not have been processed yet when it asks.
- **`schema_version` stays `1.0`.** `credential_ref` is optional and old readers ignore unknown fields.
- **No external secrets manager.** `SecretStore` is a trait with one implementation (`PgSecretStore`).
- **Master-key rotation tooling is not built** (see above).
- **The internal API is plain HTTP**, protected by network placement and the worker token. Use
  TLS or a private network when Engine and workers run on different hosts.
- **Artifact storage for large bodies** (architecture, "Phase 6+") is not built; the 256 KiB limit stands.
- The DHL worker, the Docker images and `make up` / `make smoke` have not been run by the author.
