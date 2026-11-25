# Phase 7 — Re-polling, status-mapping management, archiving and retention

What Phase 7 added and how to operate it. No message contract changed; adapters are untouched.

| Piece | Where |
|---|---|
| Polling scheduler | `courier-engine/src/services/polling.rs` |
| Status-mapping management | `courier-engine/src/services/mappings.rs` |
| Archiving and retention | `courier-engine/src/services/retention.rs` |
| Schema | `migrations/0011_polling_mappings_retention.sql` |
| Background tasks | `services/background.rs` (`run_poll_scheduler`, `run_maintenance_loop`) |

## Automatic re-polling

Tracking is not one-shot: the Engine looks a shipment up again until it is `DELIVERED` or
`RETURNED`. A poll is an ordinary job with trigger `poll` — retries, the three timers, rate
limiting, credentials and dead-lettering apply unchanged.

Policy, per provider:

| Column | Meaning |
|---|---|
| `poll_interval_ms` | Time until the next lookup. **0 = never polled** (the default). |
| `poll_max_interval_ms` | The interval doubles after each lookup that brought no new event, up to this value. A lookup with a new event resets it. |
| `poll_give_up_days` | Polling stops when a shipment has shown nothing new for this many days (default 30). |

Seeded: `dhl` 30 minutes, backing off to 6 hours. `dummy` and `demosite` are not polled
unless a policy is set: `admin set-poll-policy --provider <code> --interval-ms <n>
[--max-interval-ms <n>] [--give-up-days <n>]` (audited; open shipments are rescheduled at once).

How it works:

- `shipments.next_poll_at` is the time of the next lookup; NULL = not scheduled. The API shows
  it as `next_check_at`.
- It is set whenever a job is created (first request, manual refresh, poll), so the cadence
  is counted from the latest lookup of any kind — a manual refresh postpones the next poll.
- Every `POLL_SCHEDULER_INTERVAL_MS` the scheduler locks due shipments (`FOR UPDATE SKIP
  LOCKED`) and, in the same transaction, creates their jobs through the same function the API
  uses. Several Engine instances share the work; a shipment never has two lookups at once.
- Polling a shipment **stops** when it is finished, when nobody is subscribed any more, or
  after `poll_give_up_days` without news. It **resumes** when a new user subscribes or
  someone refreshes manually.
- Polling **pauses** while the provider is disabled, has no credential, or lost its scraping
  authorization, and continues by itself afterwards.
- A failed poll job (after its own retries) does not change the back-off; the next poll comes
  at the scheduled time.

## Status-mapping management

Adapters report the provider's wording; `provider_status_mappings` turns it into a normalized
status; an unmapped wording is stored as `UNKNOWN` and does not move the shipment's status.

```
cargo run -- admin list-unmapped-statuses [--provider dhl]     # what needs attention, most frequent first
cargo run -- admin set-status-mapping --provider dhl --raw-status "held" --status EXCEPTION
cargo run -- admin remove-status-mapping --provider dhl --raw-status "held"
cargo run -- admin list-status-mappings [--provider dhl]
```

A change is applied to the past as well, in one transaction: every stored event of that
provider with that wording is re-normalized and the status of every affected shipment is
recomputed from its event history. A shipment that becomes finished leaves the polling
schedule; one that turns out not to be finished (a wrong mapping corrected) returns to it.
Archived shipments are closed and are not rewritten. Every change is written to the audit log
with the value it replaced.

`--raw-status` is matched exactly (case-sensitive), per provider. A wording mapped to
`UNKNOWN` on purpose is no longer listed as unmapped. The descriptions shown by
`list-unmapped-statuses` are provider text: displayed as data, control characters removed.

Limit: one mapping change locks and updates all affected shipments in a single transaction —
fine for thousands, slow for millions.

## Archiving and retention

Runs every `MAINTENANCE_INTERVAL_MS` (default 5 minutes) and on demand with
`admin run-maintenance` (`make maintenance`), which prints what it did.

1. **Archive.** A shipment is one generation of a tracking number. It is closed
   (`archived_at`) when it has been finished for longer than the provider's
   `reuse_window_days` (default 90), or when it has had no subscriber and no new event for
   that long (counted from the later of: created, last new event, last subscriber left).
   Never while a lookup is running. Afterwards a request for the same number creates a new
   shipment; subscribers of the old one still see it, with `archived_at` set, and cannot
   refresh it (`409 shipment_archived`).
2. **Purge.** `RETENTION_DAYS` (default 180; 0 = never) after archiving, the shipment's
   `tracking_events` and `raw_tracking_responses` are deleted and `purged_at` is set. The
   shipment row, jobs, attempts, execution logs and subscriptions remain.
3. **Messages.** Sent outbox rows older than `OUTBOX_RETENTION_DAYS` (7) and inbox rows older
   than `INBOX_RETENTION_DAYS` (30) are deleted. Pending outbox rows are never deleted.

All steps work in batches, skip locked rows and are safe to run from several instances.
**Purging is irreversible**; set `RETENTION_DAYS=0` to keep everything.

## API changes

`GET /v1/tracking-requests[/{id}]` gained two fields: `next_check_at` and `archived_at`.
Nothing was removed.

## Not built

- No HTTP endpoint for mapping management: it is an operator task, done through the CLI.
  (There is still no operator role in the HTTP API.)
- `audit_log`, `dead_letters`, jobs and execution logs are not subject to retention.
- Poll intervals are per provider, not per shipment status (for example, no faster polling
  while "out for delivery").
- Mapping proposals from the AI service (Phase 9) will use the same `set_mapping` with another `source`.
