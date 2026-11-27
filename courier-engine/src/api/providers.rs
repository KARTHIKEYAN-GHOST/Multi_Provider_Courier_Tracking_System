//! `/v1/providers`: the couriers, and adding one that has no adapter yet (Phase 12).
//!
//! Any signed-in user may add a courier. Its adapter is then built by the AI repair service;
//! tracking requests for it are accepted and wait until it is ready. A login given here is
//! stored encrypted as the caller's organization's own (an admin's: the platform's) and is
//! never returned by any endpoint.

use axum::{
    Extension, Json,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::{
    api::ApiJson,
    db,
    error::ApiError,
    services::{
        api_keys::Caller,
        credentials::{self, CredentialError, SecretFields},
        onboarding::{self, Adder, NewCourier, OnboardError},
    },
    state::AppState,
};

pub async fn list(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let providers = db::providers::list_enabled(&state.pool).await?;
    Ok(Json(json!({ "items": providers })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AddBody {
    code: Option<String>,
    name: String,
    method: String,
    tracking_url: String,
    #[serde(default)]
    hosts: Vec<String>,
    #[serde(default)]
    notes: String,
    /// For a website: its owner has permitted automatic lookups.
    #[serde(default)]
    permitted: bool,
    #[serde(default)]
    needs_login: bool,
    login: Option<SecretFields>,
    sample: Option<String>,
}

fn adder(caller: &Caller) -> Adder {
    Adder {
        user_id: Some(caller.user_id),
        organization_id: caller.organization_id,
        is_admin: caller.is_admin(),
    }
}

impl From<OnboardError> for ApiError {
    fn from(error: OnboardError) -> Self {
        match error {
            OnboardError::Invalid(problem) => {
                let field = problem.split(':').next().unwrap_or("body");
                ApiError::validation(field, problem)
            }
            OnboardError::PermissionRequired => ApiError::validation(
                "permitted",
                "a website is looked up automatically only with its owner's permission: \
                 set permitted to true once you have it",
            ),
            OnboardError::Exists(_) => ApiError::Conflict {
                code: "provider_exists",
                message: "a courier with this code exists already",
            },
            OnboardError::TooMany => ApiError::Conflict {
                code: "too_many_new_providers",
                message: "your organization has added the most couriers allowed for one day",
            },
            OnboardError::NoMasterKey => ApiError::Unavailable("credential store"),
            OnboardError::Credential(error) => error.into(),
            OnboardError::Database(error) => error.into(),
        }
    }
}

impl From<CredentialError> for ApiError {
    fn from(error: CredentialError) -> Self {
        match error {
            CredentialError::UnknownProvider => ApiError::NotFound("provider"),
            CredentialError::Database(error) => error.into(),
            // Field names or sizes only; a secret value is never part of the message.
            CredentialError::InvalidSecret(problem) => ApiError::validation("login", problem),
            CredentialError::Crypto(_) => {
                ApiError::Internal(anyhow::anyhow!("credential encryption failed"))
            }
        }
    }
}

/// POST /v1/providers: adds a courier; its adapter is built by itself.
pub async fn add(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    ApiJson(body): ApiJson<AddBody>,
) -> Result<Response, ApiError> {
    let added = onboarding::add_courier(
        &state.pool,
        state.secrets.as_deref(),
        adder(&caller),
        NewCourier {
            code: body.code,
            name: body.name,
            method: body.method,
            tracking_url: body.tracking_url,
            hosts: body.hosts,
            notes: body.notes,
            permitted: body.permitted,
            needs_login: body.needs_login,
            login: body.login,
            sample: body.sample,
        },
    )
    .await?;
    let body = json!({ "code": added.code, "status": "building" });
    Ok((StatusCode::ACCEPTED, Json(body)).into_response())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoginBody {
    login: SecretFields,
    label: Option<String>,
}

/// PUT /v1/providers/{code}/login: stores the caller's organization's own login for a courier
/// (an admin's: the platform's login), replacing the previous one.
pub async fn set_login(
    State(state): State<AppState>,
    Extension(caller): Extension<Caller>,
    Path(code): Path<String>,
    ApiJson(body): ApiJson<LoginBody>,
) -> Result<StatusCode, ApiError> {
    let Some(store) = state.secrets.as_deref() else {
        return Err(ApiError::Unavailable("credential store"));
    };
    let organization = if caller.is_admin() {
        None
    } else {
        caller.organization_id
    };
    let actor = if caller.is_admin() { "admin" } else { "user" };
    let label = body.label.unwrap_or_else(|| "login".into());
    if label.is_empty() || label.len() > 100 {
        return Err(ApiError::validation("label", "1 to 100 characters"));
    }
    credentials::set_credential_for(
        &state.pool,
        store,
        &code,
        &label,
        &body.login,
        organization,
        actor,
    )
    .await?;
    Ok(StatusCode::NO_CONTENT)
}
