use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use crate::domain::TrackingMethod;

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ProviderView {
    pub code: String,
    pub name: String,
    pub tracking_method: TrackingMethod,
}

pub async fn list_enabled(pool: &PgPool) -> Result<Vec<ProviderView>, sqlx::Error> {
    sqlx::query_as::<_, ProviderView>(
        "SELECT code, name, tracking_method FROM providers WHERE enabled ORDER BY code",
    )
    .fetch_all(pool)
    .await
}

/// Everything needed to dispatch a job for a provider: the provider's limits and the
/// adapter that serves it.
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct DispatchTarget {
    pub provider_id: Uuid,
    pub provider_code: String,
    pub adapter_id: Uuid,
    pub adapter_key: String,
    pub timeout_ms: i32,
    pub queue_wait_max_ms: i32,
    pub max_attempts: i32,
    pub tracking_method: TrackingMethod,
    pub scraping_authorized: bool,
    pub requires_credential: bool,
    /// Minimum time between two dispatches to this provider; 0 means no limit.
    pub min_dispatch_interval_ms: i32,
    /// The provider's credential in use, if one is stored.
    pub credential_id: Option<Uuid>,
    /// A repair of the adapter is waiting or under way. Its lookups would fail one by one,
    /// so no new lookup is started until the repair has ended (see `REPAIR_PAUSE_HOURS`).
    pub under_repair: bool,
}

impl DispatchTarget {
    /// Why a new job must not be created for this provider right now, if there is a reason.
    pub fn blocked_reason(&self) -> Option<&'static str> {
        if self.tracking_method != TrackingMethod::Api && !self.scraping_authorized {
            return Some("scraping this provider has not been authorized");
        }
        if self.requires_credential && self.credential_id.is_none() {
            return Some("no credential is stored for this provider");
        }
        None
    }
}

/// A repair pauses its provider for at most this long. A task nobody works on (the repair
/// service is not running) must not keep a courier switched off for ever. The same number
/// is written in `dispatch_target_select`.
pub const REPAIR_PAUSE_HOURS: i64 = 6;

// Every SQL string in the Engine is a compile-time constant; shared fragments use macros.
macro_rules! dispatch_target_select {
    () => {
        "SELECT p.id AS provider_id, p.code AS provider_code, a.id AS adapter_id, a.key AS adapter_key,
                p.timeout_ms, p.queue_wait_max_ms, p.max_attempts, p.tracking_method,
                p.scraping_authorized, p.requires_credential, p.min_dispatch_interval_ms,
                c.id AS credential_id,
                EXISTS (SELECT 1 FROM repair_tasks t
                        WHERE t.adapter_key = a.key AND t.kind = 'repair'
                          AND t.status IN ('pending', 'running')
                          AND t.created_at > clock_timestamp() - interval '6 hours') AS under_repair
         FROM providers p
         JOIN adapters a ON a.provider_id = p.id
         LEFT JOIN provider_credentials c ON c.provider_id = p.id AND c.revoked_at IS NULL"
    };
}

pub async fn dispatch_target_by_code(
    conn: &mut PgConnection,
    code: &str,
) -> Result<Option<DispatchTarget>, sqlx::Error> {
    sqlx::query_as::<_, DispatchTarget>(concat!(
        dispatch_target_select!(),
        " WHERE p.enabled AND a.enabled AND p.code = $1 ORDER BY a.key LIMIT 1"
    ))
    .bind(code)
    .fetch_optional(conn)
    .await
}

pub async fn dispatch_target_by_id(
    conn: &mut PgConnection,
    provider_id: Uuid,
) -> Result<Option<DispatchTarget>, sqlx::Error> {
    sqlx::query_as::<_, DispatchTarget>(concat!(
        dispatch_target_select!(),
        " WHERE p.enabled AND a.enabled AND p.id = $1 ORDER BY a.key LIMIT 1"
    ))
    .bind(provider_id)
    .fetch_optional(conn)
    .await
}

/// The target of a job that already exists (used for retries). The job keeps the adapter it
/// was created with, even if the provider has been disabled since.
pub async fn dispatch_target_by_adapter(
    conn: &mut PgConnection,
    adapter_id: Uuid,
) -> Result<Option<DispatchTarget>, sqlx::Error> {
    sqlx::query_as::<_, DispatchTarget>(concat!(dispatch_target_select!(), " WHERE a.id = $1"))
        .bind(adapter_id)
        .fetch_optional(conn)
        .await
}

/// Rate limiting: reserves the next free dispatch slot of a provider and returns it.
///
/// Slots are `min_dispatch_interval_ms` apart. The provider row is locked while the slot is
/// taken, so concurrent dispatches (from any number of Engine instances) each get their own
/// slot. The caller publishes its message no earlier than the returned time.
pub async fn reserve_dispatch_slot(
    conn: &mut PgConnection,
    provider_id: Uuid,
    now: DateTime<Utc>,
) -> Result<DateTime<Utc>, sqlx::Error> {
    sqlx::query_scalar::<_, DateTime<Utc>>(
        "UPDATE providers
         SET next_dispatch_at = GREATEST(COALESCE(next_dispatch_at, $2), $2)
                                + min_dispatch_interval_ms * interval '1 millisecond'
         WHERE id = $1
         RETURNING next_dispatch_at - min_dispatch_interval_ms * interval '1 millisecond'",
    )
    .bind(provider_id)
    .bind(now)
    .fetch_one(conn)
    .await
}

/// Applies ENABLE_DUMMY_PROVIDER: switches every test provider on or off. Returns how many
/// providers changed.
pub async fn set_test_providers_enabled(pool: &PgPool, enabled: bool) -> Result<u64, sqlx::Error> {
    let result = sqlx::query(
        "UPDATE providers SET enabled = $1, updated_at = now() WHERE is_test AND enabled <> $1",
    )
    .bind(enabled)
    .execute(pool)
    .await?;
    Ok(result.rows_affected())
}
