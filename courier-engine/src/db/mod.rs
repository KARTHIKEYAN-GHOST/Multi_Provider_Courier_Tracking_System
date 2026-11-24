//! Database access. All SQL lives in this module and in `services`; handlers never build queries.

use sqlx::{PgPool, postgres::PgPoolOptions};

/// The non-terminal ("active") job statuses, as a SQL list. This is the ONLY place the list is
/// written in Rust SQL. It must be identical to the predicate of the `ux_tracking_jobs_one_active`
/// index (PostgreSQL only uses that index for `ON CONFLICT` when the predicates match) and to
/// `ExecutionStatus::is_terminal`; `tests/contracts.rs` fails if any of the three drift apart.
macro_rules! active_job_statuses {
    () => {
        "('PENDING', 'QUEUED', 'PROCESSING', 'WAITING_FOR_OTP', 'WAITING_FOR_CAPTCHA', 'RETRYING')"
    };
}

pub const ACTIVE_JOB_STATUSES_SQL: &str = active_job_statuses!();

pub mod api_keys;
pub mod providers;
pub mod tracking;

/// Migrations are compiled into the binary and applied with `courier-engine migrate`.
pub static MIGRATOR: sqlx::migrate::Migrator = sqlx::migrate!("./migrations");

pub async fn connect(database_url: &str, max_connections: u32) -> Result<PgPool, sqlx::Error> {
    PgPoolOptions::new()
        .max_connections(max_connections)
        .acquire_timeout(std::time::Duration::from_secs(10))
        .connect(database_url)
        .await
}

/// PostgreSQL error code for a unique-constraint violation.
pub fn is_unique_violation(error: &sqlx::Error) -> bool {
    matches!(error, sqlx::Error::Database(db) if db.code().as_deref() == Some("23505"))
}
