use std::sync::Arc;

use sqlx::PgPool;

use crate::{config::Config, messaging::broker::BrokerHealth};

/// Shared by every request handler.
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub config: Arc<Config>,
    /// Present when the Engine runs its broker session (`serve`); absent in API-only tests.
    pub broker: Option<BrokerHealth>,
}

impl AppState {
    pub fn new(pool: PgPool, config: Config) -> Self {
        AppState {
            pool,
            config: Arc::new(config),
            broker: None,
        }
    }

    pub fn with_broker(mut self, health: BrokerHealth) -> Self {
        self.broker = Some(health);
        self
    }
}
