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
}

// Every SQL string in the Engine is a compile-time constant; shared fragments use macros.
macro_rules! dispatch_target_select {
    () => {
        "SELECT p.id AS provider_id, p.code AS provider_code, a.id AS adapter_id, a.key AS adapter_key,
                p.timeout_ms, p.queue_wait_max_ms, p.max_attempts
         FROM providers p
         JOIN adapters a ON a.provider_id = p.id AND a.enabled
         WHERE p.enabled"
    };
}

pub async fn dispatch_target_by_code(
    conn: &mut PgConnection,
    code: &str,
) -> Result<Option<DispatchTarget>, sqlx::Error> {
    sqlx::query_as::<_, DispatchTarget>(concat!(
        dispatch_target_select!(),
        " AND p.code = $1 ORDER BY a.key LIMIT 1"
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
        " AND p.id = $1 ORDER BY a.key LIMIT 1"
    ))
    .bind(provider_id)
    .fetch_optional(conn)
    .await
}
