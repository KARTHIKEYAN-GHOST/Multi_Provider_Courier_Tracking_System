# Phase 8 — Redis, sessions, the OTP / CAPTCHA workflow and the human interface

Some couriers put the tracking page behind a step only a person can pass: a one-time code
texted to the consignee, or a CAPTCHA. Phase 8 lets a lookup stop at that step, ask the right
user, and continue with their answer — without a worker ever waiting, and without the answer
or the session ever travelling in a message.

| Piece | Where |
|---|---|
| Schema | `courier-engine/migrations/0012_interventions.sql` |
| Challenge handling, answers, expiry | `courier-engine/src/services/interventions.rs` |
| Public API | `courier-engine/src/api/interventions.rs` (`/v1/interventions…`) |
| Internal API | `courier-engine/src/api/internal.rs` (`/internal/v1/interventions/resolve`) |
| Messages | `job.intervention_required`, `job.resume` (`courier-contracts/schemas/v1/`) |
| SDK: challenge API | `courier_adapter_sdk/interventions.py` (`otp_required`, `captcha_required`), `Adapter.resume` |
| SDK: session store | `courier_adapter_sdk/sessions.py` (Redis, encrypted) |
| SDK: worker | `courier_adapter_sdk/worker.py` (`_call_adapter`, `_pause`, `_publish_intervention`) |
| Practice target | `demo_site/challenges.py`; numbers `DEMO-OTP-…`, `DEMO-CAPTCHA-…` |
| Human interface | `courier-human-interface/` (FastAPI) |
| Infrastructure | Compose services `redis`, `human-interface` |

## The workflow

```
user            Engine                    RabbitMQ                 worker (demosite)        Redis
 │ POST /tracking-requests                                                                    
 │────────────▶ job QUEUED ──── job.track ───────────────────────▶ adapter.track()            
 │                                                                 site asks for a code       
 │                                                                 session ───────────────────▶ encrypted, TTL
 │              job WAITING_FOR_OTP ◀── job.intervention_required ─ (ack; worker is free)     
 │              human_interventions row (assigned to one user)                                
 │ GET /v1/interventions  → prompt (+ picture)                                                
 │ POST /v1/interventions/{id}/response {"response": "424242"}                                
 │────────────▶ answer stored encrypted, job QUEUED (same attempt)                            
 │                           ──── job.resume (intervention_id, session_ref) ─▶                
 │                                                                 load session ◀─────────────
 │              POST /internal/v1/interventions/resolve ◀───────── fetch the answer           
 │                                                                 adapter.resume(session, answer)
 │              job COMPLETED ◀──────── job.result ──────────────── (ack; session deleted) ───▶ del
 │              answer and picture wiped                                                      
```

Points that matter:

- **Nothing waits.** The worker saves the session, reports and acknowledges the job message.
  The answer can be continued by any worker of the same adapter, after any restart.
- **Same logical attempt.** Asking a person is not a failure, so it uses no retry. One attempt
  then has two (or more) physical executions: disposition `suspended` for the one that asked,
  `accepted` for the one that finished.
