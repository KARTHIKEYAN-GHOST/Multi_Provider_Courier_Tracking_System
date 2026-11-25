//! Configuration, read from environment variables (a `.env` file is loaded first if present).

use serde::Deserialize;

#[derive(Clone, Deserialize)]
pub struct Config {
    /// PostgreSQL connection string.
    pub database_url: String,
    /// Address the public HTTP API listens on.
    #[serde(default = "default_http_addr")]
    pub engine_http_addr: String,
    #[serde(default = "default_db_max_connections")]
    pub db_max_connections: u32,
    /// A request that takes longer than this is answered with 504.
    #[serde(default = "default_request_timeout_ms")]
    pub request_timeout_ms: u64,
    /// `json` (default) or `pretty`.
    #[serde(default = "default_log_format")]
    pub log_format: String,
    /// RabbitMQ connection string for the Engine account. Required by `serve`.
    #[serde(default)]
    pub engine_amqp_url: Option<String>,
    /// How often the outbox relay looks for pending messages when it is idle.
    #[serde(default = "default_outbox_poll_ms")]
    pub outbox_poll_ms: u64,
    #[serde(default = "default_outbox_batch")]
    pub outbox_batch: i64,
    /// How many unacknowledged events RabbitMQ may hand the Engine at once.
    #[serde(default = "default_event_prefetch")]
    pub event_prefetch: u16,
    #[serde(default = "default_scheduler_interval_ms")]
    pub retry_scheduler_interval_ms: u64,
    #[serde(default = "default_reaper_interval_ms")]
    pub reaper_interval_ms: u64,
    /// How often the polling scheduler looks for shipments whose next lookup is due.
    #[serde(default = "default_poll_scheduler_interval_ms")]
    pub poll_scheduler_interval_ms: u64,
    /// How often archiving and retention run.
    #[serde(default = "default_maintenance_interval_ms")]
    pub maintenance_interval_ms: u64,
    /// Days between archiving a shipment and deleting its events and raw responses. 0 = never.
    #[serde(default = "default_retention_days")]
    pub retention_days: i32,
    /// Days a sent outbox message is kept. 0 = forever.
    #[serde(default = "default_outbox_retention_days")]
    pub outbox_retention_days: i32,
    /// Days an inbox (dedupe) row is kept. 0 = forever.
    #[serde(default = "default_inbox_retention_days")]
    pub inbox_retention_days: i32,
    /// How many times a job is retried after its message was dead-lettered.
    #[serde(default = "default_max_dead_letter_retries")]
    pub max_dead_letter_retries: i32,
    /// Prefix of every exchange and queue name. Empty in normal operation; the end-to-end
    /// tests set it so that they never touch the real queues.
    #[serde(default)]
    pub engine_topology_prefix: String,
    /// Address of the internal API that workers use to resolve credentials. It must not be
    /// reachable from outside the deployment. Not started when unset.
    #[serde(default)]
    pub engine_internal_addr: Option<String>,
    /// Token of the AI repair service for its two internal endpoints (at least 32
    /// characters). A secret. Unset: those endpoints refuse everybody.
    #[serde(default)]
    pub engine_repair_token: Option<String>,
    /// Master key for stored credentials: 64 hexadecimal characters. A secret.
    #[serde(default)]
    pub credential_master_key: Option<String>,
    /// Version number of the master key above; stored with every credential it encrypts.
    #[serde(default = "default_credential_key_version")]
    pub credential_key_version: i32,
    /// Whether the test providers (dummy, demosite) are offered. Set to false in production.
    #[serde(default = "default_enable_dummy_provider")]
    pub enable_dummy_provider: bool,
}

fn default_credential_key_version() -> i32 {
    1
}
fn default_enable_dummy_provider() -> bool {
    true
}

fn default_outbox_poll_ms() -> u64 {
    250
}
fn default_outbox_batch() -> i64 {
    50
}
fn default_event_prefetch() -> u16 {
    20
}
fn default_scheduler_interval_ms() -> u64 {
    500
}
fn default_reaper_interval_ms() -> u64 {
    1000
}
fn default_poll_scheduler_interval_ms() -> u64 {
    1000
}
fn default_maintenance_interval_ms() -> u64 {
    300_000
}
fn default_retention_days() -> i32 {
    180
}
fn default_outbox_retention_days() -> i32 {
    7
}
fn default_inbox_retention_days() -> i32 {
    30
}
fn default_max_dead_letter_retries() -> i32 {
    1
}

fn default_http_addr() -> String {
    "127.0.0.1:8080".to_string()
}
fn default_db_max_connections() -> u32 {
    10
}
fn default_request_timeout_ms() -> u64 {
    10_000
}
fn default_log_format() -> String {
    "json".to_string()
}

