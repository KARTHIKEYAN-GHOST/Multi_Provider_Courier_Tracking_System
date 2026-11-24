//! Messaging. Phase 2 only writes messages to the outbox; publishing and consuming
//! through RabbitMQ are added in Phase 3.

pub mod contracts;
pub mod outbox;

/// Routing, as defined in `courier-contracts/topology.yaml`.
pub mod routing {
    pub const JOBS_EXCHANGE: &str = "courier.jobs";

    /// The major schema version is part of the routing key.
    pub fn job_track(adapter_key: &str) -> String {
        format!("job.track.v1.{adapter_key}")
    }
}
