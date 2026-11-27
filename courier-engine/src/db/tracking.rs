//! Shipments, subscriptions (tracking requests), jobs and events.
//!
//! Every user-facing read goes through `tracking_requests` filtered by the caller's user ID
//! and `status = 'active'`. That filter is the access-control rule: a user can only reach a
//! shipment through their own active subscription.

use chrono::{DateTime, Utc};
use serde_json::Value;
use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use crate::domain::{ErrorClass, ExecutionStatus, JobOutcome, JobTrigger, ShipmentStatus};

// ----------------------------------------------------------------------------- writes

#[derive(Debug, sqlx::FromRow)]
pub struct ShipmentRef {
    pub id: Uuid,
    pub current_status: ShipmentStatus,
}

/// Finds the active shipment for (provider, tracking number), or creates it.
///
/// `DO UPDATE` (instead of `DO NOTHING`) is deliberate: it always returns the row AND takes a
/// row lock on it. Concurrent requests for the same shipment therefore run one after another
/// for the rest of their transactions, which is what makes "one job per shipment" hold.
pub async fn upsert_active_shipment(
    conn: &mut PgConnection,
    provider_id: Uuid,
    tracking_number: &str,
    access_scope: &str,
) -> Result<ShipmentRef, sqlx::Error> {
    sqlx::query_as::<_, ShipmentRef>(
        "INSERT INTO shipments (id, provider_id, tracking_number, access_scope)
         VALUES ($1, $2, $3, $4)
         ON CONFLICT (provider_id, tracking_number, access_scope) WHERE archived_at IS NULL
         DO UPDATE SET updated_at = now()
         RETURNING id, current_status",
    )
    .bind(Uuid::now_v7())
    .bind(provider_id)
    .bind(tracking_number)
    .bind(access_scope)
    .fetch_one(conn)
    .await
}

/// Creates the caller's subscription. Returns `None` if they already have an active one.
pub async fn insert_request(
    conn: &mut PgConnection,
    user_id: Uuid,
    shipment_id: Uuid,
    reference: Option<&str>,
) -> Result<Option<Uuid>, sqlx::Error> {
    sqlx::query_scalar::<_, Uuid>(
        "INSERT INTO tracking_requests (id, user_id, shipment_id, reference)
         VALUES ($1, $2, $3, $4)
         ON CONFLICT (user_id, shipment_id) WHERE status = 'active' DO NOTHING
         RETURNING id",
    )
    .bind(Uuid::now_v7())
    .bind(user_id)
    .bind(shipment_id)
    .bind(reference)
    .fetch_optional(conn)
    .await
}

pub async fn find_active_request_id(
    conn: &mut PgConnection,
    user_id: Uuid,
    shipment_id: Uuid,
) -> Result<Option<Uuid>, sqlx::Error> {
    sqlx::query_scalar::<_, Uuid>(
        "SELECT id FROM tracking_requests WHERE user_id = $1 AND shipment_id = $2 AND status = 'active'",
    )
    .bind(user_id)
    .bind(shipment_id)
    .fetch_optional(conn)
    .await
}

pub struct NewJob {
    pub id: Uuid,
    pub shipment_id: Uuid,
    pub adapter_id: Uuid,
    pub trigger: JobTrigger,
    pub max_attempts: i32,
    pub queue_deadline_at: DateTime<Utc>,
    pub correlation_id: Uuid,
    /// The user whose request created the job; `None` for an automatic poll.
    pub initiated_by: Option<Uuid>,
}

/// Inserts a QUEUED job unless the shipment already has a non-terminal job.
/// Returns whether a row was inserted. The partial unique index is the final authority.
pub async fn insert_job_if_none_active(
    conn: &mut PgConnection,
    job: &NewJob,
) -> Result<bool, sqlx::Error> {
    let inserted = sqlx::query_scalar::<_, Uuid>(
        concat!(
            "INSERT INTO tracking_jobs
                 (id, shipment_id, adapter_id, trigger, status, attempt, max_attempts, queue_deadline_at,
                  correlation_id, initiated_by)
             VALUES ($1, $2, $3, $4, 'QUEUED', 1, $5, $6, $7, $8)
             ON CONFLICT (shipment_id) WHERE status IN ",
            active_job_statuses!(),
            " DO NOTHING
             RETURNING id"
        ),
    )
    .bind(job.id)
    .bind(job.shipment_id)
    .bind(job.adapter_id)
    .bind(job.trigger)
    .bind(job.max_attempts)
    .bind(job.queue_deadline_at)
    .bind(job.correlation_id)
    .bind(job.initiated_by)
    .fetch_optional(conn)
    .await?;
    Ok(inserted.is_some())
}

