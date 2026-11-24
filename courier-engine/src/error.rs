//! One error type for the HTTP API. Every error is rendered in the same JSON shape:
//! `{"error": {"code": "...", "message": "...", "details": ...}}`.

use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};

#[derive(Debug, thiserror::Error)]
pub enum ApiError {
    #[error("authentication required")]
    Unauthorized,
    /// Also returned for resources that exist but belong to another user, so that
    /// request IDs cannot be probed.
    #[error("{0} not found")]
    NotFound(&'static str),
    #[error("{message}")]
    Validation { message: String, details: Value },
    /// The request is well-formed but cannot be carried out in the resource's current state.
    #[error("{message}")]
    Conflict {
        code: &'static str,
        message: &'static str,
    },
    /// The caller is known but may not do this.
    #[error("{message}")]
    Forbidden {
        code: &'static str,
        message: &'static str,
    },
    #[error("request timed out")]
    Timeout,
    #[error("service unavailable: {0}")]
    Unavailable(&'static str),
    #[error(transparent)]
    Internal(#[from] anyhow::Error),
}

impl ApiError {
    pub fn validation(field: &str, problem: impl Into<String>) -> Self {
        let problem = problem.into();
        ApiError::Validation {
            message: format!("{field}: {problem}"),
            details: json!({ "field": field, "problem": problem }),
        }
    }
}

impl From<sqlx::Error> for ApiError {
    fn from(e: sqlx::Error) -> Self {
        ApiError::Internal(anyhow::Error::new(e))
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, code, message, details) = match &self {
            ApiError::Unauthorized => (
                StatusCode::UNAUTHORIZED,
                "unauthorized",
                self.to_string(),
                Value::Null,
            ),
            ApiError::NotFound(_) => (
                StatusCode::NOT_FOUND,
                "not_found",
                self.to_string(),
                Value::Null,
            ),
            ApiError::Validation { details, .. } => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "validation_failed",
                self.to_string(),
                details.clone(),
            ),
            ApiError::Conflict { code, .. } => {
                (StatusCode::CONFLICT, *code, self.to_string(), Value::Null)
            }
            ApiError::Forbidden { code, .. } => {
                (StatusCode::FORBIDDEN, *code, self.to_string(), Value::Null)
            }
            ApiError::Timeout => (
                StatusCode::GATEWAY_TIMEOUT,
                "timeout",
                self.to_string(),
                Value::Null,
            ),
            ApiError::Unavailable(_) => (
                StatusCode::SERVICE_UNAVAILABLE,
                "unavailable",
                self.to_string(),
                Value::Null,
            ),
            ApiError::Internal(e) => {
                // The cause is logged, never sent to the client.
                tracing::error!(error = ?e, "internal error");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "internal error".to_string(),
                    Value::Null,
                )
            }
        };
        let body = json!({ "error": { "code": code, "message": message, "details": details } });
        (status, Json(body)).into_response()
    }
}
