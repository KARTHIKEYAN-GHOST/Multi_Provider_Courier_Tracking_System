//! API keys must never be written to logs (acceptance criterion 5).
//!
//! Kept in its own test binary: it installs a process-wide log subscriber that captures
//! everything the Engine logs, at the most verbose level, including SQL statements.

mod common;

use std::sync::{Arc, Mutex};

use axum::http::StatusCode;
use common::{app, get, user};
use courier_engine::db;
use sqlx::PgPool;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;

#[derive(Clone, Default)]
struct Captured(Arc<Mutex<Vec<u8>>>);

impl std::io::Write for Captured {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Captured {
    type Writer = Captured;
    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn api_keys_never_appear_in_logs(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);

    let captured = Captured::default();
    let subscriber = tracing_subscriber::fmt()
        .json()
        .with_max_level(tracing::Level::TRACE)
        .with_writer(captured.clone())
        .finish();
    // Global, not thread-local: this file contains a single test, so nothing else can
    // interfere with which subscriber receives the Engine's log events.
    tracing::subscriber::set_global_default(subscriber)
        .expect("no other subscriber in this test binary");

    // A successful call, a rejected call with a tampered key, and a validation failure.
    assert_eq!(
        common::track(&app, &key, "DUMMY-OK-1").await.status,
        StatusCode::CREATED
    );
    assert_eq!(
        get(&app, "/v1/providers", &format!("{key}x")).await.status,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        common::track(&app, &key, "").await.status,
        StatusCode::UNPROCESSABLE_ENTITY
    );

    let logs = String::from_utf8(captured.0.lock().unwrap().clone()).unwrap();
    assert!(
        logs.contains("request completed"),
        "expected request logs to be captured, got: {logs}"
    );
    let secret = key.rsplit('_').next().unwrap();
    assert!(!logs.contains(&key), "full API key found in logs");
    assert!(!logs.contains(secret), "API key secret found in logs");
    assert!(
        !logs.to_lowercase().contains("bearer"),
        "Authorization header found in logs"
    );
}
