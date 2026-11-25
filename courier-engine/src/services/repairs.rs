//! Phase 9: failure samples and repair tasks.
//!
//! When adapters keep failing to read a provider's answer, the Engine opens a *repair task*.
//! A separate program, the AI repair service, claims the task over the internal API, changes
//! the adapter's code elsewhere and reports back. An operator can also ask for an adapter
//! for a provider that has none yet.
//!
//! The Engine's part ends there: it runs no AI, executes no generated code and gives the
//! repair service nothing but the task and the answers the adapter could not read.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use super::credentials::{Audit, audit};
use crate::{
    domain::{ErrorClass, JobOutcome},
    messaging::contracts::JobResult,
};

/// Unreadable answers for this many different lookups of one adapter version, within
/// `WINDOW_HOURS`, open a repair task. One odd shipment is not a broken adapter.
pub const FAILURE_THRESHOLD: i64 = 3;
pub const WINDOW_HOURS: i32 = 24;
/// After a repair attempt failed, the same adapter is not tried again for this long.
pub const RETRY_AFTER_HOURS: i32 = 6;
/// A claimed task whose service went silent is handed out again after this long.
pub const CLAIM_TIMEOUT_HOURS: i32 = 2;
/// Samples kept per adapter, and handed to the repair service with a task.
const SAMPLES_KEPT: i64 = 10;
const SAMPLES_PER_TASK: i64 = 3;

const MAX_NOTES: usize = 4000;
const MAX_SAMPLE: usize = 100_000;

/// Records the answer of a failed execution if it is one the adapter could not read, and
/// opens a repair task when such failures have piled up. Called for accepted results only.
pub async fn record_failure(
    conn: &mut PgConnection,
    provider_id: Uuid,
    p: &JobResult,
) -> Result<(), sqlx::Error> {
    let (Some(error), Some(raw)) = (&p.error, &p.raw_response) else {
        return Ok(());
    };
    if p.outcome != JobOutcome::Failure
        || !matches!(
            error.class,
            ErrorClass::ParseError | ErrorClass::SchemaDrift
        )
    {
        return Ok(());
    }
    let inserted = sqlx::query(
        "INSERT INTO adapter_failure_samples
             (id, adapter_key, adapter_version, provider_id, job_id, execution_id, error_class,
              error_message, tracking_number, content_type, body)
         SELECT $1, $2, $3, $4, j.id, $6, $7, $8, s.tracking_number, $9, $10
           FROM tracking_jobs j JOIN shipments s ON s.id = j.shipment_id WHERE j.id = $5
         ON CONFLICT (execution_id) DO NOTHING",
    )
    .bind(Uuid::now_v7())
    .bind(&p.adapter_key)
    .bind(&p.adapter_version)
    .bind(provider_id)
    .bind(p.job_id)
    .bind(p.execution_id)
    .bind(class_name(error.class))
    .bind(&error.message)
    .bind(&raw.content_type)
    .bind(&raw.body)
    .execute(&mut *conn)
    .await?
    .rows_affected();
    if inserted == 0 {
        return Ok(());
    }
    sqlx::query(
        "DELETE FROM adapter_failure_samples WHERE adapter_key = $1 AND id NOT IN
             (SELECT id FROM adapter_failure_samples WHERE adapter_key = $1
               ORDER BY created_at DESC, id DESC LIMIT $2)",
    )
    .bind(&p.adapter_key)
    .bind(SAMPLES_KEPT)
    .execute(&mut *conn)
    .await?;

    let opened: Option<Uuid> = sqlx::query_scalar(
        "INSERT INTO repair_tasks (id, kind, adapter_key, adapter_version)
         SELECT $1, 'repair', $2, $3
          WHERE (SELECT count(DISTINCT job_id) FROM adapter_failure_samples
                  WHERE adapter_key = $2 AND adapter_version = $3
                    AND created_at > now() - make_interval(hours => $5)) >= $4
            AND NOT EXISTS (
                SELECT 1 FROM repair_tasks t WHERE t.adapter_key = $2 AND (
                       t.status IN ('pending', 'running')
                    OR (t.status = 'published' AND t.adapter_version = $3)
                    OR (t.status = 'failed' AND t.finished_at > now() - make_interval(hours => $6))))
         ON CONFLICT DO NOTHING
         RETURNING id",
    )
    .bind(Uuid::now_v7())
    .bind(&p.adapter_key)
    .bind(&p.adapter_version)
    .bind(FAILURE_THRESHOLD)
    .bind(WINDOW_HOURS)
    .bind(RETRY_AFTER_HOURS)
    .fetch_optional(&mut *conn)
    .await?;
    if let Some(task_id) = opened {
        tracing::warn!(adapter = %p.adapter_key, adapter_version = %p.adapter_version, %task_id,
            "adapter keeps failing to read the provider's answer; repair task opened");
        audit(
            conn,
            Audit {
                actor_type: "engine",
                action: "repair.opened",
                allowed: true,
                provider_id: Some(provider_id),
                details: Some(json!({ "task_id": task_id, "adapter_key": p.adapter_key,
                                      "adapter_version": p.adapter_version })),
                ..Audit::default()
            },
        )
        .await?;
    }
    Ok(())
}

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct Sample {
    pub id: Uuid,
    pub adapter_version: String,
    pub error_class: String,
    pub error_message: String,
    pub tracking_number: String,
    pub content_type: String,
    pub body: Value,
}