- **The answer never rides in a message.** `job.resume` carries only `intervention_id` and
  `session_ref`. The worker fetches the answer over the internal API with its worker token —
  the same rules as for credentials (right adapter, the job's current attempt, job running),
  every fetch written to `audit_log` as `intervention.resolve`. It can be fetched again if
  RabbitMQ redelivers the resume message.
- **Stored encrypted, deleted early.** The answer is sealed with the credential master key
  (AES-256-GCM envelope encryption, bound to the challenge and job), and deleted together with
  the CAPTCHA picture as soon as the challenge closes for any reason.
- **The session never reaches the Engine.** It stays in Redis, encrypted by the worker with
  `WORKER_SESSION_KEY`, bound to its reference, with an expiry. The Engine only stores the
  reference.

### Who is asked

The challenge is assigned to exactly one user: the one who started the lookup (first request or
manual refresh) if they are still subscribed; for an automatic poll, the shipment's
longest-standing subscriber. Everyone else gets 404, as for a challenge that does not exist.

### How it ends

| Situation | Result |
|---|---|
| Right answer | `job.result` closes the attempt as usual; challenge `completed` |
| Provider refuses the answer and asks again | Old challenge `rejected`, a new one for the same attempt |
| More than `providers.max_challenges` (3) challenges in one job | Job `FAILED`, `CHALLENGE_LIMIT_EXCEEDED` (not retried) |
| Nobody answers within the limit | Job `EXPIRED`, `INTERVENTION_EXPIRED`; the next poll or a manual refresh starts over |
| Session gone (Redis restarted, expired, site forgot it) | Worker reports `SESSION_LOST` (retryable): a new attempt runs and asks again |
| Answer of the wrong form (length, characters) | 422 at once; the challenge stays open. After 5 such answers it stops accepting any (410) |

The time to answer is the smaller of `providers.intervention_ttl_s` (600 s; 30–3600) and the
lifetime the adapter reports for the challenge. The reaper enforces it (`interventions::expire_due`).

## API

| Method & path | Behaviour |
|---|---|
| `GET /v1/interventions` | The caller's open challenges: `id`, `kind` (`otp` / `captcha`), `prompt`, `response_format`, `has_image`, `expires_at`, `tracking_request_id`, `provider_code`, `tracking_number` |
| `GET /v1/interventions/{id}` | One challenge, with `image` `{content_type, data}` (base64) for a CAPTCHA |
| `POST /v1/interventions/{id}/response` | Body `{"response": "…"}`. 202 answered · 404 not yours / unknown · 409 `already_answered` · 410 `intervention_expired` or `too_many_invalid_answers` · 422 wrong form · 503 no answer store (no master key) |

A waiting job shows as `WAITING_FOR_OTP` / `WAITING_FOR_CAPTCHA` in the tracking-request views.
The answer is write-only: no endpoint returns it.

## Messages

Two new message types (details in [contracts.md](contracts.md)):

- `job.intervention_required` (worker → Engine, routing key `job.intervention_required.v1`):
  execution identity, `kind`, `prompt`, `session_ref`, optional `expires_in_s`,
  `response_format` and `image` (PNG, JPEG, GIF or WebP only; SVG is refused).
- `job.resume` (Engine → worker, `job.resume.v1.<adapter_key>`): everything `job.track` has,
  plus `intervention_id` and `session_ref`.

New error classes: `SESSION_LOST` (worker-reported, retryable); `INTERVENTION_EXPIRED` and
`CHALLENGE_LIMIT_EXCEEDED` (assigned only by the Engine).

## Writing an adapter that can ask

```python
from courier_adapter_sdk import Adapter, otp_required, captcha_required

class MyAdapter(Adapter):
    async def track(self, request, ctx):
        page = await ...
        if page_asks_for_code(page):
            raise otp_required("Enter the 6-digit code sent to your phone.",
                               {"txn": txn_id, "cookies": cookies}, digits=6, expires_in_s=300)
        ...

    async def resume(self, request, ctx, session, response):
        page = await ...post(session["txn"], response)   # may raise a new challenge
        ...
```

Rules: the session is plain JSON data (at most 64 KiB); nothing may be kept on the adapter
between `track` and `resume`; the prompt is the adapter's own wording, never text copied from
the provider's page (untrusted input); the answer is used and not logged or stored; a CAPTCHA
picture is passed on unchanged and **never solved automatically**.

## Running it

`make up` starts everything, but the challenge workflow needs three secrets in `.env` first:

```
make master-key        → CREDENTIAL_MASTER_KEY=…   (if not set already: answers are stored with it)
make session-key       → WORKER_SESSION_KEY=…
make up                # then, once:
docker compose … exec engine courier-engine admin create-worker-token --adapter demosite
                       → DEMOSITE_WORKER_TOKEN=…   then: make up   (recreates the worker)
```

Without the session key or the token, ordinary demo-site lookups still work; only
`DEMO-OTP-` / `DEMO-CAPTCHA-` shipments fail (class `INTERNAL` or `AUTH_INVALID`).

Try it:

1. Create a tracking request for provider `demosite`, number `DEMO-OTP-1` (or `DEMO-CAPTCHA-1`).
2. Open the human interface, <http://localhost:8088>, and sign in with your API key.
3. For the one-time code, open the pretend phone inbox:
   <http://localhost:8090/inbox?number=DEMO-OTP-1>. For the CAPTCHA, read the picture.
4. Send the answer; the request completes with status `IN_TRANSIT`.

Host-side (without containers): `make run-demo-site`, a worker with `WORKER_SESSION_STORE_URL`,
`WORKER_SESSION_KEY`, `WORKER_ENGINE_INTERNAL_URL`, `WORKER_TOKEN` set, and `make run-human-interface`.

## The human interface

A deliberately small FastAPI site (`courier-human-interface/`): sign in, list of what is
waiting (reloads every 15 s), one page per challenge with the prompt, the picture and an input.

- It is a **thin client of the Engine's public API**. No database, RabbitMQ, Redis or internal
  API access, and no secrets of its own. It can only show what the signed-in user's API key
  can read, so it cannot leak another user's challenge even if it has a bug.
- Sign-in is the user's API key, checked by using it against the Engine. The browser gets a
  random session cookie (`HttpOnly`, `SameSite=Strict`); the key stays in the server's memory.
  A restart signs everyone out.
- Every form carries an anti-forgery token. The site sends a Content-Security-Policy that
  allows no scripts at all; pictures only as embedded data; all text is escaped; only PNG,
  JPEG, GIF and WebP are ever displayed.
- The answer is forwarded and forgotten: not logged, not echoed back.
- Set `HUMAN_INTERFACE_COOKIE_SECURE=true` wherever it is served over HTTPS. Put it behind TLS
  before exposing it beyond localhost — API keys are typed into it.

## Redis

Used only by adapter workers, only for sessions (key `courier:session:<adapter>:<session_ref>`).
The Compose service keeps nothing on disk and requires a password; the host port is 56379
(6379 is often taken by a local Redis). Losing Redis loses only waiting sessions: affected
lookups report `SESSION_LOST`, restart, and ask again.

Every worker of one adapter must share the same `WORKER_SESSION_KEY`. Changing the key makes
stored sessions unreadable, which is handled the same way as losing them.

## Deviations from the architecture document

| Architecture (sections 8–9) | Built | Why |
|---|---|---|
| Separate queue `adapter.<key>.resume` | `job.resume.v1.<key>` is bound to the adapter's existing `adapter.<key>.jobs` queue | One consumer, one delivery limit, one dead-letter path; the existing dead-letter handler closes a poisoned resume like any job message. A separate queue adds nothing until resumes need their own priority. |
| Pinned sessions (a live browser page in one worker), worker heartbeats, private resume queues | Not built; portable sessions only | No browser adapter exists yet. Building affinity without a user would be untested code. It belongs with the first Playwright adapter. |
| Single-use hashed token link for answering | The user's API key (and the human interface's sign-in on top of it) | The system has no way to send a link to a user yet (no e-mail or notification channel). Reusing API-key authentication keeps one access-control path. |
| `response_ref` in `job.resume` | `intervention_id` | The same thing: an opaque ID valid only for that challenge and attempt. |
| Wrong answers limited by `max_attempts` | Own limit `providers.max_challenges` | A challenge is not a failed attempt; mixing the two would let CAPTCHAs eat the retry budget. |

## Not built yet

- Telling the user that something is waiting (e-mail, push, webhook). Today they see it in the
  human interface or by calling `GET /v1/interventions`.
- Pinned sessions (see above).
- Rotation tooling for `WORKER_SESSION_KEY` and the master key.

## Tests

| Suite | What Phase 8 added |
|---|---|
| Contracts | schemas, 5 valid + 4 invalid fixtures, routing and bindings for the two messages |
| Engine (`tests/interventions.rs`) | challenge creation and assignment, answer validation and encryption, resume, rejection, limits, expiry, resolve rules, idempotency |
| Adapters | `test_sessions.py` (encryption, binding, expiry, real Redis), `test_worker_interventions.py` (pause, resume, redelivery, lost session, no leaks), demo-site verification tests |
| Human interface | `tests/test_app.py`: sign-in, isolation between users, forgery guard, escaping, picture filtering, headers |
| End to end (`test_interventions.py`) | OTP and CAPTCHA through real processes and Redis, wrong answer, limit, expiry, lost session, access control, the human interface |
