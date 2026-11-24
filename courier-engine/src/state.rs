use std::sync::Arc;

use sqlx::PgPool;

use crate::{
    config::Config, messaging::broker::BrokerHealth, services::credentials::PgSecretStore,
};

/// Shared by every request handler.
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub config: Arc<Config>,
    /// Present when the Engine runs its broker session (`serve`); absent in API-only tests.
    pub broker: Option<BrokerHealth>,
    /// Present when a master key is configured; needed to store and resolve credentials.
    pub secrets: Option<Arc<PgSecretStore>>,
}

impl AppState {
    pub fn new(pool: PgPool, config: Config) -> Self {
        AppState {
            pool,
            config: Arc::new(config),
            broker: None,
            secrets: None,
        }
    }

    pub fn with_secrets(mut self, store: PgSecretStore) -> Self {
        self.secrets = Some(Arc::new(store));
        self
    }

    pub fn with_broker(mut self, health: BrokerHealth) -> Self {
        self.broker = Some(health);
        self
    }
}
