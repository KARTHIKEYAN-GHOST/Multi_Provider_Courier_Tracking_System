//! Request and response bodies of the public API.
//!
//! Responses expose a shipment only through the caller's own request. They never contain
//! other subscribers, raw provider responses or internal shipment IDs.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::db::tracking::{EventRow, JobRow, RequestRow};
use crate::domain::{ErrorClass, ExecutionStatus, JobOutcome, JobTrigger, ShipmentStatus};

#[derive(Debug, Deserialize)]
pub struct CreateTrackingRequest {
    pub provider_code: String,
    pub tracking_number: String,
    pub reference: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ListParams {
    pub limit: Option<String>,
    pub cursor: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct JobView {
    pub id: Uuid,
    pub status: ExecutionStatus,
    pub attempt: i32,
    pub max_attempts: i32,
    pub trigger: JobTrigger,
    pub outcome: Option<JobOutcome>,
    pub last_error_class: Option<ErrorClass>,
    /// Physical executions across all attempts (redeliveries included).
    pub execution_count: i32,
    pub created_at: DateTime<Utc>,
    pub completed_at: Option<DateTime<Utc>>,
}

impl From<JobRow> for JobView {
    fn from(j: JobRow) -> Self {
        JobView {
            id: j.id,
            status: j.status,
            attempt: j.attempt,
            max_attempts: j.max_attempts,
            trigger: j.trigger,
            outcome: j.outcome,
            last_error_class: j.last_error_class,
            execution_count: j.execution_count,
            created_at: j.created_at,
            completed_at: j.completed_at,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct TrackingRequestView {
    pub id: Uuid,
    pub provider_code: String,
    /// `live`; `building` while the courier's adapter is being written (the lookup starts by
    /// itself once it is ready); `failed` when no adapter could be built.
    pub provider_status: String,
    pub tracking_number: String,
    pub reference: Option<String>,
    /// Normalized shipment status — where the parcel is.
    pub shipment_status: ShipmentStatus,
    pub last_event_at: Option<DateTime<Utc>>,
    /// When the Engine will look the shipment up again by itself; null when it will not
    /// (finished, archived, or the provider is not polled).
    pub next_check_at: Option<DateTime<Utc>>,
    /// Set when this shipment is a closed generation of its tracking number: it no longer
    /// changes, and a new request for the same number starts a new shipment.
    pub archived_at: Option<DateTime<Utc>>,
    pub created_at: DateTime<Utc>,
    /// The shipment's most recent lookup job — how the lookup is going.
    pub latest_job: Option<JobView>,
}

impl From<RequestRow> for TrackingRequestView {
    fn from(r: RequestRow) -> Self {
        // The job columns come from a LEFT JOIN: either all present or all absent.
        let latest_job = match (
            r.job_id,
            r.job_status,
            r.job_attempt,
            r.job_max_attempts,
            r.job_trigger,
            r.job_created_at,
        ) {
            (
                Some(id),
                Some(status),
                Some(attempt),
                Some(max_attempts),
                Some(trigger),
                Some(created_at),
            ) => Some(JobView {
                id,
                status,
                attempt,
                max_attempts,
                trigger,
                outcome: r.job_outcome,
                last_error_class: r.job_last_error_class,
                execution_count: r.job_execution_count.unwrap_or(0),
                created_at,
                completed_at: r.job_completed_at,
            }),
            _ => None,
        };
        TrackingRequestView {
            id: r.id,
            provider_code: r.provider_code,
            provider_status: r.provider_status,
            tracking_number: r.tracking_number,
            reference: r.reference,
            shipment_status: r.shipment_status,
            last_event_at: r.last_event_at,
            next_check_at: r.next_poll_at,
            archived_at: r.archived_at,
            created_at: r.created_at,
            latest_job,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct EventView {
    pub id: Uuid,
    /// The provider's own wording, always preserved.
    pub raw_status: String,
    pub raw_description: Option<String>,
    pub normalized_status: ShipmentStatus,
    pub occurred_at: DateTime<Utc>,
    pub location: Option<Value>,
}

impl From<EventRow> for EventView {
    fn from(e: EventRow) -> Self {
        EventView {
            id: e.id,
            raw_status: e.raw_status,
            raw_description: e.raw_description,
            normalized_status: e.normalized_status,
            occurred_at: e.occurred_at,
            location: e.location,
        }
    }
}
