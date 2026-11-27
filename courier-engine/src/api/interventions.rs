//! `/v1/interventions` — challenges (one-time codes, CAPTCHAs) waiting for an answer.
//!
//! Only admins see and answer them, every organization's alike; an organization user gets
//! 403. The answer is write-only: it can be submitted once and is never returned by any
//! endpoint.

use axum::{
    Extension, Json,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use base64::Engine as _;
use serde::Deserialize;
use serde_json::{Value, json};
use uuid::Uuid;

use super::ApiJson;
use crate::{
    error::ApiError,
    services::{
        api_keys::Caller,
        interventions::{self, InterventionRow, SubmitError},
    },
    state::AppState,
};

const NOT_FOUND: ApiError = ApiError::NotFound("intervention");

fn require_admin(caller: &Caller) -> Result<(), ApiError> {
    if caller.is_admin() {
        Ok(())
    } else {
        Err(ApiError::Forbidden {
            code: "admins_only",
            message: "only admins see and answer verification requests",
        })
    }
}

fn view(row: &InterventionRow) -> Value {
    json!({
        "id": row.id,
        "kind": row.kind,
        "prompt": row.prompt,
        "status": row.status,
        "response_format": row.response_format,
        "has_image": row.has_image,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "provider_code": row.provider_code,
        "tracking_number": row.tracking_number,
    })
}

/// GET /v1/interventions — every open challenge, oldest first.
pub async fn list(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
) -> Result<Json<Value>, ApiError> {
    require_admin(&caller)?;
    let rows = interventions::list_pending(&state.pool, caller.user_id).await?;
    Ok(Json(
        json!({ "items": rows.iter().map(view).collect::<Vec<_>>() }),
    ))
}

/// GET /v1/interventions/{id} — one challenge, with its picture (base64) if it has one.
pub async fn get_one(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    require_admin(&caller)?;
    let id = Uuid::parse_str(&id).map_err(|_| NOT_FOUND)?;
    let (row, image) = interventions::find(&state.pool, caller.user_id, id)
        .await?
        .ok_or(NOT_FOUND)?;
    let mut body = view(&row);
    body["image"] = match image {
        Some((content_type, bytes)) => json!({
            "content_type": content_type,
            "data": base64::engine::general_purpose::STANDARD.encode(bytes),
        }),
        None => Value::Null,
    };
    Ok(Json(body))
}

#[derive(Debug, Deserialize)]
pub struct ResponseBody {
    response: String,
}

/// POST /v1/interventions/{id}/response — answer a challenge. 202: the lookup continues.
pub async fn respond(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Path(id): Path<String>,
    ApiJson(body): ApiJson<ResponseBody>,
) -> Result<Response, ApiError> {
    require_admin(&caller)?;
    let id = Uuid::parse_str(&id).map_err(|_| NOT_FOUND)?;
    let Some(store) = state.secrets.as_deref() else {
        return Err(ApiError::Unavailable("answer store"));
    };
    let answer = body.response.trim();
    match interventions::submit(&state.pool, store.master(), caller.user_id, id, answer).await {
        Ok(()) => Ok((StatusCode::ACCEPTED, Json(json!({ "status": "answered" }))).into_response()),
        Err(SubmitError::NotFound) => Err(NOT_FOUND),
        Err(SubmitError::NotPending) => Err(ApiError::Conflict {
            code: "already_answered",
            message: "this challenge has already been answered",
        }),
        Err(SubmitError::Expired) => Err(ApiError::Gone {
            code: "intervention_expired",
            message: "this challenge is no longer open",
        }),
        Err(SubmitError::TooManyAttempts) => Err(ApiError::Gone {
            code: "too_many_invalid_answers",
            message: "too many invalid answers were submitted for this challenge",
        }),
        Err(SubmitError::Invalid(problem)) => Err(ApiError::validation("response", problem)),
        Err(SubmitError::Database(e)) => Err(e.into()),
    }
}
