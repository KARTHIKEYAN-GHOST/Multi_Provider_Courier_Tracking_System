//! The retry scheduler: the only component that opens a new attempt for an existing job.
//!
//! Closing an attempt (result, dead-letter, reaper) only ever moves a job to RETRYING. This
//! scheduler picks up RETRYING jobs whose `next_attempt_at` has passed and, in one
//! transaction, moves the job to QUEUED with `attempt + 1`, inserts the `job_attempts` row
//! and writes the `job.track` outbox message. One logical attempt therefore has exactly one
//! outbox row, and no two actors can schedule competing retries.

use chrono::{DateTime, Duration, Utc};
use sqlx::PgPool;
use uuid::Uuid;

use super::dispatch::enqueue_job_track;
use crate::db::{providers, tracking};

#[derive(sqlx::FromRow)]
struct DueJob {
    id: Uuid,
    attempt: i32,
    correlation_id: Uuid,
    tracking_number: String,
    adapter_id: Uuid,
    queue_wait_max_ms: i32,
    shipment_id: Uuid,
}

/// Opens the next attempt for up to `limit` due jobs. Returns how many it opened.
pub async fn open_due_attempts(pool: &PgPool, limit: i64) -> Result<usize, sqlx::Error> {
    let mut tx = pool.begin().await?;
    // SKIP LOCKED lets several scheduler instances run without ever picking the same job.
    let due = sqlx::query_as::<_, DueJob>(
        "SELECT j.id, j.attempt, j.correlation_id, s.tracking_number,
                j.adapter_id, p.queue_wait_max_ms, j.shipment_id
         FROM tracking_jobs j
         JOIN shipments s ON s.id = j.shipment_id
         JOIN providers p ON p.id = s.provider_id
         WHERE j.status = 'RETRYING' AND j.next_attempt_at <= clock_timestamp()
         ORDER BY j.next_attempt_at
         LIMIT $1
         FOR UPDATE OF j SKIP LOCKED",
    )
    .bind(limit)
    .fetch_all(&mut *tx)
    .await?;

    let opened = due.len();
    for job in due {
        let now: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *tx)
            .await?;
        // A fresh queue-wait deadline for the new attempt; the execution deadline starts
        // again only when a worker reports job.started.
        let queue_deadline_at = now + Duration::milliseconds(i64::from(job.queue_wait_max_ms));
        let attempt = job.attempt + 1;

        sqlx::query(
            "UPDATE tracking_jobs SET
                 status = 'QUEUED', attempt = $2, queue_deadline_at = $3,
                 execution_deadline_at = NULL, next_attempt_at = NULL, updated_at = $4
             WHERE id = $1",
        )
        .bind(job.id)
        .bind(attempt)
        .bind(queue_deadline_at)
        .bind(now)
        .execute(&mut *tx)
        .await?;
        tracking::insert_attempt(&mut tx, job.id, attempt, now).await?;

        let target = providers::dispatch_target_by_adapter(&mut tx, job.adapter_id)
            .await?
            .ok_or(sqlx::Error::RowNotFound)?;
        let target = providers::scoped(&mut tx, target, job.shipment_id).await?;
        enqueue_job_track(
            &mut tx,
            &target,
            job.id,
            attempt,
            &job.tracking_number,
            job.correlation_id,
            now,
        )
        .await?;
        tracing::info!(job_id = %job.id, attempt, correlation_id = %job.correlation_id, "retry attempt opened");
    }
    tx.commit().await?;
    Ok(opened)
}
