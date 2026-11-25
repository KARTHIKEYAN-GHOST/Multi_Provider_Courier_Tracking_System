//! Status enums. Shipment status (where the parcel is) and execution status (how a lookup
//! job is going) are separate on purpose. Each enum maps to a PostgreSQL enum type.

use serde::{Deserialize, Serialize};

/// How a tracking job is progressing.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "execution_status", rename_all = "SCREAMING_SNAKE_CASE")]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ExecutionStatus {
    Pending,
    Queued,
    Processing,
    WaitingForOtp,
    WaitingForCaptcha,
    Retrying,
    Completed,
    Failed,
    Cancelled,
    Expired,
}

impl ExecutionStatus {
    /// A terminal job never changes again. Must match the predicate of the
    /// `ux_tracking_jobs_one_active` index (which lists the non-terminal statuses).
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            ExecutionStatus::Completed
                | ExecutionStatus::Failed
                | ExecutionStatus::Cancelled
                | ExecutionStatus::Expired
        )
    }
}

/// Where the shipment is, in provider-independent terms.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "shipment_status", rename_all = "SCREAMING_SNAKE_CASE")]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ShipmentStatus {
    Created,
    PickedUp,
    InTransit,
    OutForDelivery,
    Delivered,
    Returned,
    Exception,
    Unknown,
}

impl ShipmentStatus {
    /// Nothing more will happen to the shipment, so it is not looked up again.
    pub fn is_terminal(self) -> bool {
        matches!(self, ShipmentStatus::Delivered | ShipmentStatus::Returned)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "job_outcome", rename_all = "SCREAMING_SNAKE_CASE")]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum JobOutcome {
    Success,
    NotFound,
    Failure,
}

/// Deterministic failure classification. The last three variants are assigned only by the
/// Engine; workers can never report them (enforced by the message schema).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "error_class", rename_all = "SCREAMING_SNAKE_CASE")]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ErrorClass {
    NetworkTimeout,
    NetworkConnect,
    #[sqlx(rename = "HTTP_5XX")]
    #[serde(rename = "HTTP_5XX")]
    Http5xx,
    RateLimited,
    Blocked,
    AuthInvalid,
    AuthExpired,
    ParseError,
    SchemaDrift,
    AdapterTimeout,
    Internal,
    Unknown,
    /// The session a lookup was waiting with is gone (worker-reported; the lookup restarts).
    SessionLost,
    DeliveryLimitExceeded,
    QueueWaitExceeded,
    WorkerLost,
    /// Nobody answered a one-time-code or CAPTCHA challenge in time.
    InterventionExpired,
    /// The job put more challenges to the user than the provider's limit allows.
    ChallengeLimitExceeded,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "job_trigger", rename_all = "lowercase")]
#[serde(rename_all = "lowercase")]
pub enum JobTrigger {
    Initial,
    Poll,
    Manual,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "tracking_method", rename_all = "lowercase")]
#[serde(rename_all = "lowercase")]
pub enum TrackingMethod {
    Api,
    Scrape,
    Browser,
}

// Complete variant lists, in declaration order (which is also the order of the PostgreSQL enum
// labels). `tests/contracts.rs` compares them with PostgreSQL and the JSON Schemas; the unit
// test below fails to compile if a variant is added without updating a list.
impl ExecutionStatus {
    pub const ALL: [Self; 10] = [
        Self::Pending,
        Self::Queued,
        Self::Processing,
        Self::WaitingForOtp,
        Self::WaitingForCaptcha,
        Self::Retrying,
        Self::Completed,
        Self::Failed,
        Self::Cancelled,
        Self::Expired,
    ];
}

impl ShipmentStatus {
    pub const ALL: [Self; 8] = [
        Self::Created,
        Self::PickedUp,
        Self::InTransit,
        Self::OutForDelivery,
        Self::Delivered,
        Self::Returned,
        Self::Exception,
        Self::Unknown,
    ];
}

impl JobOutcome {
    pub const ALL: [Self; 3] = [Self::Success, Self::NotFound, Self::Failure];
}

impl ErrorClass {
    pub const ALL: [Self; 18] = [
        Self::NetworkTimeout,
        Self::NetworkConnect,
        Self::Http5xx,
        Self::RateLimited,
        Self::Blocked,
        Self::AuthInvalid,
        Self::AuthExpired,
        Self::ParseError,
        Self::SchemaDrift,
        Self::AdapterTimeout,
        Self::Internal,
        Self::Unknown,
        Self::SessionLost,
        Self::DeliveryLimitExceeded,
        Self::QueueWaitExceeded,
        Self::WorkerLost,
        Self::InterventionExpired,
        Self::ChallengeLimitExceeded,
    ];

    /// Classes only the Engine may assign. They are absent from the message schema, so a
    /// worker cannot report them.
    pub fn is_engine_assigned(self) -> bool {
        matches!(
            self,
            Self::DeliveryLimitExceeded
                | Self::QueueWaitExceeded
                | Self::WorkerLost
                | Self::InterventionExpired
                | Self::ChallengeLimitExceeded
        )
    }
}

impl JobTrigger {
    pub const ALL: [Self; 3] = [Self::Initial, Self::Poll, Self::Manual];
}

