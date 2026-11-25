//! Public HTTP API.

pub mod auth;
pub mod dto;
pub mod health;
pub mod internal;
pub mod interventions;
pub mod providers;
pub mod tracking_requests;

use std::time::{Duration, Instant};

use axum::{
    Json, Router,
    extract::{FromRequest, Request, State},
    http::{HeaderName, HeaderValue},
    middleware::{self, Next},
    response::Response,
    routing::{get, post},
};
use serde::de::DeserializeOwned;
use tracing::Instrument;
use uuid::Uuid;

use crate::{error::ApiError, state::AppState};

pub static CORRELATION_HEADER: HeaderName = HeaderName::from_static("x-correlation-id");

/// Identifies one API request across logs and every message it causes.
#[derive(Debug, Clone, Copy)]
pub struct CorrelationId(pub Uuid);

pub fn router(state: AppState) -> Router {
    // Every /v1 route sits behind the API-key check. It is applied as a layer on the group,
    // so a handler cannot accidentally be left unauthenticated.
    let v1 = Router::new()
        .route("/providers", get(providers::list))
        .route(
            "/tracking-requests",
            post(tracking_requests::create).get(tracking_requests::list),
        )
        .route(
            "/tracking-requests/{id}",
            get(tracking_requests::get_one).delete(tracking_requests::cancel),
        )
        .route(
            "/tracking-requests/{id}/events",
            get(tracking_requests::events),
        )
        .route("/tracking-requests/{id}/jobs", get(tracking_requests::jobs))
        .route(
            "/tracking-requests/{id}/refresh",
            post(tracking_requests::refresh),
        )
        .route("/interventions", get(interventions::list))
        .route("/interventions/{id}", get(interventions::get_one))
        .route("/interventions/{id}/response", post(interventions::respond))
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            auth::require_api_key,
        ));

    Router::new()
        .route("/healthz", get(health::healthz))
        .route("/readyz", get(health::readyz))
        .nest("/v1", v1)
        .fallback(|| async { ApiError::NotFound("resource") })
        .layer(middleware::from_fn_with_state(
            state.clone(),
            request_context,
        ))
        .with_state(state)
}

/// Outermost middleware: assigns the correlation ID, enforces the request timeout, logs one
/// line per request, and returns the correlation ID to the client.
///
/// A caller may supply its own `X-Correlation-Id` (must be a UUID); otherwise one is generated.
/// Request headers and bodies are never logged, so API keys cannot leak into logs.
pub(crate) async fn request_context(
    State(state): State<AppState>,
    mut request: Request,
    next: Next,
) -> Response {
    let correlation_id = request
        .headers()
        .get(&CORRELATION_HEADER)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| Uuid::parse_str(v).ok())
        .unwrap_or_else(Uuid::now_v7);
    request
        .extensions_mut()
        .insert(CorrelationId(correlation_id));

    let span = tracing::info_span!(
        "http_request",
        correlation_id = %correlation_id,
        method = %request.method(),
        path = %request.uri().path(),
        user_id = tracing::field::Empty,
    );
    let started = Instant::now();
    let timeout = Duration::from_millis(state.config.request_timeout_ms);

    let mut response =
        match tokio::time::timeout(timeout, next.run(request).instrument(span.clone())).await {
            Ok(response) => response,
            Err(_) => axum::response::IntoResponse::into_response(ApiError::Timeout),
        };

    span.in_scope(|| {
        tracing::info!(
            status = response.status().as_u16(),
            elapsed_ms = started.elapsed().as_millis() as u64,
            "request completed"
        );
    });
    response.headers_mut().insert(
        CORRELATION_HEADER.clone(),
        HeaderValue::from_str(&correlation_id.to_string()).expect("a UUID is a valid header value"),
    );
    response
}

/// JSON body extractor that reports malformed or mistyped bodies in the API's own error
/// shape (422) instead of axum's default plain-text responses.
///
/// The body must be a JSON object. serde would otherwise also accept a JSON array as a
/// positional form of a struct (`["dummy", "X1"]`), which is not part of this API.
pub struct ApiJson<T>(pub T);

impl<S, T> FromRequest<S> for ApiJson<T>
where
    S: Send + Sync,
    T: DeserializeOwned,
{
    type Rejection = ApiError;

    async fn from_request(request: Request, state: &S) -> Result<Self, Self::Rejection> {
        let Json(value) = Json::<serde_json::Value>::from_request(request, state)
            .await
            .map_err(|rejection| ApiError::validation("body", rejection.body_text()))?;
        if !value.is_object() {
            return Err(ApiError::validation("body", "must be a JSON object"));
        }
        serde_json::from_value::<T>(value)
            .map(ApiJson)
            .map_err(|e| ApiError::validation("body", e.to_string()))
    }
}
