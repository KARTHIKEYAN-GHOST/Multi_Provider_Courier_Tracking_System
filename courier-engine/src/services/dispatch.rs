//! Creating tracking requests and jobs.
//!
//! Everything a request creates — shipment, subscription, job, attempt and the outgoing
//! `job.track` message — is written in ONE transaction. Concurrent requests for the same
//! shipment are serialized by a row lock on the shipment, and three partial unique indexes
//! are the final guard against duplicates:
//!   - `ux_shipments_active`          one active shipment per (provider, number, scope)
//!   - `ux_tracking_requests_active`  one active subscription per (user, shipment)
//!   - `ux_tracking_jobs_one_active`  one non-terminal job per shipment

use chrono::{DateTime, Duration, Utc};
use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use crate::db::{
    self,
    providers::DispatchTarget,
    tracking::{self, NewJob},
};
use crate::domain::{JobTrigger, ShipmentStatus, tracking_number};
use crate::messaging::{
    contracts::{Envelope, JOB_TRACK, JobTrack},
    outbox, routing,
};

#[derive(Debug, thiserror::Error)]
pub enum DispatchError {
    #[error("unknown provider")]
    UnknownProvider,
    #[error("invalid tracking number: {0}")]
    InvalidTrackingNumber(&'static str),
    #[error("tracking request not found")]
    RequestNotFound,
    #[error("shipment is archived")]
    ShipmentArchived,
    #[error("provider is not available")]
    ProviderUnavailable,
    #[error(transparent)]
    Database(#[from] sqlx::Error),
}

pub struct NewTrackingRequest<'a> {
    pub user_id: Uuid,
    pub provider_code: &'a str,
    pub tracking_number: &'a str,
    pub reference: Option<&'a str>,
    pub correlation_id: Uuid,
}

#[derive(Debug)]
pub struct Subscribed {
    pub request_id: Uuid,
    /// False when the caller already had an active subscription to this shipment.
    pub created: bool,
    /// Set when this call created a new job.
    pub job_id: Option<Uuid>,
}

/// Subscribes the caller to a shipment, creating the shipment and a lookup job when needed.
pub async fn create_tracking_request(
    pool: &PgPool,
    input: &NewTrackingRequest<'_>,
) -> Result<Subscribed, DispatchError> {
    let number = tracking_number::normalize(input.tracking_number)
        .map_err(DispatchError::InvalidTrackingNumber)?;

    // The shipment row lock makes a unique violation very unlikely, but the indexes are the
    // authority. If one still fires, the conflicting row is committed by now, so a single
    // retry finds it. A violation must never reach the client as a 500.
    match try_create(pool, input, &number).await {
        Err(DispatchError::Database(e)) if db::is_unique_violation(&e) => {
            tracing::warn!(error = %e, "unique violation while creating tracking request; retrying once");
            try_create(pool, input, &number).await
        }
        other => other,
    }
}

async fn try_create(
    pool: &PgPool,
    input: &NewTrackingRequest<'_>,
    number: &str,
) -> Result<Subscribed, DispatchError> {
    let mut tx = pool.begin().await?;

    let target = db::providers::dispatch_target_by_code(&mut tx, input.provider_code)
        .await?
        .ok_or(DispatchError::UnknownProvider)?;
    if let Some(reason) = target.blocked_reason() {
        tracing::warn!(provider = %target.provider_code, reason, "tracking request refused");
        return Err(DispatchError::ProviderUnavailable);
    }

    // 1. Shipment: find or create, and lock the row for the rest of this transaction.
    let shipment = tracking::upsert_active_shipment(&mut tx, target.provider_id, number).await?;

    // 2. Subscription: create, or find the caller's existing one.
    let (request_id, created) =
        match tracking::insert_request(&mut tx, input.user_id, shipment.id, input.reference).await?
        {
            Some(id) => (id, true),
            None => {
                let id = tracking::find_active_request_id(&mut tx, input.user_id, shipment.id)
                    .await?
                    .ok_or(sqlx::Error::RowNotFound)?;
                (id, false)
            }
        };

    // 3. Job: only for a new subscription, only if the shipment can still change,
    //    and only if no job is already active for the shipment.
    let job_id = if created && !shipment.current_status.is_terminal() {
        let job_id = ensure_job(
            &mut tx,
            &target,
            shipment.id,
            number,
            JobTrigger::Initial,
            input.correlation_id,
        )
        .await?;
        // A shipment nobody was subscribed to has stopped being polled; a new subscriber
        // starts it again even when a lookup is already running.
        super::polling::schedule_next_poll_if_unset(&mut tx, shipment.id).await?;
        job_id
    } else {
        None
    };

    tx.commit().await?;
    Ok(Subscribed {
        request_id,
        created,
        job_id,
    })
}

/// What a refresh did. The two cases are reported separately so a client can tell
/// "your refresh started a lookup" from "a lookup was already running".
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RefreshResult {
    JobCreated,
    JobAlreadyActive,
}

#[derive(Debug)]
pub struct Refreshed {
    pub shipment_id: Uuid,
    pub result: RefreshResult,
    /// The shipment's status at the time of the refresh. A refresh never changes it:
    /// the status only moves when a provider result is processed.
    pub shipment_status: ShipmentStatus,
}

/// Starts a fresh lookup at the caller's explicit request.
///
/// - A lookup already running for the shipment is reused; a second job is never created.
/// - A DELIVERED or RETURNED shipment CAN be refreshed this way (a provider can be wrong, and
///   the user asked). Automatic lookups for terminal shipments stay disabled; that rule lives
///   in `create_tracking_request` and, later, the polling scheduler.
/// - An archived shipment is a closed generation of its tracking number and is never refreshed.
pub async fn refresh(
    pool: &PgPool,
    user_id: Uuid,
    request_id: Uuid,
    correlation_id: Uuid,
) -> Result<Refreshed, DispatchError> {
    let mut tx = pool.begin().await?;

    let owned = tracking::lock_shipment_for_request(&mut tx, user_id, request_id)
        .await?
        .ok_or(DispatchError::RequestNotFound)?;
    if owned.archived_at.is_some() {
        return Err(DispatchError::ShipmentArchived);
    }
    let target = db::providers::dispatch_target_by_id(&mut tx, owned.provider_id)
        .await?
        .ok_or(DispatchError::ProviderUnavailable)?;
    if let Some(reason) = target.blocked_reason() {
        tracing::warn!(provider = %target.provider_code, reason, "refresh refused");
        return Err(DispatchError::ProviderUnavailable);
    }

    let result = match ensure_job(
        &mut tx,
        &target,
        owned.shipment_id,
        &owned.tracking_number,
        JobTrigger::Manual,
        correlation_id,
    )
    .await?
    {
        Some(_) => RefreshResult::JobCreated,
        None => RefreshResult::JobAlreadyActive,
    };

    tx.commit().await?;
    Ok(Refreshed {
        shipment_id: owned.shipment_id,
        result,
        shipment_status: owned.current_status,
    })
}

/// Creates a QUEUED job, its first attempt, and the `job.track` outbox message — or nothing
/// at all if the shipment already has a non-terminal job. Returns the new job's ID.
///
/// Used by the first request, a manual refresh and the polling scheduler alike; the caller
/// holds the shipment row lock. Creating a job also schedules the shipment's next automatic
/// lookup, so the polling cadence is counted from the most recent lookup of any kind.
pub(crate) async fn ensure_job(
    conn: &mut PgConnection,
    target: &DispatchTarget,
    shipment_id: Uuid,
    tracking_number: &str,
    trigger: JobTrigger,
    correlation_id: Uuid,
) -> Result<Option<Uuid>, sqlx::Error> {
    // PostgreSQL's clock is the single time source for everything the scheduler, reaper and
    // relay will later compare against now(): the queue-wait deadline, the message's
    // expires_at and the outbox available_at. The Engine's own clock is not used, so clock
    // skew between the Engine host and the database cannot shift a deadline.
    let now: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut *conn)
        .await?;
    // Queue-wait deadline: how long this attempt may wait for a worker to start it.
    // It is unrelated to timeout_ms, the adapter's execution budget.
    let queue_deadline_at = now + Duration::milliseconds(i64::from(target.queue_wait_max_ms));

