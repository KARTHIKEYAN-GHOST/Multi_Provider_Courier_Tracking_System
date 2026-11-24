//! Periodic database-only tasks: the retry scheduler and the reaper. They need no broker
//! and keep running while RabbitMQ is unreachable.

use std::time::Duration;

use sqlx::PgPool;
use tokio_util::sync::CancellationToken;

use super::{close_attempt::RetryPolicy, reaper::reap_expired, retry_scheduler::open_due_attempts};

const BATCH: i64 = 100;

pub async fn run_retry_scheduler(pool: PgPool, interval: Duration, shutdown: CancellationToken) {
    loop {
        match open_due_attempts(&pool, BATCH).await {
            // A full batch means more may be waiting: go again immediately.
            Ok(opened) if opened as i64 == BATCH => continue,
            Ok(_) => {}
            Err(error) => tracing::error!(%error, "retry scheduler failed"),
        }
        tokio::select! {
            _ = shutdown.cancelled() => return,
            _ = tokio::time::sleep(interval) => {}
        }
    }
}

pub async fn run_reaper(
    pool: PgPool,
    interval: Duration,
    policy: RetryPolicy,
    shutdown: CancellationToken,
) {
    loop {
        match reap_expired(&pool, BATCH, policy).await {
            Ok(closed) if closed as i64 == BATCH => continue,
            Ok(_) => {}
            Err(error) => tracing::error!(%error, "reaper failed"),
        }
        tokio::select! {
            _ = shutdown.cancelled() => return,
            _ = tokio::time::sleep(interval) => {}
        }
    }
}
