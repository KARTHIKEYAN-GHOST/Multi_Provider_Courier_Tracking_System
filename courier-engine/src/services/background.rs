//! Periodic database-only tasks: the retry scheduler, the reaper, the polling scheduler and
//! maintenance (archiving and retention). They need no broker and keep running while
//! RabbitMQ is unreachable.

use std::time::Duration;

use sqlx::PgPool;
use tokio_util::sync::CancellationToken;

use super::{
    close_attempt::RetryPolicy,
    polling::poll_due,
    reaper::reap_expired,
    retention::{RetentionSettings, run_maintenance},
    retry_scheduler::open_due_attempts,
};

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

pub async fn run_poll_scheduler(pool: PgPool, interval: Duration, shutdown: CancellationToken) {
    loop {
        match poll_due(&pool, BATCH).await {
            Ok(stats) if stats.total() as i64 == BATCH => continue,
            Ok(stats) if stats.total() > 0 => {
                tracing::info!(
                    started = stats.started,
                    postponed = stats.postponed,
                    stopped = stats.stopped,
                    "polling scheduler pass"
                );
            }
            Ok(_) => {}
            Err(error) => tracing::error!(%error, "polling scheduler failed"),
        }
        tokio::select! {
            _ = shutdown.cancelled() => return,
            _ = tokio::time::sleep(interval) => {}
        }
    }
}

pub async fn run_maintenance_loop(
    pool: PgPool,
    interval: Duration,
    settings: RetentionSettings,
    shutdown: CancellationToken,
) {
    loop {
        match run_maintenance(&pool, &settings).await {
            Ok(report) if !report.is_empty() => {
                tracing::info!(?report, "maintenance pass");
            }
            Ok(_) => {}
            Err(error) => tracing::error!(%error, "maintenance failed"),
        }
        tokio::select! {
            _ = shutdown.cancelled() => return,
            _ = tokio::time::sleep(interval) => {}
        }
    }
}
