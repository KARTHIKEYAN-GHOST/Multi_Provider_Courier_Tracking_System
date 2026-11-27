//! Human interventions: a lookup that stops for a one-time code or a CAPTCHA answer.
//!
//! 1. The worker reports `job.intervention_required`. The job leaves PROCESSING for
//!    WAITING_FOR_OTP / WAITING_FOR_CAPTCHA and a challenge row is created.
//!    Nothing is held open while waiting: the worker has saved its session and moved on.
//! 2. An admin answers through the public API (organization users do not see challenges). The answer is stored encrypted, the job goes
//!    back to QUEUED (same attempt) and `job.resume` is sent. The answer is not in the message.
//! 3. The worker fetches the answer over the internal API — allowed only for that job and
//!    attempt while it is in flight — and continues. A result closes the attempt as usual;
//!    another challenge (wrong code) starts at 1 again, up to the provider's `max_challenges`.
//! 4. A challenge nobody answers in time ends the job as EXPIRED.
//!
//! The answer is deleted as soon as the lookup has gone on with it or the challenge is over.

use base64::Engine as _;
use chrono::{DateTime, Duration, Utc};
use serde_json::{Value, json};
use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use super::close_attempt::{ClosedBy, Closure, RetryPolicy, close_attempt};
use super::credentials::{Audit, audit};
use super::crypto::{MasterKey, Sealed};
use super::events::{Handled, count_execution, lock_job_attempt};
use super::worker_tokens::WorkerIdentity;
use crate::db;
use crate::domain::ErrorClass;
use crate::messaging::{
    contracts::{Envelope, InterventionRequired, JOB_RESUME, JobTrack},
    outbox, routing,
};

/// Wrong-format answers tolerated per challenge before it stops accepting any.
pub const MAX_SUBMIT_FAILURES: i32 = 5;
/// What a closed challenge no longer needs: the stored answer and the CAPTCHA picture.
const WIPE: &str = "wrapped_key = NULL, key_nonce = NULL, ciphertext = NULL, nonce = NULL, \
     key_version = NULL, image = NULL, image_content_type = NULL";

fn binding(intervention_id: Uuid, job_id: Uuid) -> Vec<u8> {
    format!("intervention:{intervention_id}:job:{job_id}").into_bytes()
}

/// Ends every open challenge of a job with the given status and deletes any stored answer.
pub(crate) async fn close_open(
    conn: &mut PgConnection,
    job_id: Uuid,
    status: &'static str,
) -> Result<u64, sqlx::Error> {
    // WIPE and the statuses are constants of this module, never caller input.
    let sql = format!(
        "UPDATE human_interventions SET status = $2, closed_at = clock_timestamp(), {WIPE}
         WHERE job_id = $1 AND status IN ('pending', 'answered')"
    );
    Ok(sqlx::query(sqlx::AssertSqlSafe(sql))
        .bind(job_id)
        .bind(status)
        .execute(conn)
        .await?
        .rows_affected())
}

// ----------------------------------------------------------------------------- the worker asks

#[derive(sqlx::FromRow)]
struct OpenJob {
    challenge_count: i32,
    max_challenges: i32,
    intervention_ttl_s: i32,
}

