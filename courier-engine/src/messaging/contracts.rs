//! Rust mirror of the JSON Schemas in `courier-contracts/schemas/v1`.
//! The schemas are the source of truth; tests validate what this module produces against them.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

pub const SCHEMA_VERSION: &str = "1.0";
pub const JOB_TRACK: &str = "job.track";

/// Wrapper around every message.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Envelope<P> {
    /// Unique per message; consumers deduplicate on it.
    pub message_id: Uuid,
    #[serde(rename = "type")]
    pub message_type: String,
    pub schema_version: String,
    #[serde(with = "utc_timestamp")]
    pub occurred_at: DateTime<Utc>,
    /// Constant across everything caused by one API request.
    pub correlation_id: Uuid,
    pub causation_id: Option<Uuid>,
    pub producer: String,
    pub payload: P,
}

impl<P> Envelope<P> {
    pub fn new(
        message_type: &str,
        correlation_id: Uuid,
        occurred_at: DateTime<Utc>,
        payload: P,
    ) -> Self {
        Self::with_id(
            Uuid::now_v7(),
            message_type,
            correlation_id,
            occurred_at,
            payload,
        )
    }

    /// As [`Envelope::new`] with the message ID supplied by the caller. Production code uses
    /// `new`; the contract test uses this to produce a byte-for-byte reproducible message.
    pub fn with_id(
        message_id: Uuid,
        message_type: &str,
        correlation_id: Uuid,
        occurred_at: DateTime<Utc>,
        payload: P,
    ) -> Self {
        Envelope {
            message_id,
            message_type: message_type.to_string(),
            schema_version: SCHEMA_VERSION.to_string(),
            occurred_at,
            correlation_id,
            causation_id: None,
            producer: format!("engine@{}", env!("CARGO_PKG_VERSION")),
            payload,
        }
    }
}

/// `job.track`: asks a worker to look up one shipment. One message per logical attempt.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobTrack {
    pub job_id: Uuid,
    pub attempt: i32,
    pub adapter_key: String,
    pub provider_code: String,
    pub tracking_number: String,
    pub params: serde_json::Map<String, serde_json::Value>,
    /// Adapter execution budget, measured by the worker from the start of execution.
    pub timeout_ms: i32,
    /// Queue-wait deadline; independent of `timeout_ms`.
    #[serde(with = "utc_timestamp")]
    pub expires_at: DateTime<Utc>,
}

/// The contract requires UTC timestamps with a `Z` suffix (never `+00:00`).
pub mod utc_timestamp {
    use chrono::{DateTime, SecondsFormat, Utc};
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(
        value: &DateTime<Utc>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.to_rfc3339_opts(SecondsFormat::Micros, true))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<DateTime<Utc>, D::Error> {
        let text = String::deserialize(deserializer)?;
        if !text.ends_with('Z') {
            return Err(serde::de::Error::custom(
                "timestamp must be UTC with a Z suffix",
            ));
        }
        DateTime::parse_from_rfc3339(&text)
            .map(|t| t.with_timezone(&Utc))
            .map_err(serde::de::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn timestamps_serialize_with_a_z_suffix() {
        let at = Utc.with_ymd_and_hms(2026, 11, 24, 10, 0, 0).unwrap();
        let envelope = Envelope::new(JOB_TRACK, Uuid::now_v7(), at, serde_json::json!({}));
        let json = serde_json::to_value(&envelope).unwrap();
        assert_eq!(json["occurred_at"], "2026-11-24T10:00:00.000000Z");
        assert_eq!(json["type"], "job.track");
        assert_eq!(json["schema_version"], "1.0");
        assert!(json["causation_id"].is_null());
    }

    #[test]
    fn non_utc_timestamps_are_rejected() {
        let text = r#"{"message_id":"019355a0-aaaa-7000-8000-000000000001","type":"job.track",
            "schema_version":"1.0","occurred_at":"2026-11-24T15:30:00+05:30",
            "correlation_id":"019355a0-0000-7000-8000-00000000c0de","causation_id":null,
            "producer":"engine@0.1.0","payload":{}}"#;
        assert!(serde_json::from_str::<Envelope<serde_json::Value>>(text).is_err());
    }
}
