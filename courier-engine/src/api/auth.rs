//! API-key authentication for every `/v1` route.

use axum::{
    extract::{Request, State},
    http::header::AUTHORIZATION,
    middleware::Next,
    response::Response,
};

use crate::{error::ApiError, services::api_keys, state::AppState};

/// Requires `Authorization: Bearer <api key>`. On success the authenticated
/// [`api_keys::Caller`] is attached to the request for handlers to read.
/// Missing, malformed, unknown and revoked keys all produce the same 401.
pub async fn require_api_key(
    State(state): State<AppState>,
    mut request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let presented = request
        .headers()
        .get(AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::trim)
        .ok_or(ApiError::Unauthorized)?;

    let caller = api_keys::authenticate(&state.pool, presented)
        .await?
        .ok_or(ApiError::Unauthorized)?;

    tracing::Span::current().record("user_id", tracing::field::display(caller.user_id));
    request.extensions_mut().insert(caller);
    Ok(next.run(request).await)
}