    let job = NewJob {
        id: Uuid::now_v7(),
        shipment_id,
        adapter_id: target.adapter_id,
        trigger,
        max_attempts: target.max_attempts,
        queue_deadline_at,
        correlation_id,
    };
    if !tracking::insert_job_if_none_active(conn, &job).await? {
        return Ok(None);
    }

    let attempt = 1;
    tracking::insert_attempt(conn, job.id, attempt, now).await?;
    enqueue_job_track(
        conn,
        target,
        job.id,
        attempt,
        tracking_number,
        correlation_id,
        now,
    )
    .await?;
    super::polling::schedule_next_poll(conn, shipment_id).await?;
    Ok(Some(job.id))
}

/// Writes the `job.track` outbox message of one attempt, honouring the provider's rate limit.
///
/// For a rate-limited provider the attempt gets the provider's next free dispatch slot: the
/// message becomes publishable at that time, and the queue-wait deadline (already stored as
/// `now + queue_wait_max_ms` by the caller) is moved to `slot + queue_wait_max_ms`, so waiting
/// for a slot is never mistaken for waiting for a worker. Shared by first dispatch and retries.
pub(crate) async fn enqueue_job_track(
    conn: &mut PgConnection,
    target: &DispatchTarget,
    job_id: Uuid,
    attempt: i32,
    tracking_number: &str,
    correlation_id: Uuid,
    now: DateTime<Utc>,
) -> Result<(), sqlx::Error> {
    let slot = if target.min_dispatch_interval_ms > 0 {
        db::providers::reserve_dispatch_slot(conn, target.provider_id, now).await?
    } else {
        now
    };
    let queue_deadline_at = slot + Duration::milliseconds(i64::from(target.queue_wait_max_ms));
    if slot > now {
        sqlx::query("UPDATE tracking_jobs SET queue_deadline_at = $2 WHERE id = $1")
            .bind(job_id)
            .bind(queue_deadline_at)
            .execute(&mut *conn)
            .await?;
    }
    let message = job_track_message(
        Uuid::now_v7(),
        target,
        job_id,
        attempt,
        tracking_number,
        correlation_id,
        now,
        queue_deadline_at,
    );
    outbox::enqueue_at(
        conn,
        routing::JOBS_EXCHANGE,
        &routing::job_track(&target.adapter_key),
        &message,
        slot,
    )
    .await
}

/// Builds the `job.track` message for one logical attempt.
///
/// This is the only place the Engine constructs that message. It has no hidden inputs (no
/// clock, no randomness), which is what lets `tests/contracts.rs` call it with fixed values
/// and compare the result byte-for-byte with the committed fixture that the Python contract
/// tests validate against the JSON Schema.
#[allow(clippy::too_many_arguments)]
pub fn job_track_message(
    message_id: Uuid,
    target: &DispatchTarget,
    job_id: Uuid,
    attempt: i32,
    tracking_number: &str,
    correlation_id: Uuid,
    dispatched_at: DateTime<Utc>,
    queue_deadline_at: DateTime<Utc>,
) -> Envelope<JobTrack> {
    Envelope::with_id(
        message_id,
        JOB_TRACK,
        correlation_id,
        dispatched_at,
        JobTrack {
            job_id,
            attempt,
            adapter_key: target.adapter_key.clone(),
            provider_code: target.provider_code.clone(),
            tracking_number: tracking_number.to_string(),
            params: serde_json::Map::new(),
            timeout_ms: target.timeout_ms,
            // The queue-wait deadline; unrelated to timeout_ms.
            expires_at: queue_deadline_at,
            credential_ref: target.credential_id,
        },
    )
}
