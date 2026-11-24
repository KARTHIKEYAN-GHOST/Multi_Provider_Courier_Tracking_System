//! Shared helpers for integration tests.
//!
//! Each test gets its own freshly migrated database from `#[sqlx::test]`, and talks to the
//! real router in-process (no network port), so tests exercise routing, authentication,
//! validation, SQL and constraints exactly as production does.
#![allow(dead_code)]

use axum::{
    Router,
    body::Body,
    http::{HeaderMap, Method, Request, StatusCode, header},
};
use courier_engine::{api, config::Config, db, services::api_keys, state::AppState};
use http_body_util::BodyExt;
use serde_json::Value;
use sqlx::PgPool;
use tower::ServiceExt;
use uuid::Uuid;

pub fn app(pool: &PgPool) -> Router {
    api::router(AppState::new(pool.clone(), Config::for_tests()))
}

/// Creates a user with one API key. Returns (user id, full key).
pub async fn user(pool: &PgPool, email: &str) -> (Uuid, String) {
    let user_id = db::api_keys::create_user(pool, email).await.unwrap();
    let key = api_keys::issue(pool, user_id, Some("test")).await.unwrap();
    (user_id, key)
}

pub struct Reply {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: Value,
}

impl Reply {
    pub fn id(&self) -> String {
        self.body["id"]
            .as_str()
            .expect("response has an id")
            .to_string()
    }
}

pub async fn send(app: &Router, request: Request<Body>) -> Reply {
    let response = app.clone().oneshot(request).await.unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes)
            .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(&bytes).into_owned()))
    };
    Reply {
        status,
        headers,
        body,
    }
}

pub async fn call(
    app: &Router,
    method: Method,
    path: &str,
    key: Option<&str>,
    body: Option<Value>,
) -> Reply {
    let mut builder = Request::builder().method(method).uri(path);
    if let Some(key) = key {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {key}"));
    }
    let request = match body {
        Some(json) => builder
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(json.to_string()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    send(app, request).await
}

pub async fn get(app: &Router, path: &str, key: &str) -> Reply {
    call(app, Method::GET, path, Some(key), None).await
}

pub async fn track(app: &Router, key: &str, tracking_number: &str) -> Reply {
    call(
        app,
        Method::POST,
        "/v1/tracking-requests",
        Some(key),
        Some(serde_json::json!({ "provider_code": "dummy", "tracking_number": tracking_number })),
    )
    .await
}

pub async fn count(pool: &PgPool, table: &'static str) -> i64 {
    let sql = match table {
        "shipments" => "SELECT count(*) FROM shipments",
        "active_shipments" => "SELECT count(*) FROM shipments WHERE archived_at IS NULL",
        "tracking_requests" => "SELECT count(*) FROM tracking_requests",
        "active_requests" => "SELECT count(*) FROM tracking_requests WHERE status = 'active'",
        "tracking_jobs" => "SELECT count(*) FROM tracking_jobs",
        "job_attempts" => "SELECT count(*) FROM job_attempts",
        "outbox_messages" => "SELECT count(*) FROM outbox_messages",
        other => panic!("unknown table {other}"),
    };
    sqlx::query_scalar::<_, i64>(sql)
        .fetch_one(pool)
        .await
        .unwrap()
}

/// Simulates a finished lookup, which Phase 3 will do through real result messages.
pub async fn complete_all_jobs(pool: &PgPool) {
    sqlx::query(
        "UPDATE tracking_jobs SET status = 'COMPLETED', outcome = 'SUCCESS', completed_at = now()",
    )
    .execute(pool)
    .await
    .unwrap();
}

/// Inserts one tracking event directly, standing in for the result handler of Phase 3.
pub async fn add_event(pool: &PgPool, tracking_number: &str, raw_status: &str, normalized: &str) {
    sqlx::query(
        "INSERT INTO tracking_events (id, shipment_id, raw_status, raw_description, normalized_status, occurred_at, dedupe_key)
         SELECT $1, s.id, $2, 'test event', $3::shipment_status, now(), $2
         FROM shipments s WHERE s.tracking_number = $4 AND s.archived_at IS NULL",
    )
    .bind(Uuid::now_v7())
    .bind(raw_status)
    .bind(normalized)
    .bind(tracking_number)
    .execute(pool)
    .await
    .unwrap();
}
