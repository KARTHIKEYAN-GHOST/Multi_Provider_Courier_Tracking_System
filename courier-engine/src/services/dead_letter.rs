//! Handling of `job.track` messages that RabbitMQ dead-lettered (the worker died or
//! rejected the message on every delivery).
//!
//! The dead-lettered message still contains the envelope, so the job and attempt are known
//! directly. The attempt is closed through the same guarded `close_attempt` as everywhere
//! else, with the dead-letter counter incremented inside that statement.

use serde_json::Value;
use sqlx::PgPool;
use uuid::Uuid;

use super::close_attempt::{ClosedBy, Closure, RetryPolicy, close_attempt};
use super::events::record_in_inbox;
use crate::domain::ErrorClass;
use crate::messaging::contracts::parse_job_track;

/// Inbox name of the dead-letter consumer.
pub const CONSUMER: &str = "engine.dead-letter";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeadLetterHandled {
    /// The dead-letter belonged to the job's current open attempt and closed it.
    Applied,
    /// Recorded for operators, but the attempt was already closed (or a newer one is
    /// running, or the message was unreadable). No job state changed.
    RecordedOnly,
    /// This dead-letter was handled before.
    Duplicate,
}

/// Processes one dead-lettered message. Always ends with the message recorded in
/// `dead_letters`, so the DLQ can be emptied while the database keeps the evidence.
pub async fn handle_dead_letter(
    pool: &PgPool,
    body: &[u8],
    source_queue: &str,
    reason: Option<&str>,
    headers: &Value,
    policy: RetryPolicy,
) -> Result<DeadLetterHandled, sqlx::Error> {
    let mut tx = pool.begin().await?;

    let envelope = match parse_job_track(body) {
        Ok(envelope) => envelope,
        Err(invalid) => {
            // Not a readable job message: keep what we can for an operator.
            let payload = serde_json::from_slice::<Value>(body)
                .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(body).into_owned()));
            insert_record(
                &mut tx,
                None,
                source_queue,
                Some(&invalid.0),
                None,
                None,
                false,
                &payload,
                headers,
            )
            .await?;
            tx.commit().await?;
            return Ok(DeadLetterHandled::RecordedOnly);
        }
    };
    let (job_id, attempt) = (envelope.payload.job_id, envelope.payload.attempt);

    if !record_in_inbox(&mut tx, CONSUMER, envelope.message_id).await? {
        return Ok(DeadLetterHandled::Duplicate);
    }

    // Take the job lock first, like every other handler. A dead-letter for a job that does
    // not exist simply matches nothing below.
    sqlx::query("SELECT 1 FROM tracking_jobs WHERE id = $1 FOR UPDATE")
        .bind(job_id)
        .execute(&mut *tx)
        .await?;

    // Guarded by the message's own attempt number and by the open status. A dead-letter for
    // an attempt that a result or the reaper already closed, or for an old attempt while a
    // newer one runs, matches no row: counter, status and the running attempt are untouched.
    let closed = close_attempt(
        &mut tx,
        job_id,
        attempt,
        &Closure::Failed {
            class: ErrorClass::DeliveryLimitExceeded,
            message: "the job message was dead-lettered before any worker completed it",
            retryable: true,
            retry_after_ms: None,
            closed_by: ClosedBy::DeadLetter,
            execution_id: None,
        },
        policy,
    )
    .await?;

    let payload = serde_json::to_value(&envelope).unwrap_or(Value::Null);
    insert_record(
        &mut tx,
        Some(envelope.message_id),
        source_queue,
        reason,
        Some(job_id),
        Some(attempt),
        closed.is_some(),
        &payload,
        headers,
    )
    .await?;
    tx.commit().await?;

    Ok(if closed.is_some() {
        DeadLetterHandled::Applied
    } else {
        DeadLetterHandled::RecordedOnly
    })
}

#[allow(clippy::too_many_arguments)]
async fn insert_record(
    conn: &mut sqlx::PgConnection,
    message_id: Option<Uuid>,
    source_queue: &str,
    reason: Option<&str>,
    job_id: Option<Uuid>,
    attempt: Option<i32>,
    applied: bool,
    payload: &Value,
    headers: &Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO dead_letters (id, message_id, source_queue, reason, job_id, attempt, applied, payload, headers)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
    )
    .bind(Uuid::now_v7())
    .bind(message_id)
    .bind(source_queue)
    .bind(reason)
    .bind(job_id)
    .bind(attempt)
    .bind(applied)
    .bind(payload)
    .bind(headers)
    .execute(conn)
    .await?;
    Ok(())
}