/// Opens a logical attempt. One row per (job, attempt).
pub async fn insert_attempt(
    conn: &mut PgConnection,
    job_id: Uuid,
    attempt: i32,
    dispatched_at: DateTime<Utc>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO job_attempts (id, job_id, attempt, dispatched_at) VALUES ($1, $2, $3, $4)",
    )
    .bind(Uuid::now_v7())
    .bind(job_id)
    .bind(attempt)
    .bind(dispatched_at)
    .execute(conn)
    .await?;
    Ok(())
}

pub async fn find_active_job_id(
    conn: &mut PgConnection,
    shipment_id: Uuid,
) -> Result<Option<Uuid>, sqlx::Error> {
    sqlx::query_scalar::<_, Uuid>(concat!(
        "SELECT id FROM tracking_jobs WHERE shipment_id = $1 AND status IN ",
        active_job_statuses!()
    ))
    .bind(shipment_id)
    .fetch_optional(conn)
    .await
}

#[derive(Debug, sqlx::FromRow)]
pub struct OwnedShipment {
    pub shipment_id: Uuid,
    pub provider_id: Uuid,
    pub tracking_number: String,
    pub current_status: ShipmentStatus,
    /// Set when this generation of the tracking number is closed.
    pub archived_at: Option<DateTime<Utc>>,
}

/// Resolves the caller's request to its shipment and locks the shipment row, so a refresh
/// is serialized with any other transaction creating a job for the same shipment.
pub async fn lock_shipment_for_request(
    conn: &mut PgConnection,
    user_id: Uuid,
    request_id: Uuid,
) -> Result<Option<OwnedShipment>, sqlx::Error> {
    sqlx::query_as::<_, OwnedShipment>(
        "SELECT s.id AS shipment_id, s.provider_id, s.tracking_number, s.current_status, s.archived_at
         FROM tracking_requests r
         JOIN shipments s ON s.id = r.shipment_id
         WHERE r.id = $1 AND r.user_id = $2 AND r.status = 'active'
         FOR UPDATE OF s",
    )
    .bind(request_id)
    .bind(user_id)
    .fetch_optional(conn)
    .await
}

/// Cancels the caller's own subscription. Other subscribers of the shipment are unaffected.
pub async fn cancel_request(
    pool: &PgPool,
    user_id: Uuid,
    request_id: Uuid,
) -> Result<bool, sqlx::Error> {
    let result = sqlx::query(
        "UPDATE tracking_requests
         SET status = 'cancelled', cancelled_at = now(), updated_at = now()
         WHERE id = $1 AND user_id = $2 AND status = 'active'",
    )
    .bind(request_id)
    .bind(user_id)
    .execute(pool)
    .await?;
    Ok(result.rows_affected() > 0)
}

// ----------------------------------------------------------------------------- reads

/// A request with its shipment status and its shipment's most recent job (if any).
#[derive(Debug, sqlx::FromRow)]
pub struct RequestRow {
    pub id: Uuid,
    pub provider_code: String,
    pub provider_status: String,
    pub tracking_number: String,
    pub reference: Option<String>,
    pub shipment_status: ShipmentStatus,
    pub last_event_at: Option<DateTime<Utc>>,
    pub next_poll_at: Option<DateTime<Utc>>,
    pub archived_at: Option<DateTime<Utc>>,
    pub created_at: DateTime<Utc>,
    pub job_id: Option<Uuid>,
    pub job_status: Option<ExecutionStatus>,
    pub job_attempt: Option<i32>,
    pub job_max_attempts: Option<i32>,
    pub job_trigger: Option<JobTrigger>,
    pub job_outcome: Option<JobOutcome>,
    pub job_last_error_class: Option<ErrorClass>,
    pub job_execution_count: Option<i32>,
    pub job_created_at: Option<DateTime<Utc>>,
    pub job_completed_at: Option<DateTime<Utc>>,
}