/// `Debug` is written by hand so that printing the configuration (in a log line, a panic
/// message or an error) can never reveal the database password.
impl std::fmt::Debug for Config {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Config")
            .field("database_url", &"[redacted]")
            .field("engine_http_addr", &self.engine_http_addr)
            .field("db_max_connections", &self.db_max_connections)
            .field("request_timeout_ms", &self.request_timeout_ms)
            .field("log_format", &self.log_format)
            .field(
                "engine_amqp_url",
                &self.engine_amqp_url.as_ref().map(|_| "[redacted]"),
            )
            .field("outbox_poll_ms", &self.outbox_poll_ms)
            .field("outbox_batch", &self.outbox_batch)
            .field("event_prefetch", &self.event_prefetch)
            .field(
                "retry_scheduler_interval_ms",
                &self.retry_scheduler_interval_ms,
            )
            .field("reaper_interval_ms", &self.reaper_interval_ms)
            .field(
                "poll_scheduler_interval_ms",
                &self.poll_scheduler_interval_ms,
            )
            .field("maintenance_interval_ms", &self.maintenance_interval_ms)
            .field("retention_days", &self.retention_days)
            .field("outbox_retention_days", &self.outbox_retention_days)
            .field("inbox_retention_days", &self.inbox_retention_days)
            .field("max_dead_letter_retries", &self.max_dead_letter_retries)
            .field("engine_topology_prefix", &self.engine_topology_prefix)
            .field("engine_internal_addr", &self.engine_internal_addr)
            .field(
                "engine_repair_token",
                &self.engine_repair_token.as_ref().map(|_| "[redacted]"),
            )
            .field(
                "credential_master_key",
                &self.credential_master_key.as_ref().map(|_| "[redacted]"),
            )
            .field("credential_key_version", &self.credential_key_version)
            .field("enable_dummy_provider", &self.enable_dummy_provider)
            .finish()
    }
}

impl Config {
    pub fn retention(&self) -> crate::services::retention::RetentionSettings {
        crate::services::retention::RetentionSettings {
            retention_days: self.retention_days,
            outbox_retention_days: self.outbox_retention_days,
            inbox_retention_days: self.inbox_retention_days,
            batch: 500,
        }
    }

    pub fn from_env() -> anyhow::Result<Self> {
        // Looks in the current directory and its parents; a missing file is not an error.
        let _ = dotenvy::dotenv();
        envy::from_env::<Config>().map_err(|e| anyhow::anyhow!("invalid configuration: {e}"))
    }

    /// Defaults suitable for tests; only the database comes from the caller.
    pub fn for_tests() -> Self {
        Config {
            database_url: String::new(),
            engine_http_addr: default_http_addr(),
            db_max_connections: default_db_max_connections(),
            request_timeout_ms: default_request_timeout_ms(),
            log_format: default_log_format(),
            engine_amqp_url: None,
            outbox_poll_ms: default_outbox_poll_ms(),
            outbox_batch: default_outbox_batch(),
            event_prefetch: default_event_prefetch(),
            retry_scheduler_interval_ms: default_scheduler_interval_ms(),
            reaper_interval_ms: default_reaper_interval_ms(),
            poll_scheduler_interval_ms: default_poll_scheduler_interval_ms(),
            maintenance_interval_ms: default_maintenance_interval_ms(),
            retention_days: default_retention_days(),
            outbox_retention_days: default_outbox_retention_days(),
            inbox_retention_days: default_inbox_retention_days(),
            max_dead_letter_retries: default_max_dead_letter_retries(),
            engine_topology_prefix: String::new(),
            engine_internal_addr: None,
            engine_repair_token: None,
            credential_master_key: None,
            credential_key_version: default_credential_key_version(),
            enable_dummy_provider: default_enable_dummy_provider(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_output_never_contains_database_credentials() {
        let mut config = Config::for_tests();
        config.database_url = "postgres://courier:s3cret-pass@db.internal:5432/courier".to_string();
        config.engine_amqp_url =
            Some("amqp://engine:s3cret-pass@mq.internal:5672/courier".to_string());
        config.credential_master_key = Some("ab".repeat(32));
        let printed = format!("{config:?} {config:#?}");
        assert!(!printed.contains("s3cret-pass"));
        assert!(!printed.contains("postgres://"));
        assert!(!printed.contains("amqp://"));
        assert!(!printed.contains("abab"));
        assert!(printed.contains("[redacted]"));
        assert!(printed.contains("engine_http_addr"));
    }
}
