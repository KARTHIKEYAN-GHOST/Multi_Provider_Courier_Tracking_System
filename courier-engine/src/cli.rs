//! Command-line interface: `serve`, `migrate` and `admin` commands.

use anyhow::{Context, bail};
use clap::{Parser, Subcommand};
use sqlx::PgPool;

use crate::{api, config::Config, db, services::api_keys, state::AppState, telemetry};

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
}

pub async fn run() -> anyhow::Result<()> {
    let cli = Cli::parse();
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
        Command::Admin(command) => admin(&pool, command).await,
    }
}

async fn serve(pool: PgPool, config: Config) -> anyhow::Result<()> {
    let addr = config.engine_http_addr.clone();
    let app = api::router(AppState::new(pool, config));
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .with_context(|| format!("cannot listen on {addr}"))?;
    tracing::info!(%addr, "engine listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
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
async fn admin(pool: &PgPool, command: AdminCommand) -> anyhow::Result<()> {
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
