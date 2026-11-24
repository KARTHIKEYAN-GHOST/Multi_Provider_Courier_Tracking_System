//! The single place where a logical attempt is closed.
//!
//! Three actors can close an attempt: the result handler, the dead-letter handler and the
//! reaper. All of them call [`close_attempt`], whose core is one guarded UPDATE:
//!
//! ```sql
//! UPDATE tracking_jobs ... WHERE id = $job AND attempt = $attempt AND status IN ('QUEUED','PROCESSING')
//! ```
//!
//! Whichever actor commits first wins. For the others the UPDATE matches no row, they get
//! `None` back, and they change nothing. The decision "retry or fail" and the dead-letter
//! counter live inside that same statement, so neither can ever move without the attempt
//! also being closed. Only the retry scheduler opens a new attempt.

use chrono::{DateTime, Utc};
use sqlx::PgConnection;
use uuid::Uuid;

use crate::domain::{ErrorClass, ExecutionStatus, JobOutcome};

/// Who closed the attempt (PostgreSQL enum `attempt_closed_by`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClosedBy {
    Result,
    DeadLetter,
    Reaper,
}

impl ClosedBy {
    fn as_sql(self) -> &'static str {
        match self {
            ClosedBy::Result => "result",
            ClosedBy::DeadLetter => "dead_letter",
            ClosedBy::Reaper => "reaper",
        }
    }
}

/// Why the attempt is being closed.
#[derive(Debug, Clone)]
pub enum Closure<'a> {
    /// The provider answered (SUCCESS or NOT_FOUND). The job is complete.
    Answered {
        outcome: JobOutcome,
        execution_id: Uuid,
    },
    /// The attempt failed. Whether the job is retried is decided inside the UPDATE.
    Failed {
        class: ErrorClass,
        message: &'a str,
        retryable: bool,
        /// A wait the provider asked for (rate limiting); the retry is not scheduled sooner.
        retry_after_ms: Option<i64>,
        closed_by: ClosedBy,
        /// The execution that reported the failure, when a worker reported it.
        execution_id: Option<Uuid>,
    },
}

#[derive(Debug, Clone, Copy)]
pub struct RetryPolicy {
    /// How many times a job may be retried after being dead-lettered. A worker crash
    /// dead-letters every job that process was running, so one retry protects innocent
    /// neighbours of a poisonous job without letting the poisonous one loop.
    pub max_dead_letter_retries: i32,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        RetryPolicy {
            max_dead_letter_retries: 1,
        }
    }
}

/// The job's state after this call closed the attempt.
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct Closed {
    pub status: ExecutionStatus,
    pub shipment_id: Uuid,
    pub dead_letter_count: i32,
    pub next_attempt_at: Option<DateTime<Utc>>,
}

// The retry decision, written once and used by every column that depends on it.
// $3 answered, $7 retryable, $8 is a dead-letter, $9 max dead-letter retries.
// It reads the row's current values, which PostgreSQL re-reads after waiting for a
// concurrent writer, so the decision is always made on committed data.
macro_rules! will_retry {
    () => {
        "(NOT $3 AND $7 AND j.attempt < j.max_attempts AND (NOT $8 OR j.dead_letter_count + 1 <= $9))"
    };
}

/// Closes attempt `attempt` of job `job_id` if, and only if, it is still the job's current
/// open attempt. Returns `None` when someone else already closed it (or a newer attempt is
/// running); in that case nothing was changed.
///
/// Next state: answered → COMPLETED; retryable failure with attempts left → RETRYING with
/// `next_attempt_at = base · 2^(attempt−1) ± 20 %` (capped at the provider's maximum);
/// anything else → FAILED.
pub async fn close_attempt(
    conn: &mut PgConnection,
    job_id: Uuid,
    attempt: i32,
    closure: &Closure<'_>,
    policy: RetryPolicy,
) -> Result<Option<Closed>, sqlx::Error> {
    let (answered, outcome, class, message, retryable, retry_after_ms, closed_by, execution_id) =
        match closure {
            Closure::Answered {
                outcome,
                execution_id,
            } => (
                true,
                *outcome,
                None,
                None,
                false,
                None,
                ClosedBy::Result,
                Some(*execution_id),
            ),
            Closure::Failed {
                class,
                message,
                retryable,
                retry_after_ms,
                closed_by,
                execution_id,
            } => (
                false,
                JobOutcome::Failure,
                Some(*class),
                Some(truncate(message, 2000)),
                *retryable,
                *retry_after_ms,
                *closed_by,
                *execution_id,
            ),
        };
    let is_dead_letter = closed_by == ClosedBy::DeadLetter;
    // ±20 % jitter, so jobs that failed together do not all retry at the same instant.
    let jitter = 0.8 + 0.4 * rand::random::<f64>();

    let closed = sqlx::query_as::<_, Closed>(concat!(
        "UPDATE tracking_jobs j SET
             dead_letter_count = j.dead_letter_count + CASE WHEN $8 THEN 1 ELSE 0 END,
             status = CASE WHEN $3 THEN 'COMPLETED'::execution_status
                           WHEN ", will_retry!(), " THEN 'RETRYING'::execution_status
                           ELSE 'FAILED'::execution_status END,
             outcome = CASE WHEN $3 THEN $4::job_outcome
                            WHEN ", will_retry!(), " THEN NULL
                            ELSE 'FAILURE'::job_outcome END,
             last_error_class = $5::error_class,
             last_error_message = $6,
             next_attempt_at = CASE WHEN ", will_retry!(), " THEN
                 clock_timestamp() + GREATEST(
                     LEAST(p.retry_max_ms::float8, p.retry_base_ms::float8 * power(2::float8, j.attempt - 1)) * $10,
                     LEAST(COALESCE($11, 0), 3600000)::float8
                 ) * interval '1 millisecond' END,
             completed_at = CASE WHEN ", will_retry!(), " THEN NULL ELSE clock_timestamp() END,
             updated_at = clock_timestamp()
         FROM shipments s
         JOIN providers p ON p.id = s.provider_id
         WHERE j.id = $1 AND j.attempt = $2 AND j.status IN ('QUEUED', 'PROCESSING')
           AND s.id = j.shipment_id
         RETURNING j.status, j.shipment_id, j.dead_letter_count, j.next_attempt_at"
    ))
    .bind(job_id)
    .bind(attempt)
    .bind(answered)
    .bind(outcome)
    .bind(class)
    .bind(message)
    .bind(retryable)
    .bind(is_dead_letter)
    .bind(policy.max_dead_letter_retries)
    .bind(jitter)
    .bind(retry_after_ms)
    .fetch_optional(&mut *conn)
    .await?;

    if closed.is_some() {
        // The attempt row records the authoritative outcome. `accepted_execution_id` is
        // written here once and never again: the first result wins.
        sqlx::query(
            "UPDATE job_attempts SET
                 closed_at = clock_timestamp(), outcome = $3, error_class = $4, error_message = $5,
                 closed_by = $6::text::attempt_closed_by,
                 accepted_execution_id = CASE WHEN $6::text = 'result' THEN $7 END
             WHERE job_id = $1 AND attempt = $2 AND closed_at IS NULL",
        )
        .bind(job_id)
        .bind(attempt)
        .bind(outcome)
        .bind(class)
        .bind(message)
        .bind(closed_by.as_sql())
        .bind(execution_id)
        .execute(&mut *conn)
        .await?;
    }
    Ok(closed)
}

fn truncate(text: &str, max_chars: usize) -> &str {
    match text.char_indices().nth(max_chars) {
        Some((index, _)) => &text[..index],
        None => text,
    }
}