/// A task as handed to the repair service.
#[derive(Debug, Serialize)]
pub struct ClaimedTask {
    pub id: Uuid,
    pub kind: String,
    pub adapter_key: String,
    pub adapter_version: Option<String>,
    pub attempts: i32,
    pub request: Value,
    pub samples: Vec<Sample>,
}

/// Hands out the oldest waiting task (or one whose service went silent) and marks it running.
pub async fn claim(pool: &PgPool) -> Result<Option<ClaimedTask>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    let row: Option<(Uuid, String, String, Option<String>, i32, Value)> = sqlx::query_as(
        "UPDATE repair_tasks SET status = 'running', claimed_at = now(), updated_at = now(),
                                 attempts = attempts + 1
          WHERE id = (SELECT id FROM repair_tasks
                       WHERE status = 'pending'
                          OR (status = 'running' AND claimed_at < now() - make_interval(hours => $1))
                       ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
         RETURNING id, kind, adapter_key, adapter_version, attempts, request",
    )
    .bind(CLAIM_TIMEOUT_HOURS)
    .fetch_optional(&mut *tx)
    .await?;
    let Some((id, kind, adapter_key, adapter_version, attempts, request)) = row else {
        return Ok(None);
    };
    let samples = sqlx::query_as::<_, Sample>(
        "SELECT id, adapter_version, error_class, error_message, tracking_number, content_type, body
           FROM adapter_failure_samples WHERE adapter_key = $1
          ORDER BY created_at DESC, id DESC LIMIT $2",
    )
    .bind(&adapter_key)
    .bind(SAMPLES_PER_TASK)
    .fetch_all(&mut *tx)
    .await?;
    audit(
        &mut tx,
        Audit {
            actor_type: "worker",
            actor_id: Some("ai-repair".into()),
            action: "repair.claimed",
            allowed: true,
            details: Some(json!({ "task_id": id, "adapter_key": adapter_key, "kind": kind })),
            ..Audit::default()
        },
    )
    .await?;
    tx.commit().await?;
    Ok(Some(ClaimedTask {
        id,
        kind,
        adapter_key,
        adapter_version,
        attempts,
        request,
        samples,
    }))
}

/// What the repair service reports when it is done with a task.
#[derive(Debug, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum Finish {
    Published { commit_sha: String, summary: String },
    Failed { reason: String },
}

/// Closes a running task. Returns false when there is no such running task.
pub async fn finish(pool: &PgPool, id: Uuid, outcome: &Finish) -> Result<bool, sqlx::Error> {
    let (status, commit_sha, summary, reason) = match outcome {
        Finish::Published {
            commit_sha,
            summary,
        } => (
            "published",
            Some(clip(commit_sha, 64)),
            Some(clip(summary, 2000)),
            None,
        ),
        Finish::Failed { reason } => ("failed", None, None, Some(clip(reason, 2000))),
    };
    let mut tx = pool.begin().await?;
    let adapter_key: Option<String> = sqlx::query_scalar(
        "UPDATE repair_tasks SET status = $2, commit_sha = $3, summary = $4, failure_reason = $5,
                                 finished_at = now(), updated_at = now()
          WHERE id = $1 AND status = 'running' RETURNING adapter_key",
    )
    .bind(id)
    .bind(status)
    .bind(&commit_sha)
    .bind(&summary)
    .bind(&reason)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(adapter_key) = adapter_key else {
        return Ok(false);
    };
    audit(
        &mut tx,
        Audit {
            actor_type: "worker",
            actor_id: Some("ai-repair".into()),
            action: "repair.finished",
            allowed: status == "published",
            reason: (status == "failed").then_some("repair_failed"),
            details: Some(
                json!({ "task_id": id, "adapter_key": adapter_key, "commit_sha": commit_sha }),
            ),
            ..Audit::default()
        },
    )
    .await?;
    tx.commit().await?;
    Ok(true)
}

/// The class as it is written in messages, e.g. `SCHEMA_DRIFT`.
fn class_name(class: ErrorClass) -> String {
    serde_json::to_value(class)
        .ok()
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_default()
}

