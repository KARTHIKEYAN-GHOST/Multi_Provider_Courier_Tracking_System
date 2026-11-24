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

use super::dispatch::job_track_message;
use crate::db::{providers::DispatchTarget, tracking};
use crate::messaging::{outbox, routing};

#[derive(sqlx::FromRow)]
struct DueJob {
    id: Uuid,
    attempt: i32,
    correlation_id: Uuid,
    tracking_number: String,
    provider_id: Uuid,
    provider_code: String,
    adapter_id: Uuid,
    adapter_key: String,
    timeout_ms: i32,
    queue_wait_max_ms: i32,
    max_attempts: i32,
}

/// Opens the next attempt for up to `limit` due jobs. Returns how many it opened.
pub async fn open_due_attempts(pool: &PgPool, limit: i64) -> Result<usize, sqlx::Error> {
    let mut tx = pool.begin().await?;
    // SKIP LOCKED lets several scheduler instances run without ever picking the same job.
    let due = sqlx::query_as::<_, DueJob>(
        "SELECT j.id, j.attempt, j.correlation_id, s.tracking_number,
                p.id AS provider_id, p.code AS provider_code, a.id AS adapter_id, a.key AS adapter_key,
                p.timeout_ms, p.queue_wait_max_ms, j.max_attempts
         FROM tracking_jobs j
         JOIN shipments s ON s.id = j.shipment_id
         JOIN providers p ON p.id = s.provider_id
         JOIN adapters a ON a.id = j.adapter_id
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

        let target = DispatchTarget {
            provider_id: job.provider_id,
            provider_code: job.provider_code,
            adapter_id: job.adapter_id,
            adapter_key: job.adapter_key,
            timeout_ms: job.timeout_ms,
            queue_wait_max_ms: job.queue_wait_max_ms,
            max_attempts: job.max_attempts,
        };
        let message = job_track_message(
            Uuid::now_v7(),
            &target,
            job.id,
            attempt,
            &job.tracking_number,
            job.correlation_id,
            now,
            queue_deadline_at,
        );
        outbox::enqueue(
            &mut tx,
            routing::JOBS_EXCHANGE,
            &routing::job_track(&target.adapter_key),
            &message,
        )
        .await?;
        tracing::info!(job_id = %job.id, attempt, correlation_id = %job.correlation_id, "retry attempt opened");
    }
    tx.commit().await?;
    Ok(opened)
}
