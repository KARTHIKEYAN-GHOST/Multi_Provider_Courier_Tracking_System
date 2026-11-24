use axum::{Json, extract::State};
use serde_json::{Value, json};

use crate::{db, error::ApiError, state::AppState};

pub async fn list(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let providers = db::providers::list_enabled(&state.pool).await?;
    Ok(Json(json!({ "items": providers })))
}