/// Handles `job.intervention_required`. Idempotent and order-independent like the other
/// event handlers: it only acts on the job's current attempt while that attempt is running.
pub(crate) async fn on_intervention_required(
    conn: &mut PgConnection,
    envelope: &Envelope<InterventionRequired>,
    policy: RetryPolicy,
) -> Result<Handled, sqlx::Error> {
    let p = &envelope.payload;
    if !lock_job_attempt(conn, p.job_id, p.attempt).await? {
        return Ok(Handled::Rejected("unknown job or attempt".into()));
    }
    let image = match &p.image {
        Some(image) => match base64::engine::general_purpose::STANDARD.decode(&image.data) {
            Ok(bytes) => Some((image.content_type.as_str(), bytes)),
            Err(_) => {
                return Ok(Handled::Rejected(
                    "payload.image: data is not base64".into(),
                ));
            }
        },
        None => None,
    };

    // The execution that asked is over. Recorded once; a repeated report changes nothing.
    let recorded: Option<bool> = sqlx::query_scalar(
        "INSERT INTO adapter_execution_logs
             (execution_id, job_id, attempt, worker_id, adapter_version, delivery_count,
              started_at, finished_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
         ON CONFLICT (execution_id) DO UPDATE SET
             finished_at = EXCLUDED.finished_at,
             started_at = COALESCE(adapter_execution_logs.started_at, EXCLUDED.started_at)
         WHERE adapter_execution_logs.finished_at IS NULL
         RETURNING (xmax = 0)",
    )
    .bind(p.execution_id)
    .bind(p.job_id)
    .bind(p.attempt)
    .bind(&p.worker_id)
    .bind(&p.adapter_version)
    .bind(p.delivery_count)
    .bind(p.started_at)
    .bind(p.finished_at)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(inserted) = recorded else {
        return Ok(Handled::Processed);
    };
    if inserted {
        count_execution(conn, p.job_id, p.attempt).await?;
    }

    let job = sqlx::query_as::<_, OpenJob>(
        "SELECT j.challenge_count, p.max_challenges, p.intervention_ttl_s
         FROM tracking_jobs j
         JOIN shipments s ON s.id = j.shipment_id
         JOIN providers p ON p.id = s.provider_id
         WHERE j.id = $1 AND j.attempt = $2 AND j.status IN ('QUEUED', 'PROCESSING')",
    )
    .bind(p.job_id)
    .bind(p.attempt)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(job) = job else {
        // The attempt is closed, or already waiting for another execution's challenge.
        set_disposition(conn, p.execution_id, "stale").await?;
        return Ok(Handled::Processed);
    };

    // A challenge that follows an answered one means the provider refused that answer.
    close_open(conn, p.job_id, "rejected").await?;

    if job.challenge_count >= job.max_challenges {
        let closure = Closure::Failed {
            class: ErrorClass::ChallengeLimitExceeded,
            message: "the provider kept asking for a code or CAPTCHA; the limit for one job is reached",
            retryable: false,
            retry_after_ms: None,
            closed_by: ClosedBy::Result,
            execution_id: Some(p.execution_id),
        };
        close_attempt(conn, p.job_id, p.attempt, &closure, policy).await?;
        set_disposition(conn, p.execution_id, "accepted").await?;
        return Ok(Handled::Processed);
    }

    let wait_s = p
        .expires_in_s
        .map_or(job.intervention_ttl_s, |s| s.min(job.intervention_ttl_s));
    let status = if p.kind == "captcha" {
        "WAITING_FOR_CAPTCHA"
    } else {
        "WAITING_FOR_OTP"
    };
    let expires_at: DateTime<Utc> = sqlx::query_scalar(
        "UPDATE tracking_jobs SET
             status = $2::text::execution_status,
             intervention_deadline_at = clock_timestamp() + $3 * interval '1 second',
             execution_deadline_at = NULL,
             challenge_count = challenge_count + 1,
             updated_at = clock_timestamp()
         WHERE id = $1
         RETURNING intervention_deadline_at",
    )
    .bind(p.job_id)
    .bind(status)
    .bind(wait_s)
    .fetch_one(&mut *conn)
    .await?;

    sqlx::query(
        "INSERT INTO human_interventions
             (id, job_id, attempt, sequence, kind, prompt, image_content_type, image, response_format,
              session_ref, execution_id, expires_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
    )
    .bind(Uuid::now_v7())
    .bind(p.job_id)
    .bind(p.attempt)
    .bind(job.challenge_count + 1)
    .bind(&p.kind)
    .bind(&p.prompt)
    .bind(image.as_ref().map(|(content_type, _)| *content_type))
    .bind(image.as_ref().map(|(_, bytes)| bytes.as_slice()))
    .bind(p.response_format.as_ref().map(|f| json!(f)))
    .bind(p.session_ref)
    .bind(p.execution_id)
    .bind(expires_at)
    .execute(&mut *conn)
    .await?;
    set_disposition(conn, p.execution_id, "suspended").await?;
    Ok(Handled::Processed)
}

async fn set_disposition(
    conn: &mut PgConnection,
    execution_id: Uuid,
    disposition: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE adapter_execution_logs SET disposition = $2::text::execution_disposition
         WHERE execution_id = $1 AND disposition = 'incomplete'",
    )
    .bind(execution_id)
    .bind(disposition)
    .execute(conn)
    .await?;
    Ok(())
}

// ----------------------------------------------------------------------------- the user answers

/// A challenge as an admin sees it. No job, shipment or session identifiers.
#[derive(Debug, sqlx::FromRow)]
pub struct InterventionRow {
    pub id: Uuid,
    pub kind: String,
    pub prompt: String,
    pub status: String,
    pub response_format: Option<Value>,
    pub has_image: bool,
    pub expires_at: DateTime<Utc>,
    pub created_at: DateTime<Utc>,
    pub provider_code: String,
    pub tracking_number: String,
}

macro_rules! intervention_select {
    () => {
        "SELECT i.id, i.kind, i.prompt,
                CASE WHEN i.status = 'pending' AND i.expires_at <= clock_timestamp()
                     THEN 'expired' ELSE i.status END AS status,
                i.response_format, i.image IS NOT NULL AS has_image, i.expires_at, i.created_at,
                p.code AS provider_code, s.tracking_number
         FROM human_interventions i
         JOIN tracking_jobs j ON j.id = i.job_id
         JOIN shipments s ON s.id = j.shipment_id
         JOIN providers p ON p.id = s.provider_id
         WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = $1 AND u.role = 'admin' AND u.status = 'active')"
    };
}

