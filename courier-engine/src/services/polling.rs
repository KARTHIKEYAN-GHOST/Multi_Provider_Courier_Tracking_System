//! Automatic re-polling: a shipment is looked up again and again until it is finished.
//!
//! Tracking is not one-shot. After the first lookup the Engine keeps asking the provider,
//! on a per-provider policy, until the shipment reaches DELIVERED or RETURNED:
//!
//! - `poll_interval_ms`: time until the next lookup (0 = the provider is never polled)
//! - `poll_max_interval_ms`: the interval doubles after each lookup that brought nothing
//!   new, up to this value; a lookup with a new event resets it
//! - `poll_give_up_days`: polling stops when nothing new has appeared for this long
//!
//! `shipments.next_poll_at` holds the time of the next lookup; NULL means "not scheduled".
//! It is written only while the shipment row is locked, by whoever creates a job (first
//! request, manual refresh, this scheduler) and by the result handler. A poll is an ordinary
//! job with trigger `poll`: retries, timers, rate limiting and dead-lettering all apply.

use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use super::dispatch;
use crate::db;
use crate::domain::{JobTrigger, ShipmentStatus};

/// Sets the time of the shipment's next automatic lookup from its provider's policy and the
/// shipment's current back-off, or clears it if the shipment is not to be polled.
/// The caller holds the shipment row lock.
pub async fn schedule_next_poll(
    conn: &mut PgConnection,
    shipment_id: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE shipments s SET next_poll_at = CASE
                 WHEN p.poll_interval_ms > 0
                      AND s.archived_at IS NULL
                      AND s.current_status NOT IN ('DELIVERED', 'RETURNED')
                 THEN clock_timestamp() + LEAST(
                          GREATEST(p.poll_max_interval_ms, p.poll_interval_ms)::float8,
                          p.poll_interval_ms * power(2::float8, LEAST(s.polls_without_change, 20))
                      ) * interval '1 millisecond'
             END
         FROM providers p
         WHERE s.id = $1 AND p.id = s.provider_id",
    )
    .bind(shipment_id)
    .execute(conn)
    .await?;
    Ok(())
}

/// Like `schedule_next_poll`, but leaves an already scheduled lookup where it is.
pub async fn schedule_next_poll_if_unset(
    conn: &mut PgConnection,
    shipment_id: Uuid,
) -> Result<(), sqlx::Error> {
    let unset: bool =
        sqlx::query_scalar("SELECT next_poll_at IS NULL FROM shipments WHERE id = $1")
            .bind(shipment_id)
            .fetch_one(&mut *conn)
            .await?;
    if unset {
        schedule_next_poll(conn, shipment_id).await?;
    }
    Ok(())
}

/// What one scheduler pass did.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct PollStats {
    /// Lookups started.
    pub started: usize,
    /// Due, but a lookup was already running or the provider is unavailable: asked again later.
    pub postponed: usize,
    /// Polling ended: finished, nobody subscribed any more, or nothing new for too long.
    pub stopped: usize,
}

impl PollStats {
    pub fn total(&self) -> usize {
        self.started + self.postponed + self.stopped
    }
}

#[derive(sqlx::FromRow)]
struct Due {
    id: Uuid,
    provider_id: Uuid,
    tracking_number: String,
    current_status: ShipmentStatus,
    has_subscriber: bool,
    gave_up: bool,
}

/// Starts a lookup for up to `limit` shipments whose next lookup is due.
///
/// Each due shipment is locked (`SKIP LOCKED`, so several Engine instances share the work and
/// never poll the same shipment twice) and handled in the same transaction that creates its
/// job — the same lock and the same job-creation path the API uses.
pub async fn poll_due(pool: &PgPool, limit: i64) -> Result<PollStats, sqlx::Error> {
    let mut tx = pool.begin().await?;
    let due = sqlx::query_as::<_, Due>(
        "SELECT s.id, s.provider_id, s.tracking_number, s.current_status,
                EXISTS (SELECT 1 FROM tracking_requests r
                        WHERE r.shipment_id = s.id AND r.status = 'active') AS has_subscriber,
                COALESCE(s.last_change_at, s.created_at)
                    < clock_timestamp() - p.poll_give_up_days * interval '1 day' AS gave_up
         FROM shipments s
         JOIN providers p ON p.id = s.provider_id
         WHERE s.next_poll_at <= clock_timestamp() AND s.archived_at IS NULL
         ORDER BY s.next_poll_at
         LIMIT $1
         FOR UPDATE OF s SKIP LOCKED",
    )
    .bind(limit)
    .fetch_all(&mut *tx)
    .await?;

    let mut stats = PollStats::default();
    for shipment in due {
        // Polling continues only while somebody is subscribed and the shipment can still change.
        if shipment.current_status.is_terminal() || !shipment.has_subscriber || shipment.gave_up {
            sqlx::query("UPDATE shipments SET next_poll_at = NULL WHERE id = $1")
                .bind(shipment.id)
                .execute(&mut *tx)
                .await?;
            stats.stopped += 1;
            continue;
        }
        let target = db::providers::dispatch_target_by_id(&mut tx, shipment.provider_id).await?;
        let job_id = match target {
            // A disabled provider, a missing credential or withdrawn scraping permission
            // pauses polling; it resumes by itself once the provider is available again.
            Some(target) if target.blocked_reason().is_none() => {
                dispatch::ensure_job(
                    &mut tx,
                    &target,
                    shipment.id,
                    &shipment.tracking_number,
                    JobTrigger::Poll,
                    Uuid::now_v7(),
                )
                .await?
            }
            _ => None,
        };
        match job_id {
            Some(job_id) => {
                tracing::debug!(shipment_id = %shipment.id, %job_id, "poll started");
                stats.started += 1;
            }
            None => {
                // `ensure_job` schedules the next lookup only when it creates a job.
                schedule_next_poll(&mut tx, shipment.id).await?;
                stats.postponed += 1;
            }
        }
    }
    tx.commit().await?;
    Ok(stats)
}