fn clip(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

/// What an operator says about a provider that has no adapter yet.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NewProvider {
    pub code: String,
    pub name: String,
    /// `api` or `scrape`.
    pub method: String,
    /// The only hosts the new adapter may contact.
    pub hosts: Vec<String>,
    pub needs_credential: bool,
    /// How the provider is tracked, in the operator's words.
    pub notes: String,
    /// An example answer of the provider, if the operator has one.
    pub sample: Option<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum RequestError {
    #[error("{0}")]
    Invalid(&'static str),
    #[error("a task for this adapter is already waiting or running")]
    AlreadyOpen,
    #[error(transparent)]
    Database(#[from] sqlx::Error),
}

fn valid_code(code: &str) -> bool {
    let mut chars = code.chars();
    code.len() <= 30
        && chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
}

fn valid_host(host: &str) -> bool {
    host.len() <= 253
        && host.contains('.')
        && host.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && !label.starts_with('-')
                && !label.ends_with('-')
                && label
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        })
}

async fn open(
    pool: &PgPool,
    kind: &str,
    adapter_key: &str,
    request: Value,
) -> Result<Uuid, RequestError> {
    let id = Uuid::now_v7();
    let mut tx = pool.begin().await?;
    let inserted = sqlx::query(
        "INSERT INTO repair_tasks (id, kind, adapter_key, request) VALUES ($1, $2, $3, $4)
         ON CONFLICT DO NOTHING",
    )
    .bind(id)
    .bind(kind)
    .bind(adapter_key)
    .bind(&request)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if inserted == 0 {
        return Err(RequestError::AlreadyOpen);
    }
    audit(
        &mut tx,
        Audit {
            actor_type: "admin",
            action: "repair.requested",
            allowed: true,
            details: Some(json!({ "task_id": id, "adapter_key": adapter_key, "kind": kind })),
            ..Audit::default()
        },
    )
    .await?;
    tx.commit().await?;
    Ok(id)
}

/// Asks for an adapter for a provider the system does not know yet.
pub async fn request_new_provider(pool: &PgPool, spec: &NewProvider) -> Result<Uuid, RequestError> {
    if !valid_code(&spec.code) {
        return Err(RequestError::Invalid(
            "code: 1-30 lowercase letters and digits, starting with a letter",
        ));
    }
    if spec.name.trim().is_empty()
        || spec.name.len() > 100
        || spec.name.contains(['\'', '"', '\\', '\n'])
    {
        return Err(RequestError::Invalid(
            "name: 1-100 characters, without quotes, backslashes or line breaks",
        ));
    }
    if !matches!(spec.method.as_str(), "api" | "scrape") {
        return Err(RequestError::Invalid("method: api or scrape"));
    }
    if spec.hosts.is_empty() || spec.hosts.len() > 5 || !spec.hosts.iter().all(|h| valid_host(h)) {
        return Err(RequestError::Invalid(
            "hosts: one to five host names such as api.example.com (no scheme, no path)",
        ));
    }
    if spec.notes.trim().is_empty() || spec.notes.len() > MAX_NOTES {
        return Err(RequestError::Invalid(
            "notes: say how the provider is tracked (at most 4000 characters)",
        ));
    }
    if spec.sample.as_ref().is_some_and(|s| s.len() > MAX_SAMPLE) {
        return Err(RequestError::Invalid("sample: at most 100000 characters"));
    }
    let exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM adapters WHERE key = $1)
             OR EXISTS (SELECT 1 FROM providers WHERE code = $1)",
    )
    .bind(&spec.code)
    .fetch_one(pool)
    .await?;
    if exists {
        return Err(RequestError::Invalid(
            "a provider or adapter with this code already exists",
        ));
    }
    open(pool, "new_provider", &spec.code, json!(spec)).await
}

/// Asks for a repair of an existing adapter without waiting for the failure threshold.
/// There must be at least one stored answer the adapter could not read.
pub async fn request_repair(pool: &PgPool, adapter_key: &str) -> Result<Uuid, RequestError> {
    let version: Option<String> = sqlx::query_scalar(
        "SELECT adapter_version FROM adapter_failure_samples WHERE adapter_key = $1
          ORDER BY created_at DESC, id DESC LIMIT 1",
    )
    .bind(adapter_key)
    .fetch_optional(pool)
    .await?;
    let Some(version) = version else {
        return Err(RequestError::Invalid(
            "no unreadable answer is stored for this adapter, so there is nothing to repair from",
        ));
    };
    let id = open(pool, "repair", adapter_key, json!({})).await?;
    sqlx::query("UPDATE repair_tasks SET adapter_version = $2 WHERE id = $1")
        .bind(id)
        .bind(version)
        .execute(pool)
        .await?;
    Ok(id)
}

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct TaskRow {
    pub id: Uuid,
    pub kind: String,
    pub adapter_key: String,
    pub adapter_version: Option<String>,
    pub status: String,
    pub attempts: i32,
    pub commit_sha: Option<String>,
    pub summary: Option<String>,
    pub failure_reason: Option<String>,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub finished_at: Option<chrono::DateTime<chrono::Utc>>,
}

/// The most recent tasks, newest first.
pub async fn list(pool: &PgPool, limit: i64) -> Result<Vec<TaskRow>, sqlx::Error> {
    sqlx::query_as(
        "SELECT id, kind, adapter_key, adapter_version, status, attempts, commit_sha, summary,
                failure_reason, created_at, finished_at
           FROM repair_tasks ORDER BY created_at DESC, id DESC LIMIT $1",
    )
    .bind(limit)
    .fetch_all(pool)
    .await
}