/// Every challenge that still waits for an answer, oldest first. Empty unless the caller is an admin.
pub async fn list_pending(
    pool: &PgPool,
    user_id: Uuid,
) -> Result<Vec<InterventionRow>, sqlx::Error> {
    sqlx::query_as::<_, InterventionRow>(concat!(
        intervention_select!(),
        " AND i.status = 'pending' AND i.expires_at > clock_timestamp() ORDER BY i.created_at, i.id"
    ))
    .bind(user_id)
    .fetch_all(pool)
    .await
}

/// One challenge of the caller, in any state, with its picture if it has one.
pub async fn find(
    pool: &PgPool,
    user_id: Uuid,
    id: Uuid,
) -> Result<Option<(InterventionRow, Option<(String, Vec<u8>)>)>, sqlx::Error> {
    let Some(row) =
        sqlx::query_as::<_, InterventionRow>(concat!(intervention_select!(), " AND i.id = $2"))
            .bind(user_id)
            .bind(id)
            .fetch_optional(pool)
            .await?
    else {
        return Ok(None);
    };
    let image: Option<(Option<String>, Option<Vec<u8>>)> =
        sqlx::query_as("SELECT image_content_type, image FROM human_interventions WHERE id = $1")
            .bind(id)
            .fetch_optional(pool)
            .await?;
    let image = match image {
        Some((Some(content_type), Some(bytes))) => Some((content_type, bytes)),
        _ => None,
    };
    Ok(Some((row, image)))
}