impl TrackingMethod {
    pub const ALL: [Self; 3] = [Self::Api, Self::Scrape, Self::Browser];
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_four_execution_statuses_are_terminal() {
        use ExecutionStatus::*;
        let all = [
            Pending,
            Queued,
            Processing,
            WaitingForOtp,
            WaitingForCaptcha,
            Retrying,
            Completed,
            Failed,
            Cancelled,
            Expired,
        ];
        let terminal: Vec<_> = all.into_iter().filter(|s| s.is_terminal()).collect();
        assert_eq!(terminal, vec![Completed, Failed, Cancelled, Expired]);
    }

    #[test]
    fn statuses_serialize_exactly_as_the_contract_spells_them() {
        let json = |v: serde_json::Value| v.as_str().unwrap().to_string();
        assert_eq!(
            json(serde_json::to_value(ExecutionStatus::WaitingForOtp).unwrap()),
            "WAITING_FOR_OTP"
        );
        assert_eq!(
            json(serde_json::to_value(ShipmentStatus::OutForDelivery).unwrap()),
            "OUT_FOR_DELIVERY"
        );
        assert_eq!(
            json(serde_json::to_value(ErrorClass::Http5xx).unwrap()),
            "HTTP_5XX"
        );
        assert_eq!(
            json(serde_json::to_value(ErrorClass::DeliveryLimitExceeded).unwrap()),
            "DELIVERY_LIMIT_EXCEEDED"
        );
        assert_eq!(
            json(serde_json::to_value(JobTrigger::Manual).unwrap()),
            "manual"
        );
    }

    #[test]
    fn delivered_and_returned_are_the_terminal_shipment_statuses() {
        assert!(ShipmentStatus::Delivered.is_terminal());
        assert!(ShipmentStatus::Returned.is_terminal());
        assert!(!ShipmentStatus::InTransit.is_terminal());
        assert!(!ShipmentStatus::Unknown.is_terminal());
        assert!(!ShipmentStatus::Exception.is_terminal());
    }

    /// Each `match` below has no wildcard arm, so adding a variant without extending the
    /// corresponding `ALL` list is a compile error here, not a silent gap.
    #[test]
    fn variant_lists_are_complete_and_in_declaration_order() {
        fn execution(s: ExecutionStatus) -> usize {
            use ExecutionStatus::*;
            match s {
                Pending => 0,
                Queued => 1,
                Processing => 2,
                WaitingForOtp => 3,
                WaitingForCaptcha => 4,
                Retrying => 5,
                Completed => 6,
                Failed => 7,
                Cancelled => 8,
                Expired => 9,
            }
        }
        fn shipment(s: ShipmentStatus) -> usize {
            use ShipmentStatus::*;
            match s {
                Created => 0,
                PickedUp => 1,
                InTransit => 2,
                OutForDelivery => 3,
                Delivered => 4,
                Returned => 5,
                Exception => 6,
                Unknown => 7,
            }
        }
        fn outcome(o: JobOutcome) -> usize {
            match o {
                JobOutcome::Success => 0,
                JobOutcome::NotFound => 1,
                JobOutcome::Failure => 2,
            }
        }
        fn error(e: ErrorClass) -> usize {
            use ErrorClass::*;
            match e {
                NetworkTimeout => 0,
                NetworkConnect => 1,
                Http5xx => 2,
                RateLimited => 3,
                Blocked => 4,
                AuthInvalid => 5,
                AuthExpired => 6,
                ParseError => 7,
                SchemaDrift => 8,
                AdapterTimeout => 9,
                Internal => 10,
                Unknown => 11,
                SessionLost => 12,
                DeliveryLimitExceeded => 13,
                QueueWaitExceeded => 14,
                WorkerLost => 15,
                InterventionExpired => 16,
                ChallengeLimitExceeded => 17,
            }
        }
        fn trigger(t: JobTrigger) -> usize {
            match t {
                JobTrigger::Initial => 0,
                JobTrigger::Poll => 1,
                JobTrigger::Manual => 2,
            }
        }
        fn method(m: TrackingMethod) -> usize {
            match m {
                TrackingMethod::Api => 0,
                TrackingMethod::Scrape => 1,
                TrackingMethod::Browser => 2,
            }
        }
        let in_order = |ordinals: Vec<usize>| ordinals.iter().copied().eq(0..ordinals.len());
        assert!(in_order(ExecutionStatus::ALL.map(execution).to_vec()));
        assert!(in_order(ShipmentStatus::ALL.map(shipment).to_vec()));
        assert!(in_order(JobOutcome::ALL.map(outcome).to_vec()));
        assert!(in_order(ErrorClass::ALL.map(error).to_vec()));
        assert!(in_order(JobTrigger::ALL.map(trigger).to_vec()));
        assert!(in_order(TrackingMethod::ALL.map(method).to_vec()));
    }

    #[test]
    fn exactly_five_error_classes_are_engine_assigned() {
        let engine_only: Vec<_> = ErrorClass::ALL
            .into_iter()
            .filter(|c| c.is_engine_assigned())
            .collect();
        assert_eq!(
            engine_only,
            vec![
                ErrorClass::DeliveryLimitExceeded,
                ErrorClass::QueueWaitExceeded,
                ErrorClass::WorkerLost,
                ErrorClass::InterventionExpired,
                ErrorClass::ChallengeLimitExceeded
            ]
        );
    }
}
