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
        background::{run_maintenance_loop, run_poll_scheduler, run_reaper, run_retry_scheduler},
        close_attempt::RetryPolicy,
        credentials::{self, PgSecretStore},
        crypto::MasterKey,
        mappings, retention, worker_tokens,
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
    /// Manage organizations, users, API keys and the platform
    #[command(subcommand)]
    Admin(AdminCommand),
}

#[derive(Subcommand)]
enum AdminCommand {
    /// Create an organization (prints its id; an existing one with that name is kept)
    CreateOrganization {
        #[arg(long)]
        name: String,
    },
    /// Create a user and print their first API key (shown once). Give either the
    /// organization the user belongs to, or --admin for an admin of the platform.
    #[command(group(clap::ArgGroup::new("membership").required(true).args(["organization", "admin"])))]
    CreateUser {
        #[arg(long)]
        email: String,
        /// The organization's name (create it first with create-organization)
        #[arg(long)]
        organization: Option<String>,
        /// An admin: answers every organization's one-time codes and CAPTCHAs
        #[arg(long)]
        admin: bool,
    },
    /// Change a user's role: --role admin, or --role org-user with --organization
    SetRole {
        #[arg(long)]
        email: String,
        #[arg(long, value_parser = ["admin", "org-user"])]
        role: String,
        #[arg(long, required_if_eq("role", "org-user"))]
        organization: Option<String>,
    },
    /// List users with their role and organization
    ListUsers,
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
    /// Ask the AI repair service to write an adapter for a provider that has none yet
    RequestAdapter {
        /// The new provider's code: lowercase letters and digits (also the adapter's name)
        #[arg(long)]
        code: String,
        /// The courier's name as shown to users
        #[arg(long)]
        name: String,
        /// How it is tracked: api or scrape
        #[arg(long)]
        method: String,
        /// Host names the adapter may contact, separated by commas (e.g. api.example.com)
        #[arg(long, value_delimiter = ',')]
        hosts: Vec<String>,
        /// The provider's API needs a credential (stored later with set-credential)
        #[arg(long)]
        needs_credential: bool,
        /// How the provider is tracked: address, parameters, what the answer looks like
        #[arg(long)]
        notes: String,
        /// A file with an example answer of the provider (JSON or HTML), if you have one
        #[arg(long)]
        sample_file: Option<std::path::PathBuf>,
    },
    /// Ask for a repair of an adapter now, from its stored unreadable answers
    RequestRepair {
        #[arg(long)]
        adapter: String,
    },
    /// List the most recent AI repair tasks
    ListRepairs,
    /// Record whether the owner of a provider's website has permitted scraping it
    SetScrapingAuthorized {
        #[arg(long)]
        provider: String,
        #[arg(long, action = clap::ArgAction::Set)]
        authorized: bool,
    },
    /// List the raw-status mappings (optionally of one provider)
    ListStatusMappings {
        #[arg(long)]
        provider: Option<String>,
    },
    /// List provider wordings that events carry but no mapping covers, most frequent first
    ListUnmappedStatuses {
        #[arg(long)]
        provider: Option<String>,
    },
    /// Map a provider wording to a normalized status and apply it to the stored events
    SetStatusMapping {
        #[arg(long)]
        provider: String,
        /// The provider's wording, exactly as stored (case-sensitive)
        #[arg(long)]
        raw_status: String,
        /// CREATED, PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, RETURNED, EXCEPTION or UNKNOWN
        #[arg(long)]
        status: String,
    },
    /// Remove a mapping; stored events with that wording become UNKNOWN again
    RemoveStatusMapping {
        #[arg(long)]
        provider: String,
        #[arg(long)]
        raw_status: String,
    },
    /// Set how a provider's shipments are re-polled. --interval-ms 0 switches polling off.
    SetPollPolicy {
        #[arg(long)]
        provider: String,
        /// Time until the next lookup, in milliseconds
        #[arg(long)]
        interval_ms: i32,
        /// Upper limit of the interval while nothing changes (default: same as --interval-ms)
        #[arg(long)]
        max_interval_ms: Option<i32>,
        /// Stop polling a shipment that showed nothing new for this many days
        #[arg(long)]
        give_up_days: Option<i32>,
    },
    /// Run archiving and retention once, now, and print what was done
    RunMaintenance,
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
    background.spawn(run_poll_scheduler(
        pool.clone(),
        Duration::from_millis(config.poll_scheduler_interval_ms),
        shutdown.clone(),
    ));
    background.spawn(run_maintenance_loop(
        pool.clone(),
        Duration::from_millis(config.maintenance_interval_ms),
        config.retention(),
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

/// An admin when no organization is named, otherwise a user of that existing organization.
async fn membership(
    pool: &PgPool,
    organization: Option<&str>,
) -> anyhow::Result<db::api_keys::Membership> {
    let Some(name) = organization else {
        return Ok(db::api_keys::Membership::Admin);
    };
    match db::api_keys::find_organization_id(pool, name.trim()).await? {
        Some(id) => Ok(db::api_keys::Membership::Organization(id)),
        None => bail!(
            "no organization named {name:?} (create it with: admin create-organization --name ...)"
        ),
    }
}

/// Admin commands print results to stdout. The API key is printed exactly once and is
/// never written to the log.
async fn admin(pool: &PgPool, config: &Config, command: AdminCommand) -> anyhow::Result<()> {
    match command {
        AdminCommand::CreateOrganization { name } => {
            let name = name.trim();
            if name.is_empty() || name.chars().count() > 200 {
                bail!("the name must have 1 to 200 characters");
            }
            if let Some(id) = db::api_keys::find_organization_id(pool, name).await? {
                println!("organization_id: {id}");
                println!("An organization with this name already exists; it was kept.");
            } else {
                let id = db::api_keys::create_organization(pool, name).await?;
                println!("organization_id: {id}");
            }
        }
        AdminCommand::CreateUser {
            email,
            organization,
            admin: _,
        } => {
            let email = normalize_email(&email)?;
            let membership = membership(pool, organization.as_deref()).await?;
            let user_id = match db::api_keys::create_user(pool, &email, membership).await {
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
        AdminCommand::SetRole {
            email,
            role,
            organization,
        } => {
            let email = normalize_email(&email)?;
            let organization = if role == "admin" { None } else { organization };
            let membership = membership(pool, organization.as_deref()).await?;
            if !db::api_keys::set_membership(pool, &email, membership).await? {
                bail!("no user with this email");
            }
            println!(
                "{email}: {role}{}",
                organization.map(|o| format!(" of {o}")).unwrap_or_default()
            );
        }
        AdminCommand::ListUsers => {
            let rows: Vec<(String, String, Option<String>, String)> = sqlx::query_as(
                "SELECT u.email, u.role::text, o.name, u.status::text
                 FROM users u LEFT JOIN organizations o ON o.id = u.organization_id
                 ORDER BY o.name NULLS FIRST, u.email",
            )
            .fetch_all(pool)
            .await?;
            for (email, role, organization, status) in rows {
                let organization = organization.unwrap_or_else(|| "-".into());
                println!("{email:40} {role:9} {organization:30} {status}");
            }
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
        AdminCommand::RequestAdapter {
            code,
            name,
            method,
            hosts,
            needs_credential,
            notes,
            sample_file,
        } => {
            let sample = match sample_file {
                Some(path) => Some(std::fs::read_to_string(&path).map_err(|e| {
                    anyhow::anyhow!("cannot read the sample file {}: {e}", path.display())
                })?),
                None => None,
            };
            let spec = crate::services::repairs::NewProvider {
                code,
                name,
                method,
                hosts,
                needs_credential,
                notes,
                sample,
            };
            let id = crate::services::repairs::request_new_provider(pool, &spec)
                .await
                .map_err(|e| anyhow::anyhow!("{e}"))?;
            println!("repair task {id} created for new provider {}", spec.code);
        }
        AdminCommand::RequestRepair { adapter } => {
            let id = crate::services::repairs::request_repair(pool, &adapter)
                .await
                .map_err(|e| anyhow::anyhow!("{e}"))?;
            println!("repair task {id} created for adapter {adapter}");
        }
        AdminCommand::ListRepairs => {
            for task in crate::services::repairs::list(pool, 50).await? {
                println!(
                    "{}  {:<12} {:<14} {:<9} attempts={} {}",
                    task.created_at.format("%Y-%m-%d %H:%M"),
                    task.kind,
                    task.adapter_key,
                    task.status,
                    task.attempts,
                    task.commit_sha
                        .or(task.failure_reason)
                        .or(task.summary)
                        .unwrap_or_default()
                );
            }
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
        AdminCommand::ListStatusMappings { provider } => {
            for m in mappings::list_mappings(pool, provider.as_deref()).await? {
                println!(
                    "{:<12} {:<32} -> {:<17} {:<8} {}",
                    m.provider_code,
                    m.raw_status,
                    format!("{:?}", m.normalized_status),
                    m.source,
                    m.updated_at.format("%Y-%m-%d")
                );
            }
        }
        AdminCommand::ListUnmappedStatuses { provider } => {
            let unmapped = mappings::list_unmapped(pool, provider.as_deref()).await?;
            if unmapped.is_empty() {
                println!("every status seen so far has a mapping");
            }
            for u in unmapped {
                // Provider text is shown as data: control characters removed, length bounded.
                let sample: String = u
                    .sample_description
                    .unwrap_or_default()
                    .chars()
                    .filter(|c| !c.is_control())
                    .take(80)
                    .collect();
                println!(
                    "{:<12} {:?}  events: {}  shipments: {}  last seen: {}  e.g. {:?}",
                    u.provider_code,
                    u.raw_status,
                    u.events,
                    u.shipments,
                    u.last_seen_at.format("%Y-%m-%d"),
                    sample
                );
            }
        }
        AdminCommand::SetStatusMapping {
            provider,
            raw_status,
            status,
        } => {
            let normalized: crate::domain::ShipmentStatus =
                serde_json::from_value(serde_json::Value::String(status.to_uppercase()))
                    .map_err(|_| anyhow::anyhow!("unknown status; see --help for the list"))?;
            let applied =
                mappings::set_mapping(pool, &provider, &raw_status, normalized, "manual").await?;
            println!(
                "mapping stored; events updated: {}, shipments whose status changed: {}",
                applied.events_updated, applied.shipments_changed
            );
        }
        AdminCommand::RemoveStatusMapping {
            provider,
            raw_status,
        } => match mappings::remove_mapping(pool, &provider, &raw_status).await? {
            Some(applied) => println!(
                "mapping removed; events updated: {}, shipments whose status changed: {}",
                applied.events_updated, applied.shipments_changed
            ),
            None => bail!("no such mapping"),
        },
        AdminCommand::SetPollPolicy {
            provider,
            interval_ms,
            max_interval_ms,
            give_up_days,
        } => {
            let max_interval_ms = max_interval_ms.unwrap_or(interval_ms);
            if interval_ms < 0 || max_interval_ms < interval_ms {
                bail!("--interval-ms must not be negative and --max-interval-ms not below it");
            }
            if give_up_days.is_some_and(|d| d < 1) {
                bail!("--give-up-days must be at least 1");
            }
            let mut tx = pool.begin().await?;
            let Some(provider_id) = sqlx::query_scalar::<_, uuid::Uuid>(
                "UPDATE providers SET poll_interval_ms = $2, poll_max_interval_ms = $3,
                        poll_give_up_days = COALESCE($4, poll_give_up_days), updated_at = now()
                 WHERE code = $1 RETURNING id",
            )
            .bind(&provider)
            .bind(interval_ms)
            .bind(max_interval_ms)
            .bind(give_up_days)
            .fetch_optional(&mut *tx)
            .await?
            else {
                bail!("no provider with this code");
            };
            // Shipments already being tracked pick the new policy up now, not after their
            // next (possibly distant, possibly never scheduled) lookup.
            let shipments: Vec<uuid::Uuid> = sqlx::query_scalar(
                "SELECT id FROM shipments
                 WHERE provider_id = $1 AND archived_at IS NULL
                   AND current_status NOT IN ('DELIVERED', 'RETURNED')
                 ORDER BY id FOR UPDATE",
            )
            .bind(provider_id)
            .fetch_all(&mut *tx)
            .await?;
            for shipment_id in &shipments {
                crate::services::polling::schedule_next_poll(&mut tx, *shipment_id).await?;
            }
            credentials::audit(
                &mut tx,
                credentials::Audit {
                    actor_type: "admin",
                    action: "provider.poll_policy",
                    provider_id: Some(provider_id),
                    details: Some(serde_json::json!({
                        "interval_ms": interval_ms, "max_interval_ms": max_interval_ms,
                        "give_up_days": give_up_days, "shipments_rescheduled": shipments.len(),
                    })),
                    ..credentials::Audit::default()
                },
            )
            .await?;
            tx.commit().await?;
            println!(
                "poll policy stored; open shipments rescheduled: {}",
                shipments.len()
            );
        }
        AdminCommand::RunMaintenance => {
            let report = retention::run_maintenance(pool, &config.retention()).await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
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
