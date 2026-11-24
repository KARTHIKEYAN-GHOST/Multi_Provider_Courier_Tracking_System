//! Structured logging. Logs go to stderr so stdout stays clean for CLI output
//! (for example the API key printed by `admin create-user`).

use tracing_subscriber::{EnvFilter, fmt, layer::SubscriberExt, util::SubscriberInitExt};

pub fn init(log_format: &str) {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let registry = tracing_subscriber::registry().with(filter);
    let result = if log_format == "pretty" {
        registry
            .with(fmt::layer().with_writer(std::io::stderr))
            .try_init()
    } else {
        registry
            .with(
                fmt::layer()
                    .json()
                    .flatten_event(true)
                    .with_current_span(true)
                    .with_span_list(false)
                    .with_writer(std::io::stderr),
            )
            .try_init()
    };
    // Already initialised (for example in tests): keep the existing subscriber.
    let _ = result;
}
