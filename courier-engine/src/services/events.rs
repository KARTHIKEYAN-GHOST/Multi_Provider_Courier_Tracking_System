//! Handling of worker events (`job.started`, `job.result`) consumed from `engine.events`.
//!
//! Delivery is at-least-once and unordered, so every handler is idempotent and makes no
//! assumption about which message arrives first:
//!
//! - the inbox (`processed_messages`) stops the same message from being applied twice;
//! - both handlers upsert the execution log on `execution_id`, so `job.result` may arrive
//!   before, after, or without `job.started`;
//! - the attempt is closed through the guarded `close_attempt`, so the first result wins.
//!
//! Everything a message causes is written in one transaction together with its inbox row.
//! The caller acknowledges the message only after that transaction has committed.

use sha2::{Digest, Sha256};
use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use super::close_attempt::{ClosedBy, Closure, RetryPolicy, close_attempt};
use crate::domain::{ErrorClass, JobOutcome};
use crate::messaging::contracts::{
    Envelope, IncomingEvent, JobResult, JobStarted, ProviderEvent, parse_incoming_event,
};

/// Inbox name of the event consumer.
pub const CONSUMER: &str = "engine.events";

/// What the consumer should do with the delivery.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Handled {
    /// Applied (or deliberately ignored); acknowledge.
    Processed,
    /// This exact message was applied before; acknowledge.
    Duplicate,
    /// The message can never be processed (malformed, unsupported version, unknown job).
    /// Reject without requeue so RabbitMQ parks it in `engine.events.dlq`.
    Rejected(String),
}

/// Processes one message body from `engine.events`.
///
/// `Err` means a transient problem (database unavailable, deadlock): nothing was committed
/// and the delivery should be requeued.
pub async fn handle_event(
    pool: &PgPool,
    body: &[u8],
    policy: RetryPolicy,
) -> Result<Handled, sqlx::Error> {
    let event = match parse_incoming_event(body) {
        Ok(event) => event,
        Err(invalid) => return Ok(Handled::Rejected(invalid.0)),
    };

    let mut tx = pool.begin().await?;
    if !record_in_inbox(&mut tx, CONSUMER, event.message_id()).await? {
        return Ok(Handled::Duplicate);
    }
    let handled = match &event {
        IncomingEvent::Started(envelope) => on_started(&mut tx, envelope).await?,
        IncomingEvent::Result(envelope) => on_result(&mut tx, envelope, policy).await?,
    };
    match handled {
        // A rejected message leaves no trace, not even its inbox row.
        Handled::Rejected(_) => tx.rollback().await?,
        _ => tx.commit().await?,
    }
    Ok(handled)
}

/// Inserts the inbox row. Returns false if this consumer has already processed the message.
pub(crate) async fn record_in_inbox(
    conn: &mut PgConnection,
    consumer: &str,
    message_id: Uuid,
) -> Result<bool, sqlx::Error> {
    let inserted = sqlx::query(
        "INSERT INTO processed_messages (consumer, message_id) VALUES ($1, $2)
         ON CONFLICT (consumer, message_id) DO NOTHING",
    )
    .bind(consumer)
    .bind(message_id)
    .execute(conn)
    .await?;
    Ok(inserted.rows_affected() == 1)
}

/// Locks the job row and confirms the attempt exists. Every handler takes this lock first,
/// so all changes to one job are applied one after another.
pub(crate) async fn lock_job_attempt(
    conn: &mut PgConnection,
    job_id: Uuid,
    attempt: i32,
) -> Result<bool, sqlx::Error> {
    let found = sqlx::query_scalar::<_, Uuid>(
        "SELECT j.id FROM tracking_jobs j
         JOIN job_attempts a ON a.job_id = j.id AND a.attempt = $2
         WHERE j.id = $1
         FOR UPDATE OF j",
    )
    .bind(job_id)
    .bind(attempt)
    .fetch_optional(conn)
    .await?;
    Ok(found.is_some())
}

async fn count_execution(
    conn: &mut PgConnection,
    job_id: Uuid,
    attempt: i32,
) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE job_attempts SET execution_count = execution_count + 1 WHERE job_id = $1 AND attempt = $2")
        .bind(job_id)
        .bind(attempt)
        .execute(conn)
        .await?;
    Ok(())
}

// ----------------------------------------------------------------------------- job.started

