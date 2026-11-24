//! Messaging: message contracts, the transactional outbox and its relay, and the RabbitMQ
//! connection (topology, publishing, consuming).

pub mod broker;
pub mod contracts;
pub mod outbox;
pub mod relay;
pub mod topology;

/// Routing, as defined in `courier-contracts/topology.yaml`.
pub mod routing {
    pub const JOBS_EXCHANGE: &str = "courier.jobs";

    /// The major schema version is part of the routing key.
    pub fn job_track(adapter_key: &str) -> String {
        format!("job.track.v1.{adapter_key}")
    }
}
