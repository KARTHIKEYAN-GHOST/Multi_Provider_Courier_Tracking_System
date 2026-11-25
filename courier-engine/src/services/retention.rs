//! Archiving and retention: the periodic clean-up that keeps the database from growing
//! without bound and lets couriers reuse tracking numbers.
//!
//! 1. **Archive.** A shipment row is one generation of a tracking number. It is closed
//!    (`archived_at`) when it has been finished for longer than its provider's reuse window,
//!    or when nobody has been subscribed and nothing has happened for that long. A later
//!    request for the same number then starts a new generation.
//! 2. **Purge.** `RETENTION_DAYS` after archiving, the shipment's events and raw provider
//!    responses are deleted. The shipment row, its jobs and the users' subscriptions stay as
//!    a record of what was tracked.
//! 3. **Messages.** Sent outbox rows and inbox (dedupe) rows are deleted after a few days.
//!
//! Every step works in bounded batches, skips rows another instance has locked, and can be
//! run by several Engine instances at once.

use serde::Serialize;
use sqlx::PgPool;
use uuid::Uuid;

#[derive(Debug, Clone, Copy)]
pub struct RetentionSettings {
    /// Days between archiving a shipment and deleting its events. 0 = never delete.
    pub retention_days: i32,
    /// Days a sent outbox message is kept. 0 = keep forever.
    pub outbox_retention_days: i32,
    /// Days an inbox (dedupe) row is kept. 0 = keep forever.
    pub inbox_retention_days: i32,
    /// Rows handled per statement.
    pub batch: i64,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct MaintenanceReport {
    pub shipments_archived: u64,
    pub shipments_purged: u64,
    pub events_deleted: u64,
    pub raw_responses_deleted: u64,
    pub outbox_rows_deleted: u64,
    pub inbox_rows_deleted: u64,
}

impl MaintenanceReport {
    pub fn is_empty(&self) -> bool {
        *self == MaintenanceReport::default()
    }
}

/// Closes up to `limit` shipments whose generation is over. Returns how many.
pub async fn archive_due(pool: &PgPool, limit: i64) -> Result<u64, sqlx::Error> {
    // The shipment lock is the one job creation takes first, so no job can appear for a
    // shipment while it is being archived (and a database trigger refuses it regardless).
    let result = sqlx::query(
        "WITH due AS (
             SELECT s.id FROM shipments s
             JOIN providers p ON p.id = s.provider_id
             WHERE s.archived_at IS NULL
               AND NOT EXISTS (SELECT 1 FROM tracking_jobs j
                               WHERE j.shipment_id = s.id
                                 AND j.status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED'))
               AND (
                   -- finished for longer than the reuse window
                   s.terminal_at < clock_timestamp() - p.reuse_window_days * interval '1 day'
                   -- or: no subscriber, and nothing has happened for that long
                   OR (NOT EXISTS (SELECT 1 FROM tracking_requests r
                                   WHERE r.shipment_id = s.id AND r.status = 'active')
                       AND GREATEST(
                               s.created_at,
                               COALESCE(s.last_change_at, s.created_at),
                               COALESCE((SELECT max(r.cancelled_at) FROM tracking_requests r
                                         WHERE r.shipment_id = s.id), s.created_at)
                           ) < clock_timestamp() - p.reuse_window_days * interval '1 day')
               )
             ORDER BY s.id
             LIMIT $1
             FOR UPDATE OF s SKIP LOCKED
         )
         UPDATE shipments s
         SET archived_at = clock_timestamp(), next_poll_at = NULL, updated_at = clock_timestamp()
         FROM due WHERE s.id = due.id",
    )
    .bind(limit)
    .execute(pool)
    .await?;
    Ok(result.rows_affected())
}

/// Deletes the events and raw responses of up to `limit` shipments archived more than
/// `retention_days` ago. Returns (shipments, events, raw responses).
pub async fn purge_due(
    pool: &PgPool,
    retention_days: i32,
    limit: i64,
) -> Result<(u64, u64, u64), sqlx::Error> {
    if retention_days <= 0 {
        return Ok((0, 0, 0));
    }
    let mut tx = pool.begin().await?;
    let shipments: Vec<Uuid> = sqlx::query_scalar(
        "SELECT id FROM shipments
         WHERE archived_at < clock_timestamp() - $1 * interval '1 day' AND purged_at IS NULL
         ORDER BY archived_at
         LIMIT $2
         FOR UPDATE SKIP LOCKED",
    )
    .bind(retention_days)
    .bind(limit)
    .fetch_all(&mut *tx)
    .await?;
    if shipments.is_empty() {
        return Ok((0, 0, 0));
    }
    // Events refer to raw responses, so they go first.
    let events = sqlx::query("DELETE FROM tracking_events WHERE shipment_id = ANY($1)")
        .bind(&shipments)
        .execute(&mut *tx)
        .await?
        .rows_affected();
    let raw = sqlx::query("DELETE FROM raw_tracking_responses WHERE shipment_id = ANY($1)")
        .bind(&shipments)
        .execute(&mut *tx)
        .await?
        .rows_affected();
    sqlx::query("UPDATE shipments SET purged_at = clock_timestamp() WHERE id = ANY($1)")
        .bind(&shipments)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok((shipments.len() as u64, events, raw))
}

/// Deletes old sent outbox rows and old inbox rows. Returns (outbox, inbox).
///
/// Pending outbox rows are never deleted. Inbox rows only stop an identical message from
/// being handled twice; after weeks no such duplicate can still be in flight, and the guarded
/// state transitions would make it harmless anyway.
pub async fn clean_messages(
    pool: &PgPool,
    settings: &RetentionSettings,
) -> Result<(u64, u64), sqlx::Error> {
    let mut outbox = 0;
    if settings.outbox_retention_days > 0 {
        outbox = sqlx::query(
            "DELETE FROM outbox_messages WHERE message_id IN (
                 SELECT message_id FROM outbox_messages
                 WHERE status = 'sent' AND sent_at < clock_timestamp() - $1 * interval '1 day'
                 LIMIT $2 FOR UPDATE SKIP LOCKED)",
        )
        .bind(settings.outbox_retention_days)
        .bind(settings.batch)
        .execute(pool)
        .await?
        .rows_affected();
    }
    let mut inbox = 0;
    if settings.inbox_retention_days > 0 {
        inbox = sqlx::query(
            "DELETE FROM processed_messages WHERE (consumer, message_id) IN (
                 SELECT consumer, message_id FROM processed_messages
                 WHERE processed_at < clock_timestamp() - $1 * interval '1 day'
                 LIMIT $2 FOR UPDATE SKIP LOCKED)",
        )
        .bind(settings.inbox_retention_days)
        .bind(settings.batch)
        .execute(pool)
        .await?
        .rows_affected();
    }
    Ok((outbox, inbox))
}

/// One complete maintenance pass: repeats every step until nothing is left to do.
pub async fn run_maintenance(
    pool: &PgPool,
    settings: &RetentionSettings,
) -> Result<MaintenanceReport, sqlx::Error> {
    let mut report = MaintenanceReport::default();
    loop {
        let archived = archive_due(pool, settings.batch).await?;
        report.shipments_archived += archived;
        if (archived as i64) < settings.batch {
            break;
        }
    }
    loop {
        let (shipments, events, raw) =
            purge_due(pool, settings.retention_days, settings.batch).await?;
        report.shipments_purged += shipments;
        report.events_deleted += events;
        report.raw_responses_deleted += raw;
        if (shipments as i64) < settings.batch {
            break;
        }
    }
    loop {
        let (outbox, inbox) = clean_messages(pool, settings).await?;
        report.outbox_rows_deleted += outbox;
        report.inbox_rows_deleted += inbox;
        if (outbox as i64) < settings.batch && (inbox as i64) < settings.batch {
            break;
        }
    }
    Ok(report)
}