macro_rules! request_select {
    () => {
        "SELECT r.id, p.code AS provider_code, p.status AS provider_status, s.tracking_number, r.reference,
                s.current_status AS shipment_status, s.last_event_at, s.next_poll_at, s.archived_at,
                r.created_at,
                j.id AS job_id, j.status AS job_status, j.attempt AS job_attempt,
                j.max_attempts AS job_max_attempts, j.trigger AS job_trigger, j.outcome AS job_outcome,
                j.last_error_class AS job_last_error_class, j.execution_count AS job_execution_count,
                j.created_at AS job_created_at, j.completed_at AS job_completed_at
         FROM tracking_requests r
         JOIN shipments s ON s.id = r.shipment_id
         JOIN providers p ON p.id = s.provider_id
         LEFT JOIN LATERAL (
             SELECT j.*,
                    (SELECT COALESCE(SUM(a.execution_count), 0)::int
                     FROM job_attempts a WHERE a.job_id = j.id) AS execution_count
             FROM tracking_jobs j
             WHERE j.shipment_id = s.id
             ORDER BY j.created_at DESC, j.id DESC
             LIMIT 1
         ) j ON true
         WHERE r.user_id = $1 AND r.status = 'active'"
    };
}

pub async fn find_request(
    pool: &PgPool,
    user_id: Uuid,
    request_id: Uuid,
) -> Result<Option<RequestRow>, sqlx::Error> {
    sqlx::query_as::<_, RequestRow>(concat!(request_select!(), " AND r.id = $2"))
        .bind(user_id)
        .bind(request_id)
        .fetch_optional(pool)
        .await
}

/// Newest first. `before` is the ID of the last item of the previous page.
pub async fn list_requests(
    pool: &PgPool,
    user_id: Uuid,
    before: Option<Uuid>,
    limit: i64,
) -> Result<Vec<RequestRow>, sqlx::Error> {
    sqlx::query_as::<_, RequestRow>(concat!(
        request_select!(),
        " AND ($2::uuid IS NULL OR r.id < $2) ORDER BY r.id DESC LIMIT $3"
    ))
    .bind(user_id)
    .bind(before)
    .bind(limit)
    .fetch_all(pool)
    .await
}

/// The access check used by the events and jobs endpoints.
pub async fn shipment_id_for_request(
    pool: &PgPool,
    user_id: Uuid,
    request_id: Uuid,
) -> Result<Option<Uuid>, sqlx::Error> {
    sqlx::query_scalar::<_, Uuid>(
        "SELECT shipment_id FROM tracking_requests WHERE id = $1 AND user_id = $2 AND status = 'active'",
    )
    .bind(request_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
}

#[derive(Debug, sqlx::FromRow)]
pub struct EventRow {
    pub id: Uuid,
    pub raw_status: String,
    pub raw_description: Option<String>,
    pub normalized_status: ShipmentStatus,
    pub occurred_at: DateTime<Utc>,
    pub location: Option<Value>,
}

pub async fn list_events(pool: &PgPool, shipment_id: Uuid) -> Result<Vec<EventRow>, sqlx::Error> {
    sqlx::query_as::<_, EventRow>(
        "SELECT id, raw_status, raw_description, normalized_status, occurred_at, location
         FROM tracking_events
         WHERE shipment_id = $1
         ORDER BY occurred_at, id",
    )
    .bind(shipment_id)
    .fetch_all(pool)
    .await
}

#[derive(Debug, sqlx::FromRow)]
pub struct JobRow {
    pub id: Uuid,
    pub status: ExecutionStatus,
    pub attempt: i32,
    pub max_attempts: i32,
    pub trigger: JobTrigger,
    pub outcome: Option<JobOutcome>,
    pub last_error_class: Option<ErrorClass>,
    pub execution_count: i32,
    pub created_at: DateTime<Utc>,
    pub completed_at: Option<DateTime<Utc>>,
}

macro_rules! job_select {
    () => {
        "SELECT j.id, j.status, j.attempt, j.max_attempts, j.trigger, j.outcome, j.last_error_class,
                (SELECT COALESCE(SUM(a.execution_count), 0)::int
                 FROM job_attempts a WHERE a.job_id = j.id) AS execution_count,
                j.created_at, j.completed_at
         FROM tracking_jobs j"
    };
}

pub async fn list_jobs(pool: &PgPool, shipment_id: Uuid) -> Result<Vec<JobRow>, sqlx::Error> {
    sqlx::query_as::<_, JobRow>(concat!(
        job_select!(),
        " WHERE j.shipment_id = $1 ORDER BY j.created_at DESC, j.id DESC"
    ))
    .bind(shipment_id)
    .fetch_all(pool)
    .await
}

pub async fn latest_job(pool: &PgPool, shipment_id: Uuid) -> Result<Option<JobRow>, sqlx::Error> {
    sqlx::query_as::<_, JobRow>(concat!(
        job_select!(),
        " WHERE j.shipment_id = $1 ORDER BY j.created_at DESC, j.id DESC LIMIT 1"
    ))
    .bind(shipment_id)
    .fetch_optional(pool)
    .await
}