/// A worker began executing an attempt.
///
/// On a known execution (its result got here first) this can only fill in a missing
/// `started_at`. It never touches `finished_at`, `outcome` or `disposition`, and its job
/// transition is guarded, so a late `job.started` cannot reopen or relabel anything.
async fn on_started(
    conn: &mut PgConnection,
    envelope: &Envelope<JobStarted>,
) -> Result<Handled, sqlx::Error> {
    let p = &envelope.payload;
    if !lock_job_attempt(conn, p.job_id, p.attempt).await? {
        return Ok(Handled::Rejected("unknown job or attempt".into()));
    }

    // `xmax = 0` is true only for a row this statement inserted (not one it updated).
    let inserted: bool = sqlx::query_scalar(
        "INSERT INTO adapter_execution_logs
             (execution_id, job_id, attempt, worker_id, adapter_version, delivery_count, started_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7)
         ON CONFLICT (execution_id) DO UPDATE
             SET started_at = COALESCE(adapter_execution_logs.started_at, EXCLUDED.started_at)
         RETURNING (xmax = 0)",
    )
    .bind(p.execution_id)
    .bind(p.job_id)
    .bind(p.attempt)
    .bind(&p.worker_id)
    .bind(&p.adapter_version)
    .bind(p.delivery_count)
    .bind(p.started_at)
    .fetch_one(&mut *conn)
    .await?;

    if inserted {
        count_execution(conn, p.job_id, p.attempt).await?;
        // QUEUED → PROCESSING, and start the execution recovery deadline: the Engine now
        // knows an execution exists and how long it may take (timeout + grace). A redelivery
        // of the same open attempt is a new execution and moves the deadline forward.
        sqlx::query(
            "UPDATE tracking_jobs j SET
                 status = 'PROCESSING',
                 execution_deadline_at = clock_timestamp()
                     + (p.timeout_ms + p.result_grace_ms) * interval '1 millisecond',
                 updated_at = clock_timestamp()
             FROM shipments s
             JOIN providers p ON p.id = s.provider_id
             WHERE j.id = $1 AND j.attempt = $2 AND j.status IN ('QUEUED', 'PROCESSING')
               AND s.id = j.shipment_id",
        )
        .bind(p.job_id)
        .bind(p.attempt)
        .execute(&mut *conn)
        .await?;
    }
    Ok(Handled::Processed)
}

// ----------------------------------------------------------------------------- job.result

/// A worker finished one execution. Works whether or not `job.started` was seen.
async fn on_result(
    conn: &mut PgConnection,
    envelope: &Envelope<JobResult>,
    policy: RetryPolicy,
) -> Result<Handled, sqlx::Error> {
    let p = &envelope.payload;

    // Lock order is shipment → job everywhere a shipment is written (the API's dispatch
    // path does the same), so the two can never deadlock each other.
    let Some(shipment_id) =
        sqlx::query_scalar::<_, Uuid>("SELECT shipment_id FROM tracking_jobs WHERE id = $1")
            .bind(p.job_id)
            .fetch_optional(&mut *conn)
            .await?
    else {
        return Ok(Handled::Rejected("unknown job".into()));
    };
    let provider_id: Uuid =
        sqlx::query_scalar("SELECT provider_id FROM shipments WHERE id = $1 FOR UPDATE")
            .bind(shipment_id)
            .fetch_one(&mut *conn)
            .await?;
    if !lock_job_attempt(conn, p.job_id, p.attempt).await? {
        return Ok(Handled::Rejected("unknown attempt".into()));
    }

    let (error_class, error_message) = match &p.error {
        Some(e) => (Some(e.class), Some(e.message.as_str())),
        None => (None, None),
    };
    // Creates the execution row if `job.started` has not been processed, completes it if it
    // has. An execution that already has a result is left exactly as it is.
    let upserted: Option<bool> = sqlx::query_scalar(
        "INSERT INTO adapter_execution_logs
             (execution_id, job_id, attempt, worker_id, adapter_version, delivery_count,
              started_at, finished_at, outcome, error_class, error_message, metrics)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
         ON CONFLICT (execution_id) DO UPDATE SET
             finished_at = EXCLUDED.finished_at,
             outcome = EXCLUDED.outcome,
             error_class = EXCLUDED.error_class,
             error_message = EXCLUDED.error_message,
             metrics = EXCLUDED.metrics,
             started_at = COALESCE(adapter_execution_logs.started_at, EXCLUDED.started_at)
         WHERE adapter_execution_logs.finished_at IS NULL
         RETURNING (xmax = 0)",
    )
    .bind(p.execution_id)
    .bind(p.job_id)
    .bind(p.attempt)
    .bind(&p.worker_id)
    .bind(&p.adapter_version)
    .bind(p.delivery_count)
    .bind(p.started_at)
    .bind(p.finished_at)
    .bind(p.outcome)
    .bind(error_class)
    .bind(error_message)
    .bind(&p.metrics)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(inserted) = upserted else {
        // A second result for an execution that already reported one (same execution_id,
        // different message). The first one stands.
        return Ok(Handled::Processed);
    };
    if inserted {
        count_execution(conn, p.job_id, p.attempt).await?;
    }

    let closure = match (&p.outcome, &p.error) {
        (JobOutcome::Failure, Some(error)) => Closure::Failed {
            class: error.class,
            message: &error.message,
            retryable: error.retryable,
            retry_after_ms: error.retry_after_ms,
            closed_by: ClosedBy::Result,
            execution_id: Some(p.execution_id),
        },
        (JobOutcome::Failure, None) => Closure::Failed {
            class: ErrorClass::Unknown,
            message: "failure without error details",
            retryable: false,
            retry_after_ms: None,
            closed_by: ClosedBy::Result,
            execution_id: Some(p.execution_id),
        },
        (outcome, _) => Closure::Answered {
            outcome: *outcome,
            execution_id: p.execution_id,
        },
    };
    let closed = close_attempt(conn, p.job_id, p.attempt, &closure, policy).await?;

    let disposition = if closed.is_some() {
        "accepted"
    } else {
        // The attempt was closed before this result arrived. By another execution's result:
        // `duplicate`. By the reaper or the dead-letter handler: `stale`.
        let closed_by: Option<String> = sqlx::query_scalar(
            "SELECT closed_by::text FROM job_attempts WHERE job_id = $1 AND attempt = $2",
        )
        .bind(p.job_id)
        .bind(p.attempt)
        .fetch_one(&mut *conn)
        .await?;
        if closed_by.as_deref() == Some("result") {
            "duplicate"
        } else {
            "stale"
        }
    };
    // A disposition moves from `incomplete` to its final value once and never again.
    sqlx::query(
        "UPDATE adapter_execution_logs SET disposition = $2::text::execution_disposition
         WHERE execution_id = $1 AND disposition = 'incomplete'",
    )
    .bind(p.execution_id)
    .bind(disposition)
    .execute(&mut *conn)
    .await?;

    // Only the accepted execution's data is stored.
    if closed.is_some() && p.outcome != JobOutcome::Failure {
        store_answer(conn, shipment_id, provider_id, p).await?;
    }
    Ok(Handled::Processed)
}

