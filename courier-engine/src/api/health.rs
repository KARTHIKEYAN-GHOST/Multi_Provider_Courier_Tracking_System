use axum::{Json, extract::State};
use serde_json::{Value, json};

use crate::{error::ApiError, state::AppState};

/// Liveness: the process is running.
pub async fn healthz() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

/// Readiness: PostgreSQL answers and the broker session is up.
///
/// The API itself keeps accepting requests while RabbitMQ is down (jobs wait in the outbox),
/// but the Engine reports not-ready so an orchestrator or operator can see it.
pub async fn readyz(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    sqlx::query_scalar::<_, i32>("SELECT 1")
        .fetch_one(&state.pool)
        .await
        .map_err(|e| {
            tracing::warn!(error = %e, "readiness check failed: database");
            ApiError::Unavailable("database")
        })?;
    let broker = match &state.broker {
        Some(health) if health.is_connected() => "ok",
        Some(_) => {
            tracing::warn!("readiness check failed: broker");
            return Err(ApiError::Unavailable("broker"));
        }
        None => "not_configured",
    };
    Ok(Json(
        json!({ "status": "ready", "checks": { "database": "ok", "broker": broker } }),
    ))
}
