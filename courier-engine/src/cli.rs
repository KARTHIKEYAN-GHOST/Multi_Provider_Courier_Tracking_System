//! Command-line interface: `serve`, `migrate` and `admin` commands.

use anyhow::{Context, bail};
use clap::{Parser, Subcommand};
use sqlx::PgPool;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

use crate::{
    api,
    config::Config,
    db,
    messaging::{
        broker::{self, BrokerHealth, BrokerSettings},
        topology::Topology,
    },
    services::{
        api_keys,
        background::{run_reaper, run_retry_scheduler},
        close_attempt::RetryPolicy,
        credentials::{self, PgSecretStore},
        crypto::MasterKey,
        worker_tokens,
    },
    state::AppState,
    telemetry,
};

#[derive(Parser)]
#[command(name = "courier-engine", version, about = "Courier tracking Engine")]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Run the HTTP API (default)
    Serve,
    /// Apply database migrations
    Migrate,
    /// Manage users and API keys
    #[command(subcommand)]
    Admin(AdminCommand),
}

#[derive(Subcommand)]
enum AdminCommand {
    /// Create a user and print their first API key (shown once)
    CreateUser {
        #[arg(long)]
        email: String,
    },
    /// Create an additional API key for an existing user (shown once)
    CreateApiKey {
        #[arg(long)]
        email: String,
        /// Optional label, for example "laptop" or "ci"
        #[arg(long)]
        name: Option<String>,
    },
    /// Revoke an API key by its prefix (the part between the first and second underscore)
    RevokeApiKey {
        #[arg(long)]
        prefix: String,
    },
    /// Print a new random master key for CREDENTIAL_MASTER_KEY
    GenerateMasterKey,
    /// Store a provider's credential, replacing the one in use. The secret is a JSON object
    /// such as {"api_key": "..."} read from standard input, so it never appears in the
    /// shell history or the process list.
    SetCredential {
        #[arg(long)]
        provider: String,
        /// A name for the operator, for example "production key 2026"
        #[arg(long, default_value = "default")]
        label: String,
    },
    /// Revoke the provider's credential in use
    RevokeCredential {
        #[arg(long)]
        provider: String,
    },
    /// List stored credentials (never their secrets)
    ListCredentials,
    /// Create a token that lets a worker of one adapter resolve credentials (shown once)
    CreateWorkerToken {
        #[arg(long)]
        adapter: String,
        #[arg(long)]
        name: Option<String>,
    },
    /// Revoke a worker token by its prefix
    RevokeWorkerToken {
        #[arg(long)]
        prefix: String,
    },
    /// Record whether the owner of a provider's website has permitted scraping it
    SetScrapingAuthorized {
        #[arg(long)]
        provider: String,
        #[arg(long, action = clap::ArgAction::Set)]
        authorized: bool,
    },
}

/// The credential store, if a master key is configured.
fn secret_store(config: &Config) -> anyhow::Result<Option<PgSecretStore>> {
    let Some(hex_key) = config
        .credential_master_key
        .as_deref()
        .filter(|k| !k.is_empty())
    else {
        return Ok(None);
    };
    let master = MasterKey::from_hex(hex_key, config.credential_key_version)
        .context("CREDENTIAL_MASTER_KEY is not usable")?;
    Ok(Some(PgSecretStore::new(master)))
}

pub async fn run() -> anyhow::Result<()> {
    let cli = Cli::parse();
    if let Some(Command::Admin(AdminCommand::GenerateMasterKey)) = &cli.command {
        // Needs neither configuration nor a database.
        println!("{}", MasterKey::generate_hex());
        return Ok(());
    }
    let config = Config::from_env()?;
    telemetry::init(&config.log_format);

    let pool = db::connect(&config.database_url, config.db_max_connections)
        .await
        .context("cannot connect to PostgreSQL")?;

    match cli.command.unwrap_or(Command::Serve) {
        Command::Serve => serve(pool, config).await,
        Command::Migrate => {
            db::MIGRATOR.run(&pool).await.context("migration failed")?;
            println!("migrations applied");
            Ok(())
        }
        Command::Admin(command) => admin(&pool, &config, command).await,
    }
}

