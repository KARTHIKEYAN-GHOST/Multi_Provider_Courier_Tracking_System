//! `/v1/tracking-requests` — the request-centric API.
//!
//! Every handler resolves the `{id}` together with the caller's user ID. A request that does
//! not exist and a request that belongs to someone else are indistinguishable: both are 404.

use axum::{
    Extension, Json,
    extract::{Path, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::json;
use uuid::Uuid;

use super::{
    ApiJson, CorrelationId,
    dto::{CreateTrackingRequest, EventView, JobView, ListParams, TrackingRequestView},
};
use crate::{
    db::tracking,
    error::ApiError,
    services::{
        api_keys::Caller,
        dispatch::{self, DispatchError, NewTrackingRequest, RefreshResult},
    },
    state::AppState,
};

const DEFAULT_PAGE_SIZE: i64 = 50;
const MAX_PAGE_SIZE: i64 = 200;
const MAX_REFERENCE_LEN: usize = 200;
const NOT_FOUND: ApiError = ApiError::NotFound("tracking request");

impl From<DispatchError> for ApiError {
    fn from(error: DispatchError) -> Self {
        match error {
            DispatchError::UnknownProvider => ApiError::NotFound("provider"),
            DispatchError::InvalidTrackingNumber(problem) => {
                ApiError::validation("tracking_number", problem)
            }
            DispatchError::RequestNotFound => NOT_FOUND,
            DispatchError::ShipmentArchived => ApiError::Conflict {
                code: "shipment_archived",
                message: "this shipment is archived and can no longer be refreshed; \
                          create a new tracking request for the tracking number",
            },
            DispatchError::ProviderUnavailable => ApiError::Conflict {
                code: "provider_unavailable",
                message: "the provider for this shipment is currently disabled",
            },
            DispatchError::Database(e) => e.into(),
        }
    }
}

/// An ID that is not a UUID cannot name anything, so it is a 404 like any other unknown ID.
fn parse_id(raw: &str) -> Result<Uuid, ApiError> {
    Uuid::parse_str(raw).map_err(|_| NOT_FOUND)
}

fn is_valid_provider_code(code: &str) -> bool {
    let mut chars = code.chars();
    code.len() <= 50
        && chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// POST /v1/tracking-requests — 201 for a new subscription, 200 if the caller already had one.
pub async fn create(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Extension(CorrelationId(correlation_id)): Extension<CorrelationId>,
    ApiJson(body): ApiJson<CreateTrackingRequest>,
) -> Result<Response, ApiError> {
    if !is_valid_provider_code(&body.provider_code) {
        return Err(ApiError::validation(
            "provider_code",
            "must be a lowercase provider code such as \"dummy\"",
        ));
    }
    let reference = body
        .reference
        .as_deref()
        .map(str::trim)
        .filter(|r| !r.is_empty());
    if reference.is_some_and(|r| r.chars().count() > MAX_REFERENCE_LEN) {
        return Err(ApiError::validation(
            "reference",
            "must be at most 200 characters",
        ));
    }

    let subscribed = dispatch::create_tracking_request(
        &state.pool,
        &NewTrackingRequest {
            user_id: caller.user_id,
            provider_code: &body.provider_code,
            tracking_number: &body.tracking_number,
            reference,
            correlation_id,
        },
    )
    .await?;

    let row = tracking::find_request(&state.pool, caller.user_id, subscribed.request_id)
        .await?
        .ok_or(NOT_FOUND)?;
    let status = if subscribed.created {
        StatusCode::CREATED
    } else {
        StatusCode::OK
    };
    Ok((status, Json(TrackingRequestView::from(row))).into_response())
}

/// GET /v1/tracking-requests — the caller's own requests, newest first.
pub async fn list(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Query(params): Query<ListParams>,
) -> Result<Response, ApiError> {
    let limit = match params.limit.as_deref() {
        None => DEFAULT_PAGE_SIZE,
        Some(raw) => raw
            .parse::<i64>()
            .ok()
            .filter(|n| (1..=MAX_PAGE_SIZE).contains(n))
            .ok_or_else(|| ApiError::validation("limit", "must be a number between 1 and 200"))?,
    };
    let before = params
        .cursor
        .as_deref()
        .map(|raw| {
            Uuid::parse_str(raw)
                .map_err(|_| ApiError::validation("cursor", "is not a valid cursor"))
        })
        .transpose()?;

    // One extra row tells us whether another page exists.
    let mut rows = tracking::list_requests(&state.pool, caller.user_id, before, limit + 1).await?;
    let has_more = rows.len() as i64 > limit;
    rows.truncate(limit as usize);
    let next_cursor = if has_more {
        rows.last().map(|r| r.id)
    } else {
        None
    };

    let items: Vec<TrackingRequestView> = rows.into_iter().map(Into::into).collect();
    Ok(Json(json!({ "items": items, "next_cursor": next_cursor })).into_response())
}

/// GET /v1/tracking-requests/{id}
pub async fn get_one(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Path(id): Path<String>,
) -> Result<Json<TrackingRequestView>, ApiError> {
    let row = tracking::find_request(&state.pool, caller.user_id, parse_id(&id)?)
        .await?
        .ok_or(NOT_FOUND)?;
    Ok(Json(row.into()))
}

/// GET /v1/tracking-requests/{id}/events — tracking history, oldest first.
pub async fn events(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let shipment_id =
        tracking::shipment_id_for_request(&state.pool, caller.user_id, parse_id(&id)?)
            .await?
            .ok_or(NOT_FOUND)?;
    let items: Vec<EventView> = tracking::list_events(&state.pool, shipment_id)
        .await?
        .into_iter()
        .map(Into::into)
        .collect();
    Ok(Json(json!({ "items": items })).into_response())
}

/// GET /v1/tracking-requests/{id}/jobs — lookup jobs for the shipment, newest first.
pub async fn jobs(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let shipment_id =
        tracking::shipment_id_for_request(&state.pool, caller.user_id, parse_id(&id)?)
            .await?
            .ok_or(NOT_FOUND)?;
    let items: Vec<JobView> = tracking::list_jobs(&state.pool, shipment_id)
        .await?
        .into_iter()
        .map(Into::into)
        .collect();
    Ok(Json(json!({ "items": items })).into_response())
}

/// POST /v1/tracking-requests/{id}/refresh — an explicit, user-initiated lookup.
///
/// - 202 `result: "job_created"`        a new lookup job was created
/// - 200 `result: "job_already_active"` a lookup was already running; that job is returned
/// - 409 `shipment_archived`            the shipment is a closed generation of its number
///
/// `shipment_terminal` tells the client the shipment was DELIVERED or RETURNED when it asked.
/// Refreshing does not change the shipment status; only a processed provider result does.
pub async fn refresh(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Extension(CorrelationId(correlation_id)): Extension<CorrelationId>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let refreshed =
        dispatch::refresh(&state.pool, caller.user_id, parse_id(&id)?, correlation_id).await?;
    let job: Option<JobView> = tracking::latest_job(&state.pool, refreshed.shipment_id)
        .await?
        .map(Into::into);
    let status = match refreshed.result {
        RefreshResult::JobCreated => StatusCode::ACCEPTED,
        RefreshResult::JobAlreadyActive => StatusCode::OK,
    };
    let body = json!({
        "result": refreshed.result,
        "shipment_status": refreshed.shipment_status,
        "shipment_terminal": refreshed.shipment_status.is_terminal(),
        "job": job,
    });
    Ok((status, Json(body)).into_response())
}

/// DELETE /v1/tracking-requests/{id} — cancels the caller's subscription only.
pub async fn cancel(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Path(id): Path<String>,
) -> Result<StatusCode, ApiError> {
    if tracking::cancel_request(&state.pool, caller.user_id, parse_id(&id)?).await? {
        Ok(StatusCode::NO_CONTENT)
    } else {
        Err(NOT_FOUND)
    }
}