#[derive(Debug, thiserror::Error)]
pub enum SubmitError {
    #[error("intervention not found")]
    NotFound,
    #[error("the challenge has already been answered")]
    NotPending,
    #[error("the challenge is no longer open")]
    Expired,
    #[error("too many invalid answers")]
    TooManyAttempts,
    #[error("{0}")]
    Invalid(&'static str),
    #[error(transparent)]
    Database(#[from] sqlx::Error),
}

/// Checks an answer against what the adapter said a valid one looks like. This only refuses
/// what cannot be right; whether it IS right is for the provider to say.
pub fn validate_response(response: &str, format: Option<&Value>) -> Result<(), &'static str> {
    let length = response.chars().count();
    if length == 0 {
        return Err("the answer is empty");
    }
    if length > 64 || response.chars().any(char::is_control) {
        return Err("the answer is too long or contains control characters");
    }
    let Some(format) = format else {
        return Ok(());
    };
    let bound = |key: &str, default: usize| {
        format
            .get(key)
            .and_then(Value::as_u64)
            .map_or(default, |n| n as usize)
    };
    if length < bound("min_length", 1) || length > bound("max_length", 64) {
        return Err("the answer does not have the expected length");
    }
    let fits = match format.get("charset").and_then(Value::as_str) {
        Some("digits") => response.bytes().all(|c| c.is_ascii_digit()),
        Some("alphanumeric") => response.bytes().all(|c| c.is_ascii_alphanumeric()),
        _ => true,
    };
    if fits {
        Ok(())
    } else {
        Err("the answer contains characters that cannot be part of it")
    }
}

#[derive(sqlx::FromRow)]
struct WaitingJob {
    status: String,
    attempt: i32,
    adapter_id: Uuid,
    correlation_id: Uuid,
    tracking_number: String,
    shipment_id: Uuid,
}

#[derive(sqlx::FromRow)]
struct Challenge {
    attempt: i32,
    status: String,
    overdue: bool,
    response_format: Option<Value>,
    submit_failures: i32,
    session_ref: Uuid,
}

/// Stores the user's answer and sends the lookup on its way. One answer per challenge:
/// a second submission is refused.
pub async fn submit(
    pool: &PgPool,
    master: &MasterKey,
    user_id: Uuid,
    intervention_id: Uuid,
    response: &str,
) -> Result<(), SubmitError> {
    let mut tx = pool.begin().await?;
    // A caller who is not an admin and a challenge that does not exist look the same.
    let job_id: Uuid = sqlx::query_scalar(
        "SELECT job_id FROM human_interventions
         WHERE id = $1
           AND EXISTS (SELECT 1 FROM users u WHERE u.id = $2 AND u.role = 'admin' AND u.status = 'active')",
    )
    .bind(intervention_id)
    .bind(user_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(SubmitError::NotFound)?;

    // Job first, then the challenge: the order the event handlers use.
    let job = sqlx::query_as::<_, WaitingJob>(
        "SELECT j.status::text AS status, j.attempt, j.adapter_id, j.correlation_id, s.tracking_number,
                j.shipment_id
         FROM tracking_jobs j JOIN shipments s ON s.id = j.shipment_id
         WHERE j.id = $1 FOR UPDATE OF j",
    )
    .bind(job_id)
    .fetch_one(&mut *tx)
    .await?;
    let challenge = sqlx::query_as::<_, Challenge>(
        "SELECT attempt, status, expires_at <= clock_timestamp() AS overdue, response_format,
                submit_failures, session_ref
         FROM human_interventions WHERE id = $1 FOR UPDATE",
    )
    .bind(intervention_id)
    .fetch_one(&mut *tx)
    .await?;

    if challenge.status == "answered" || challenge.status == "completed" {
        return Err(SubmitError::NotPending);
    }
    let waiting = matches!(
        job.status.as_str(),
        "WAITING_FOR_OTP" | "WAITING_FOR_CAPTCHA"
    );
    if challenge.status != "pending"
        || challenge.overdue
        || !waiting
        || job.attempt != challenge.attempt
    {
        return Err(SubmitError::Expired);
    }
    if challenge.submit_failures >= MAX_SUBMIT_FAILURES {
        return Err(SubmitError::TooManyAttempts);
    }
    if let Err(problem) = validate_response(response, challenge.response_format.as_ref()) {
        sqlx::query(
            "UPDATE human_interventions SET submit_failures = submit_failures + 1 WHERE id = $1",
        )
        .bind(intervention_id)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        return Err(SubmitError::Invalid(problem));
    }

    let sealed = master.seal(response.as_bytes(), &binding(intervention_id, job_id));
    sqlx::query(
        "UPDATE human_interventions SET
             status = 'answered', answered_at = clock_timestamp(), answered_by = $7,
             wrapped_key = $2, key_nonce = $3, ciphertext = $4, nonce = $5, key_version = $6
         WHERE id = $1",
    )
    .bind(intervention_id)
    .bind(&sealed.wrapped_key)
    .bind(sealed.key_nonce.as_slice())
    .bind(&sealed.ciphertext)
    .bind(sealed.nonce.as_slice())
    .bind(sealed.key_version)
    .bind(user_id)
    .execute(&mut *tx)
    .await?;

    // The lookup continues on the same attempt. It is QUEUED again, so the ordinary
    // queue-wait deadline (and the reaper) covers a resume that no worker picks up.
    let target = db::providers::dispatch_target_by_adapter(&mut tx, job.adapter_id)
        .await?
        .ok_or(sqlx::Error::RowNotFound)?;
    let target = db::providers::scoped(&mut tx, target, job.shipment_id).await?;
    let now: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut *tx)
        .await?;
    let queue_deadline_at = now + Duration::milliseconds(i64::from(target.queue_wait_max_ms));
    sqlx::query(
        "UPDATE tracking_jobs SET status = 'QUEUED', queue_deadline_at = $2,
                intervention_deadline_at = NULL, updated_at = clock_timestamp()
         WHERE id = $1",
    )
    .bind(job_id)
    .bind(queue_deadline_at)
    .execute(&mut *tx)
    .await?;

    let message = Envelope::new(
        JOB_RESUME,
        job.correlation_id,
        now,
        JobTrack {
            job_id,
            attempt: job.attempt,
            adapter_key: target.adapter_key.clone(),
            provider_code: target.provider_code.clone(),
            tracking_number: job.tracking_number,
            params: serde_json::Map::new(),
            timeout_ms: target.timeout_ms,
            expires_at: queue_deadline_at,
            credential_ref: target.credential_id,
            intervention_id: Some(intervention_id),
            session_ref: Some(challenge.session_ref),
        },
    );
    outbox::enqueue(
        &mut tx,
        routing::JOBS_EXCHANGE,
        &routing::job_resume(&target.adapter_key),
        &message,
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

// ----------------------------------------------------------------------------- the worker fetches

pub struct ResponseRequest {
    pub intervention_id: Uuid,
    /// Reference of the session the worker holds for this challenge.
    pub session_ref: Uuid,
    pub job_id: Uuid,
    pub attempt: i32,
}

pub enum ResponseResolved {
    Response(String),
    /// Refused; the reason goes to the audit log, not to the caller.
    Denied(&'static str),
}

#[derive(sqlx::FromRow)]
struct StoredAnswer {
    job_id: Uuid,
    session_ref: Uuid,
    attempt: i32,
    status: String,
    key_version: Option<i32>,
    wrapped_key: Option<Vec<u8>>,
    key_nonce: Option<Vec<u8>>,
    ciphertext: Option<Vec<u8>>,
    nonce: Option<Vec<u8>>,
}

/// Hands a worker the user's answer for the job it is resuming. Allowed only to the worker
/// of the job's adapter, for the job's current attempt while it is in flight, for the exact
/// session the challenge was raised with, and only while the answer has not been used up. Repeatable, so a redelivered `job.resume` still works.
/// Every request is audited (never with the answer).
pub async fn resolve_response(
    pool: &PgPool,
    master: &MasterKey,
    worker: &WorkerIdentity,
    request: ResponseRequest,
) -> Result<ResponseResolved, sqlx::Error> {
    let mut conn = pool.acquire().await?;
    let job: Option<(String, i32, Uuid)> =
        sqlx::query_as("SELECT status::text, attempt, adapter_id FROM tracking_jobs WHERE id = $1")
            .bind(request.job_id)
            .fetch_optional(&mut *conn)
            .await?;
    let stored = sqlx::query_as::<_, StoredAnswer>(
        "SELECT job_id, session_ref, attempt, status, key_version, wrapped_key, key_nonce, ciphertext, nonce
         FROM human_interventions WHERE id = $1",
    )
    .bind(request.intervention_id)
    .fetch_optional(&mut *conn)
    .await?;

    let decision: Result<String, &'static str> = 'decide: {
        let Some((status, attempt, adapter_id)) = job else {
            break 'decide Err("unknown_job");
        };
        if adapter_id != worker.adapter_id {
            break 'decide Err("job_belongs_to_another_adapter");
        }
        if attempt != request.attempt || !matches!(status.as_str(), "QUEUED" | "PROCESSING") {
            break 'decide Err("attempt_not_in_flight");
        }
        let Some(stored) = stored else {
            break 'decide Err("unknown_intervention");
        };
        if stored.job_id != request.job_id || stored.attempt != request.attempt {
            break 'decide Err("intervention_not_for_this_attempt");
        }
        // The answer belongs to one session: the one the challenge was raised with. A worker
        // holding any other session (an older challenge's, another job's) gets nothing.
        if stored.session_ref != request.session_ref {
            break 'decide Err("session_mismatch");
        }
        let (Some(key_version), Some(wrapped_key), Some(key_nonce), Some(ciphertext), Some(nonce)) = (
            stored.key_version,
            stored.wrapped_key,
            stored.key_nonce,
            stored.ciphertext,
            stored.nonce,
        ) else {
            break 'decide Err("no_answer_stored");
        };
        if stored.status != "answered" {
            break 'decide Err("no_answer_stored");
        }
        let (Ok(key_nonce), Ok(nonce)) = (key_nonce.try_into(), nonce.try_into()) else {
            break 'decide Err("decryption_failed");
        };
        let sealed = Sealed {
            key_version,
            wrapped_key,
            key_nonce,
            ciphertext,
            nonce,
        };
        match master.open(&sealed, &binding(request.intervention_id, request.job_id)) {
            Ok(plain) => String::from_utf8(plain).map_err(|_| "decryption_failed"),
            Err(error) => {
                tracing::error!(%error, intervention_id = %request.intervention_id, "stored answer cannot be decrypted");
                Err("decryption_failed")
            }
        }
    };

    audit(
        &mut conn,
        Audit {
            actor_type: "worker",
            actor_id: Some(worker.token_id.to_string()),
            action: "intervention.resolve",
            allowed: decision.is_ok(),
            reason: decision.as_ref().err().copied(),
            job_id: Some(request.job_id),
            attempt: Some(request.attempt),
            details: Some(json!({
                "adapter_key": worker.adapter_key, "intervention_id": request.intervention_id,
            })),
            ..Audit::default()
        },
    )
    .await?;
    Ok(match decision {
        Ok(response) => ResponseResolved::Response(response),
        Err(reason) => ResponseResolved::Denied(reason),
    })
}

// ----------------------------------------------------------------------------- nobody answers

/// Ends up to `limit` jobs whose challenge was not answered in time: the job becomes EXPIRED
/// (terminal), its attempt is closed and the challenge is marked expired. Also tidies up
/// challenges left open by an attempt that ended some other way. Returns how many jobs expired.
pub async fn expire_due(pool: &PgPool, limit: i64) -> Result<usize, sqlx::Error> {
    let mut tx = pool.begin().await?;
    let overdue: Vec<(Uuid, i32)> = sqlx::query_as(
        "SELECT id, attempt FROM tracking_jobs
         WHERE status IN ('WAITING_FOR_OTP', 'WAITING_FOR_CAPTCHA')
           AND intervention_deadline_at < clock_timestamp()
         ORDER BY id
         LIMIT $1
         FOR UPDATE SKIP LOCKED",
    )
    .bind(limit)
    .fetch_all(&mut *tx)
    .await?;
    const MESSAGE: &str = "the challenge was not answered in time";
    for (job_id, attempt) in &overdue {
        sqlx::query(
            "UPDATE tracking_jobs SET
                 status = 'EXPIRED', outcome = 'FAILURE', last_error_class = 'INTERVENTION_EXPIRED',
                 last_error_message = $2, completed_at = clock_timestamp(),
                 intervention_deadline_at = NULL, updated_at = clock_timestamp()
             WHERE id = $1",
        )
        .bind(job_id)
        .bind(MESSAGE)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "UPDATE job_attempts SET
                 closed_at = clock_timestamp(), outcome = 'FAILURE', error_class = 'INTERVENTION_EXPIRED',
                 error_message = $3, closed_by = 'reaper'
             WHERE job_id = $1 AND attempt = $2 AND closed_at IS NULL",
        )
        .bind(job_id)
        .bind(attempt)
        .bind(MESSAGE)
        .execute(&mut *tx)
        .await?;
        close_open(&mut tx, *job_id, "expired").await?;
        tracing::warn!(%job_id, attempt, "challenge expired unanswered; job EXPIRED");
    }
    tx.commit().await?;

    // Challenges whose attempt ended without a result (reaper, dead-letter) or moved on.
    let sql = format!(
        "UPDATE human_interventions i SET status = 'closed', closed_at = clock_timestamp(), {WIPE}
         FROM tracking_jobs j
         WHERE j.id = i.job_id AND i.status IN ('pending', 'answered')
           AND (j.attempt <> i.attempt OR j.status IN ('COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED', 'RETRYING'))"
    );
    sqlx::query(sqlx::AssertSqlSafe(sql)).execute(pool).await?;
    Ok(overdue.len())
}