/// Runs the HTTP API together with the background work: the broker session (outbox relay,
/// event consumer, dead-letter consumer), the retry scheduler and the reaper.
async fn serve(pool: PgPool, config: Config) -> anyhow::Result<()> {
    let amqp_url = config
        .engine_amqp_url
        .clone()
        .context("ENGINE_AMQP_URL is not set; the Engine needs RabbitMQ to dispatch jobs")?;
    let policy = RetryPolicy {
        max_dead_letter_retries: config.max_dead_letter_retries,
    };
    let settings = BrokerSettings {
        amqp_url,
        topology: Topology::load()
            .map_err(anyhow::Error::msg)?
            .with_prefix(&config.engine_topology_prefix),
        policy,
        event_prefetch: config.event_prefetch,
        outbox_batch: config.outbox_batch,
        outbox_poll: Duration::from_millis(config.outbox_poll_ms),
    };

    let shutdown = CancellationToken::new();
    let health = BrokerHealth::default();
    let mut background = tokio::task::JoinSet::new();
    background.spawn(broker::run(
        settings,
        pool.clone(),
        health.clone(),
        shutdown.clone(),
    ));
    background.spawn(run_retry_scheduler(
        pool.clone(),
        Duration::from_millis(config.retry_scheduler_interval_ms),
        shutdown.clone(),
    ));
    background.spawn(run_reaper(
        pool.clone(),
        Duration::from_millis(config.reaper_interval_ms),
        policy,
        shutdown.clone(),
    ));

    // ENABLE_DUMMY_PROVIDER decides whether the test providers are offered at all.
    let changed =
        db::providers::set_test_providers_enabled(&pool, config.enable_dummy_provider).await?;
    tracing::info!(
        enabled = config.enable_dummy_provider,
        changed,
        "test providers switched according to ENABLE_DUMMY_PROVIDER"
    );

    let addr = config.engine_http_addr.clone();
    let internal_addr = config
        .engine_internal_addr
        .clone()
        .filter(|a| !a.is_empty());
    let mut state = AppState::new(pool, config.clone()).with_broker(health);
    if let Some(store) = secret_store(&config)? {
        state = state.with_secrets(store);
    }

    // The internal API (credential resolve for workers) has its own listener, so it can be
    // kept off any network that users can reach.
    if let Some(internal_addr) = internal_addr {
        if state.secrets.is_none() {
            tracing::warn!(
                "ENGINE_INTERNAL_ADDR is set but CREDENTIAL_MASTER_KEY is not: credentials cannot be resolved"
            );
        }
        let listener = tokio::net::TcpListener::bind(&internal_addr)
            .await
            .with_context(|| format!("cannot listen on {internal_addr}"))?;
        tracing::info!(addr = %internal_addr, "internal API listening");
        let app = api::internal::router(state.clone());
        let stop = shutdown.clone();
        background.spawn(async move {
            if let Err(error) = axum::serve(listener, app)
                .with_graceful_shutdown(async move { stop.cancelled().await })
                .await
            {
                tracing::error!(%error, "internal API stopped with an error");
            }
        });
    }

    let app = api::router(state);
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .with_context(|| format!("cannot listen on {addr}"))?;
    tracing::info!(%addr, "engine listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    // The API has drained; now let each background task finish the message it is on.
    shutdown.cancel();
    while background.join_next().await.is_some() {}
    tracing::info!("engine stopped");
    Ok(())
}

/// Resolves on Ctrl-C or SIGTERM, letting in-flight requests finish.
async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        if let Ok(mut signal) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            signal.recv().await;
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

fn normalize_email(raw: &str) -> anyhow::Result<String> {
    let email = raw.trim().to_lowercase();
    let valid = email.len() <= 254
        && email.split_once('@').is_some_and(|(local, domain)| {
            !local.is_empty() && domain.contains('.') && !domain.contains('@')
        })
        && !email.chars().any(char::is_whitespace);
    if !valid {
        bail!("not a valid email address");
    }
    Ok(email)
}

/// Admin commands print results to stdout. The API key is printed exactly once and is
/// never written to the log.
async fn admin(pool: &PgPool, config: &Config, command: AdminCommand) -> anyhow::Result<()> {
    match command {
        AdminCommand::CreateUser { email } => {
            let email = normalize_email(&email)?;
            let user_id = match db::api_keys::create_user(pool, &email).await {
                Ok(id) => id,
                Err(e) if db::is_unique_violation(&e) => {
                    bail!("a user with this email already exists")
                }
                Err(e) => return Err(e.into()),
            };
            let key = api_keys::issue(pool, user_id, Some("initial")).await?;
            println!("user_id: {user_id}");
            println!("api_key: {key}");
            println!("Store this key now. It cannot be shown again.");
        }
        AdminCommand::CreateApiKey { email, name } => {
            let email = normalize_email(&email)?;
            let Some(user_id) = db::api_keys::find_user_id_by_email(pool, &email).await? else {
                bail!("no user with this email");
            };
            let key = api_keys::issue(pool, user_id, name.as_deref()).await?;
            println!("api_key: {key}");
            println!("Store this key now. It cannot be shown again.");
        }
        AdminCommand::GenerateMasterKey => unreachable!("handled before the database is opened"),
        AdminCommand::SetCredential { provider, label } => {
            let store = secret_store(config)?
                .context("CREDENTIAL_MASTER_KEY is not set; credentials cannot be stored")?;
            let mut input = String::new();
            std::io::Read::read_to_string(
                &mut std::io::Read::take(std::io::stdin(), 64 * 1024),
                &mut input,
            )
            .context("cannot read the secret from standard input")?;
            // A parse error is reported without the input, which may contain the secret.
            let secret: credentials::SecretFields =
                serde_json::from_str(input.trim()).map_err(|_| {
                    anyhow::anyhow!(
                        "standard input must be a JSON object, for example {{\"api_key\": \"...\"}}"
                    )
                })?;
            let id = credentials::set_credential(pool, &store, &provider, &label, &secret).await?;
            println!("credential_id: {id}");
            println!("The previous credential of this provider, if any, has been revoked.");
        }
        AdminCommand::RevokeCredential { provider } => {
            if !credentials::revoke_credential(pool, &provider).await? {
                bail!("this provider has no credential in use");
            }
            println!("revoked");
        }
        AdminCommand::ListCredentials => {
            for c in credentials::list_credentials(pool).await? {
                let state = match c.revoked_at {
                    Some(at) => format!("revoked {}", at.format("%Y-%m-%d")),
                    None => "in use".to_string(),
                };
                println!(
                    "{}  {:<12} {:<24} key v{}  created {}  {}",
                    c.id,
                    c.provider_code,
                    c.label,
                    c.key_version,
                    c.created_at.format("%Y-%m-%d"),
                    state
                );
            }
        }
        AdminCommand::CreateWorkerToken { adapter, name } => {
            let Some(token) = worker_tokens::issue(pool, &adapter, name.as_deref()).await? else {
                bail!("no adapter with this key");
            };
            println!("worker_token: {token}");
            println!("Store this token now. It cannot be shown again.");
        }
        AdminCommand::RevokeWorkerToken { prefix } => {
            if !worker_tokens::revoke_by_prefix(pool, &prefix).await? {
                bail!("no active worker token with this prefix");
            }
            println!("revoked");
        }
        AdminCommand::SetScrapingAuthorized {
            provider,
            authorized,
        } => {
            let mut tx = pool.begin().await?;
            let Some(provider_id) = sqlx::query_scalar::<_, uuid::Uuid>(
                "UPDATE providers SET scraping_authorized = $2, updated_at = now() WHERE code = $1 RETURNING id",
            )
            .bind(&provider)
            .bind(authorized)
            .fetch_optional(&mut *tx)
            .await?
            else {
                bail!("no provider with this code");
            };
            credentials::audit(
                &mut tx,
                credentials::Audit {
                    actor_type: "admin",
                    action: "provider.scraping_authorized",
                    provider_id: Some(provider_id),
                    details: Some(serde_json::json!({ "authorized": authorized })),
                    ..credentials::Audit::default()
                },
            )
            .await?;
            tx.commit().await?;
            println!("scraping_authorized = {authorized} for {provider}");
        }
        AdminCommand::RevokeApiKey { prefix } => {
            if db::api_keys::revoke_by_prefix(pool, &prefix).await? {
                println!("revoked");
            } else {
                bail!("no active key with this prefix");
            }
        }
    }
    Ok(())
}
