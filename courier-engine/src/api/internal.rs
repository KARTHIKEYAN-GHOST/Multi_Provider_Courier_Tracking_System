//! Internal HTTP API, for adapter workers only.
//!
//! It listens on its own address (`ENGINE_INTERNAL_ADDR`), which must never be reachable from
//! outside the deployment. Its single purpose is to hand a worker the credential for a job
//! it is executing. Callers authenticate with a worker token, not a user API key.

use axum::{
    Extension, Json, Router,
    extract::{Request, State},
    http::{HeaderValue, StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::post,
};
use serde::Deserialize;
use serde_json::json;
use uuid::Uuid;

use super::{ApiJson, request_context};
use crate::{
    error::ApiError,
    services::{
        credentials::{self, ResolveRequest, Resolved},
        interventions::{self, ResponseRequest, ResponseResolved},
        repairs::{self, Finish},
        worker_tokens::{self, WorkerIdentity},
    },
    state::AppState,
};

pub fn router(state: AppState) -> Router {
    // The AI repair service has its own token and reaches these two routes only.
    let repair = Router::new()
        .route("/internal/v1/repairs/claim", post(claim_repair))
        .route("/internal/v1/repairs/{id}/result", post(finish_repair))
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            require_repair_token,
        ));
    Router::new()
        .route("/internal/v1/credentials/resolve", post(resolve))
        .route(
            "/internal/v1/interventions/resolve",
            post(resolve_intervention),
        )
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            require_worker_token,
        ))
        .merge(repair)
        .fallback(|| async { ApiError::NotFound("resource") })
        .layer(middleware::from_fn_with_state(
            state.clone(),
            request_context,
        ))
        .with_state(state)
}

async fn require_worker_token(
    State(state): State<AppState>,
    mut request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let presented = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::trim)
        .ok_or(ApiError::Unauthorized)?;
    let worker = worker_tokens::authenticate(&state.pool, presented)
        .await?
        .ok_or(ApiError::Unauthorized)?;
    request.extensions_mut().insert(worker);
    Ok(next.run(request).await)
}

#[derive(Debug, Deserialize)]
struct ResolveBody {
    credential_ref: Uuid,
    job_id: Uuid,
    attempt: i32,
}

/// POST /internal/v1/credentials/resolve
///
/// Every refusal looks the same to the caller (403 `resolve_denied`); the actual reason is
/// in the audit log. The secret is returned with `Cache-Control: no-store` and is not logged
/// (the Engine never logs request or response bodies).
async fn resolve(
    State(state): State<AppState>,
    Extension(worker): Extension<WorkerIdentity>,
    ApiJson(body): ApiJson<ResolveBody>,
) -> Result<Response, ApiError> {
    let Some(store) = state.secrets.as_deref() else {
        return Err(ApiError::Unavailable("credential store"));
    };
    let request = ResolveRequest {
        credential_ref: body.credential_ref,
        job_id: body.job_id,
        attempt: body.attempt,
    };
    let resolved = credentials::resolve(&state.pool, store, &worker, request)
        .await
        .map_err(|e| ApiError::Internal(anyhow::Error::new(e)))?;
    match resolved {
        Resolved::Secret(secret) => {
            let mut response = (StatusCode::OK, Json(json!({ "secret": secret }))).into_response();
            response
                .headers_mut()
                .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
            Ok(response)
        }
        Resolved::Denied(_) => Err(ApiError::Forbidden {
            code: "resolve_denied",
            message: "the credential cannot be resolved for this job",
        }),
    }
}

#[derive(Debug, Deserialize)]
struct ResolveInterventionBody {
    intervention_id: Uuid,
    /// The session the worker is about to continue; must be the one this challenge was raised with.
    session_ref: Uuid,
    job_id: Uuid,
    attempt: i32,
}

/// POST /internal/v1/interventions/resolve
///
/// Hands the worker the user's answer for the job it is resuming. As with credentials, every
/// refusal is the same 403 and the reason is only in the audit log.
async fn resolve_intervention(
    State(state): State<AppState>,
    Extension(worker): Extension<WorkerIdentity>,
    ApiJson(body): ApiJson<ResolveInterventionBody>,
) -> Result<Response, ApiError> {
    let Some(store) = state.secrets.as_deref() else {
        return Err(ApiError::Unavailable("answer store"));
    };
    let request = ResponseRequest {
        intervention_id: body.intervention_id,
        session_ref: body.session_ref,
        job_id: body.job_id,
        attempt: body.attempt,
    };
    match interventions::resolve_response(&state.pool, store.master(), &worker, request).await? {
        ResponseResolved::Response(answer) => {
            let mut response =
                (StatusCode::OK, Json(json!({ "response": answer }))).into_response();
            response
                .headers_mut()
                .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
            Ok(response)
        }
        ResponseResolved::Denied(_) => Err(ApiError::Forbidden {
            code: "resolve_denied",
            message: "the answer cannot be resolved for this job",
        }),
    }
}

/// The repair service's token is a configured secret, compared without leaking by timing
/// where the two differ. With no token configured, nobody passes.
async fn require_repair_token(
    State(state): State<AppState>,
    request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    use sha2::{Digest, Sha256};
    use subtle::ConstantTimeEq;

    let presented = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::trim)
        .ok_or(ApiError::Unauthorized)?;
    let expected = state
        .config
        .engine_repair_token
        .as_deref()
        .filter(|token| token.len() >= 32)
        .ok_or(ApiError::Unauthorized)?;
    let same = Sha256::digest(presented.as_bytes())
        .as_slice()
        .ct_eq(Sha256::digest(expected.as_bytes()).as_slice());
    if !bool::from(same) {
        return Err(ApiError::Unauthorized);
    }
    Ok(next.run(request).await)
}

/// POST /internal/v1/repairs/claim
///
/// The next task with the answers the adapter could not read, or 204 when there is none.
async fn claim_repair(State(state): State<AppState>) -> Result<Response, ApiError> {
    Ok(match repairs::claim(&state.pool).await? {
        Some(task) => {
            let mut response = Json(task).into_response();
            response
                .headers_mut()
                .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
            response
        }
        None => StatusCode::NO_CONTENT.into_response(),
    })
}

/// POST /internal/v1/repairs/{id}/result
async fn finish_repair(
    State(state): State<AppState>,
    axum::extract::Path(id): axum::extract::Path<Uuid>,
    ApiJson(outcome): ApiJson<Finish>,
) -> Result<Response, ApiError> {
    if repairs::finish(&state.pool, id, &outcome).await? {
        Ok(StatusCode::NO_CONTENT.into_response())
    } else {
        Err(ApiError::NotFound("repair task"))
    }
}
