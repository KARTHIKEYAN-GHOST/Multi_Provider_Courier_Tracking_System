use axum::{Json, extract::State};
use serde_json::{Value, json};

use crate::{error::ApiError, state::AppState};

/// Liveness: the process is running.
pub async fn healthz() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

/// Readiness: dependencies are reachable. (The RabbitMQ check is added with messaging in Phase 3.)
pub async fn readyz(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    sqlx::query_scalar::<_, i32>("SELECT 1")
        .fetch_one(&state.pool)
        .await
        .map_err(|e| {
            tracing::warn!(error = %e, "readiness check failed: database");
            ApiError::Unavailable("database")
        })?;
    Ok(Json(
        json!({ "status": "ready", "checks": { "database": "ok" } }),
    ))
}