/// Stores the raw response and the events of the accepted result, then recomputes the
/// shipment's status from its event history.
async fn store_answer(
    conn: &mut PgConnection,
    shipment_id: Uuid,
    provider_id: Uuid,
    p: &JobResult,
) -> Result<(), sqlx::Error> {
    let mut raw_response_id = None;
    if let Some(raw) = &p.raw_response {
        let id = Uuid::now_v7();
        let canonical = serde_json::to_vec(&raw.body).unwrap_or_default();
        sqlx::query(
            "INSERT INTO raw_tracking_responses
                 (id, shipment_id, job_id, attempt, execution_id, content_type, body, sha256)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        )
        .bind(id)
        .bind(shipment_id)
        .bind(p.job_id)
        .bind(p.attempt)
        .bind(p.execution_id)
        .bind(&raw.content_type)
        .bind(&raw.body)
        .bind(Sha256::digest(&canonical).as_slice())
        .execute(&mut *conn)
        .await?;
        raw_response_id = Some(id);
    }

    for event in &p.events {
        // The provider's wording is kept as-is; the normalized status comes from the
        // Engine's own mapping table. An unmapped status is stored as UNKNOWN, never guessed.
        sqlx::query(
            "INSERT INTO tracking_events
                 (id, shipment_id, raw_response_id, raw_status, raw_description, normalized_status,
                  occurred_at, location, dedupe_key)
             VALUES ($1, $2, $3, $4, $5,
                     COALESCE((SELECT m.normalized_status FROM provider_status_mappings m
                               WHERE m.provider_id = $6 AND m.raw_status = $4), 'UNKNOWN'),
                     $7, $8, $9)
             ON CONFLICT (shipment_id, dedupe_key) DO NOTHING",
        )
        .bind(Uuid::now_v7())
        .bind(shipment_id)
        .bind(raw_response_id)
        .bind(&event.raw_status)
        .bind(&event.raw_description)
        .bind(provider_id)
        .bind(event.occurred_at)
        .bind(&event.location)
        .bind(dedupe_key(event))
        .execute(&mut *conn)
        .await?;
    }

    // The shipment status is that of the most recent event with a known status. This is the
    // only place it changes: a refresh of a DELIVERED shipment leaves it DELIVERED until a
    // new result is processed here.
    sqlx::query(
        "UPDATE shipments s SET
             current_status = COALESCE(latest.normalized_status, s.current_status),
             last_event_at = COALESCE(newest.occurred_at, s.last_event_at),
             terminal_at = CASE
                 WHEN COALESCE(latest.normalized_status, s.current_status) IN ('DELIVERED', 'RETURNED')
                     THEN COALESCE(s.terminal_at, clock_timestamp())
                 END,
             poll_count = s.poll_count + 1,
             updated_at = clock_timestamp()
         FROM (SELECT 1) one
         LEFT JOIN LATERAL (
             SELECT e.normalized_status FROM tracking_events e
             WHERE e.shipment_id = $1 AND e.normalized_status <> 'UNKNOWN'
             ORDER BY e.occurred_at DESC, e.id DESC LIMIT 1
         ) latest ON true
         LEFT JOIN LATERAL (
             SELECT max(e.occurred_at) AS occurred_at FROM tracking_events e WHERE e.shipment_id = $1
         ) newest ON true
         WHERE s.id = $1",
    )
    .bind(shipment_id)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// Identifies an event within a shipment, so the same event reported by a later lookup is
/// not stored twice.
fn dedupe_key(event: &ProviderEvent) -> String {
    let mut hasher = Sha256::new();
    hasher.update(event.raw_status.as_bytes());
    hasher.update([0x1f]);
    hasher.update(event.occurred_at.timestamp_micros().to_be_bytes());
    hex::encode(hasher.finalize())
}
