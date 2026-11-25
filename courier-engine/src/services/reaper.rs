//! The reaper: a backstop that guarantees no job stays QUEUED or PROCESSING forever.
//!
//! It covers every case where no message arrives to close an attempt: no worker running,
//! a worker that hung, a lost result, a dead-letter consumer that is down. It enforces two
//! of the three timers (the third, the adapter timeout, belongs to the worker):
//!
//! - queue-wait deadline   — QUEUED past `queue_deadline_at`         → `QUEUE_WAIT_EXCEEDED`
//! - execution recovery    — PROCESSING past `execution_deadline_at` → `WORKER_LOST`
//!
//! Both go through the guarded `close_attempt`, so the reaper can never close an attempt a
//! result or dead-letter already closed, and several reaper instances can run at once.

use sqlx::PgPool;
use uuid::Uuid;

use super::close_attempt::{ClosedBy, Closure, RetryPolicy, close_attempt};
use crate::domain::{ErrorClass, ExecutionStatus};

/// Closes up to `limit` expired attempts. Returns how many it closed.
pub async fn reap_expired(
    pool: &PgPool,
    limit: i64,
    policy: RetryPolicy,
) -> Result<usize, sqlx::Error> {
    let mut tx = pool.begin().await?;
    // SKIP LOCKED: rows another reaper (or a handler) is working on are left to them.
    let expired = sqlx::query_as::<_, (Uuid, i32, ExecutionStatus)>(
        "SELECT id, attempt, status FROM tracking_jobs
         WHERE (status = 'QUEUED' AND queue_deadline_at < clock_timestamp())
            OR (status = 'PROCESSING' AND execution_deadline_at < clock_timestamp())
         ORDER BY id
         LIMIT $1
         FOR UPDATE SKIP LOCKED",
    )
    .bind(limit)
    .fetch_all(&mut *tx)
    .await?;

    let mut closed = 0;
    for (job_id, attempt, status) in expired {
        let (class, message) = match status {
            ExecutionStatus::Queued => (
                ErrorClass::QueueWaitExceeded,
                "no worker started the attempt before its queue-wait deadline",
            ),
            _ => (
                ErrorClass::WorkerLost,
                "a worker started the attempt but no result arrived before the recovery deadline",
            ),
        };
        let closure = Closure::Failed {
            class,
            message,
            retryable: true,
            retry_after_ms: None,
            closed_by: ClosedBy::Reaper,
            execution_id: None,
        };
        if close_attempt(&mut tx, job_id, attempt, &closure, policy)
            .await?
            .is_some()
        {
            tracing::warn!(%job_id, attempt, error_class = ?class, "reaper closed an expired attempt");
            closed += 1;
        }
    }
    tx.commit().await?;
    // The third thing a job can wait for: a person. Unanswered challenges end the job.
    closed += super::interventions::expire_due(pool, limit).await?;
    Ok(closed)
}
