//! Phase 12: a courier added before it has an adapter, and its adapter built by the AI.
//!
//! 1. Anyone signed in (an organization user or an admin) adds a courier: name, method,
//!    tracking address, notes, the website owner's permission for a website, and optionally
//!    a login. A row inserted straight into `providers` with status 'building' works too.
//! 2. [`open_build_tasks`] (run by the polling scheduler) sees a courier in 'building' with
//!    no adapter and no open task, and asks the AI repair service for an adapter.
//! 3. The AI's adapter arrives with a migration that inserts the adapter row and sets the
//!    courier 'live'. Tracking requests that waited start by themselves.
//! 4. After three failed tries the courier is 'failed'; `admin retry-provider` starts again.
//!
//! The Engine still runs no AI and no generated code: it only records the courier and opens
//! tasks, as for repairs.

use serde_json::json;
use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use super::credentials::{self, Audit, CredentialError, SecretFields, SecretStore, audit};
use super::repairs::{self, NewProvider, RequestError, valid_code, valid_host};

/// Tries per round before a courier is marked failed.
pub const MAX_BUILD_TRIES: i64 = 3;
/// After a failed try the next one waits this long (the site may be down for a moment).
pub const RETRY_AFTER_MINUTES: i32 = 10;
/// New couriers one organization may add per day. Admins are not limited.
pub const MAX_PER_ORGANIZATION_PER_DAY: i64 = 5;

/// Who adds the courier.
#[derive(Debug, Clone, Copy)]
pub struct Adder {
    pub user_id: Option<Uuid>,
    pub organization_id: Option<Uuid>,
    pub is_admin: bool,
}

/// A courier as somebody describes it.
#[derive(Debug, Clone, Default)]
pub struct NewCourier {
    /// Derived from the name when not given.
    pub code: Option<String>,
    pub name: String,
    /// `api` or `scrape`.
    pub method: String,
    /// The tracking page or API address the AI starts from.
    pub tracking_url: String,
    /// Further hosts it may contact, beside the tracking address's own.
    pub hosts: Vec<String>,
    pub notes: String,
    /// For a website: the owner has permitted automatic lookups.
    pub permitted: bool,
    /// Its API or website needs a login even if none is given now.
    pub needs_login: bool,
    /// The login (email, password, API key ...), stored encrypted for the adder's
    /// organization (or as the platform's login when an admin adds it).
    pub login: Option<SecretFields>,
    pub sample: Option<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum OnboardError {
    #[error("{0}")]
    Invalid(&'static str),
    #[error("a website is looked up automatically only with its owner's permission")]
    PermissionRequired,
    #[error("a courier with the code '{0}' exists already")]
    Exists(String),
    #[error("this organization has added the most couriers allowed for one day")]
    TooMany,
    #[error("logins cannot be stored: the Engine has no CREDENTIAL_MASTER_KEY")]
    NoMasterKey,
    #[error(transparent)]
    Credential(#[from] CredentialError),
    #[error(transparent)]
    Database(#[from] sqlx::Error),
}

#[derive(Debug, Clone)]
pub struct Added {
    pub code: String,
    pub provider_id: Uuid,
}

/// A code from a name: its letters and digits in lower case, starting with a letter.
pub fn code_from_name(name: &str) -> Option<String> {
    let code: String = name
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .map(|c| c.to_ascii_lowercase())
        .skip_while(char::is_ascii_digit)
        .take(30)
        .collect();
    valid_code(&code).then_some(code)
}

/// The host of an http(s) address, in lower case.
pub fn host_of(url: &str) -> Option<String> {
    let rest = url
        .strip_prefix("https://")
        .or_else(|| url.strip_prefix("http://"))?;
    let authority = rest.split(['/', '?', '#']).next()?;
    let host = authority
        .rsplit_once('@')
        .map_or(authority, |(_, h)| h)
        .split(':')
        .next()?
        .to_ascii_lowercase();
    valid_host(&host).then_some(host)
}

/// A host a user may name: a public DNS name, not an IP address and not an internal name
/// (the AI and the adapter must never be pointed into the deployment's own network).
fn public_host(host: &str) -> bool {
    valid_host(host)
        && host.parse::<std::net::IpAddr>().is_err()
        && host
            .rsplit('.')
            .next()
            .is_some_and(|tld| tld.chars().any(|c| c.is_ascii_lowercase()))
        && ![
            ".local",
            ".internal",
            ".localhost",
            ".lan",
            ".home",
            ".corp",
            ".intranet",
        ]
        .iter()
        .any(|suffix| host.ends_with(suffix))
}

fn valid_name(name: &str) -> bool {
    let name = name.trim();
    !name.is_empty() && name.len() <= 100 && !name.contains(['\'', '"', '\\', '\n'])
}

/// Adds a courier in 'building' and asks for its adapter at once.
pub async fn add_courier<S: SecretStore>(
    pool: &PgPool,
    store: Option<&S>,
    adder: Adder,
    input: NewCourier,
) -> Result<Added, OnboardError> {
    if !valid_name(&input.name) {
        return Err(OnboardError::Invalid(
            "name: 1-100 characters, without quotes, backslashes or line breaks",
        ));
    }
    if !matches!(input.method.as_str(), "api" | "scrape") {
        return Err(OnboardError::Invalid("method: api or scrape"));
    }
    let url = input.tracking_url.trim();
    if url.len() > 500 {
        return Err(OnboardError::Invalid(
            "tracking_url: at most 500 characters",
        ));
    }
    let Some(own_host) = host_of(url).filter(|h| public_host(h)) else {
        return Err(OnboardError::Invalid(
            "tracking_url: the http(s) address of the tracking page or API, on a public host",
        ));
    };
    let code = match input
        .code
        .as_deref()
        .map(str::trim)
        .filter(|c| !c.is_empty())
    {
        Some(code) if valid_code(code) => code.to_string(),
        Some(_) => {
            return Err(OnboardError::Invalid(
                "code: 1-30 lowercase letters and digits, starting with a letter",
            ));
        }
        None => code_from_name(&input.name).ok_or(OnboardError::Invalid(
            "code: the name has no letters to make a code from; give a code",
        ))?,
    };
    let mut hosts = vec![own_host];
    for host in &input.hosts {
        let host = host.trim().to_ascii_lowercase();
        if !public_host(&host) {
            return Err(OnboardError::Invalid(
                "hosts: public host names such as api.example.com (no scheme, no path, \
                 no IP address, no internal name)",
            ));
        }
        if !hosts.contains(&host) {
            hosts.push(host);
        }
    }
    if hosts.len() > 5 {
        return Err(OnboardError::Invalid("hosts: at most five"));
    }
    let notes = input.notes.trim();
    if notes.len() > 4000 {
        return Err(OnboardError::Invalid("notes: at most 4000 characters"));
    }
    let notes = if notes.is_empty() {
        format!("Tracked at {url}")
    } else {
        notes.to_string()
    };
    if input.sample.as_ref().is_some_and(|s| s.len() > 100_000) {
        return Err(OnboardError::Invalid("sample: at most 100000 characters"));
    }
    if input.method == "scrape" && !input.permitted {
        return Err(OnboardError::PermissionRequired);
    }
    if let Some(login) = &input.login {
        credentials::validate_secret(login)?;
        if store.is_none() {
            return Err(OnboardError::NoMasterKey);
        }
    }

    let mut tx = pool.begin().await?;
    if !adder.is_admin
        && let Some(organization) = adder.organization_id
    {
        // Serializes two additions of one organization, so the daily limit holds.
        sqlx::query("SELECT id FROM organizations WHERE id = $1 FOR UPDATE")
            .bind(organization)
            .execute(&mut *tx)
            .await?;
        let today: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM providers
              WHERE added_by_organization = $1 AND created_at > now() - interval '1 day'",
        )
        .bind(organization)
        .fetch_one(&mut *tx)
        .await?;
        if today >= MAX_PER_ORGANIZATION_PER_DAY {
            return Err(OnboardError::TooMany);
        }
    }
    let exists: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM providers WHERE code = $1)
             OR EXISTS (SELECT 1 FROM adapters WHERE key = $1)",
    )
    .bind(&code)
    .fetch_one(&mut *tx)
    .await?;
    if exists {
        return Err(OnboardError::Exists(code));
    }
    let provider_id = Uuid::now_v7();
    let permitted = input.method == "scrape" && input.permitted;
    sqlx::query(
        "INSERT INTO providers
             (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
              max_attempts, retry_base_ms, retry_max_ms, requires_credential,
              min_dispatch_interval_ms, status, tracking_url, hosts, notes, added_by,
              added_by_organization, scraping_authorized, permission_recorded_by,
              permission_recorded_at, build_started_at, build_sample)
         VALUES ($1, $2, $3, $4::tracking_method, 20000, 900000, 30000, 3, 5000, 300000, $5,
                 2000, 'building', $6, $7, $8, $9, $10, $11,
                 CASE WHEN $11 THEN $9 END, CASE WHEN $11 THEN now() END, now(), $12)",
    )
    .bind(provider_id)
    .bind(&code)
    .bind(input.name.trim())
    .bind(&input.method)
    .bind(input.needs_login || input.login.is_some())
    .bind(url)
    .bind(&hosts)
    .bind(&notes)
    .bind(adder.user_id)
    .bind(adder.organization_id)
    .bind(permitted)
    .bind(&input.sample)
    .execute(&mut *tx)
    .await?;
    audit(
        &mut tx,
        Audit {
            actor_type: if adder.is_admin { "admin" } else { "user" },
            actor_id: adder.user_id.map(|id| id.to_string()),
            action: "provider.added",
            allowed: true,
            provider_id: Some(provider_id),
            details: Some(
                json!({ "code": code, "method": input.method, "hosts": hosts,
                                  "organization_id": adder.organization_id,
                                  "permission_recorded": permitted,
                                  "login": input.login.is_some() }),
            ),
            ..Audit::default()
        },
    )
    .await?;
    tx.commit().await?;

    if let (Some(login), Some(store)) = (&input.login, store) {
        let organization = if adder.is_admin {
            None
        } else {
            adder.organization_id
        };
        let actor = if adder.is_admin { "admin" } else { "user" };
        credentials::set_credential_for(pool, store, &code, "login", login, organization, actor)
            .await?;
    }
    open_build_tasks(pool).await?;
    Ok(Added { code, provider_id })
}

#[derive(sqlx::FromRow)]
struct Building {
    id: Uuid,
    code: String,
    name: String,
    method: String,
    tracking_url: Option<String>,
    hosts: Vec<String>,
    notes: Option<String>,
    requires_credential: bool,
    sample: Option<String>,
    failed: i64,
    last_failure: Option<String>,
    waiting: bool,
    login: bool,
}

/// Opens a new-adapter task for every courier that is being built and has none open, marks
/// couriers with an adapter 'live' and couriers that failed too often 'failed'. Returns how
/// many tasks it opened. Safe to run from several Engine instances at once.
pub async fn open_build_tasks(pool: &PgPool) -> Result<usize, sqlx::Error> {
    // An adapter that arrived without setting the status (an older migration) still counts.
    sqlx::query(
        "UPDATE providers p SET status = 'live', build_failure = NULL, updated_at = now()
          WHERE p.status = 'building' AND EXISTS (SELECT 1 FROM adapters a WHERE a.provider_id = p.id)",
    )
    .execute(pool)
    .await?;

    let due = sqlx::query_as::<_, Building>(
        "SELECT p.id, p.code, p.name, p.tracking_method::text AS method, p.tracking_url, p.hosts,
                p.notes, p.requires_credential, p.build_sample AS sample,
                (SELECT count(*) FROM repair_tasks t
                  WHERE t.adapter_key = p.code AND t.kind = 'new_provider' AND t.status = 'failed'
                    AND t.created_at >= COALESCE(p.build_started_at, p.created_at)) AS failed,
                (SELECT t.failure_reason FROM repair_tasks t
                  WHERE t.adapter_key = p.code AND t.kind = 'new_provider' AND t.status = 'failed'
                  ORDER BY t.finished_at DESC NULLS LAST LIMIT 1) AS last_failure,
                EXISTS (SELECT 1 FROM repair_tasks t
                         WHERE t.adapter_key = p.code AND t.kind = 'new_provider'
                           AND t.status = 'failed'
                           AND t.finished_at > now() - make_interval(mins => $1)) AS waiting,
                EXISTS (SELECT 1 FROM provider_credentials c
                         WHERE c.provider_id = p.id AND c.revoked_at IS NULL) AS login
           FROM providers p
          WHERE p.status = 'building' AND p.enabled
            AND NOT EXISTS (SELECT 1 FROM repair_tasks t
                             WHERE t.adapter_key = p.code AND t.kind = 'new_provider'
                               AND (t.status IN ('pending', 'running')
                                    OR (t.status = 'published'
                                        AND t.created_at >= COALESCE(p.build_started_at, p.created_at))))",
    )
    .bind(RETRY_AFTER_MINUTES)
    .fetch_all(pool)
    .await?;

    let mut opened = 0;
    for courier in due {
        if courier.failed >= MAX_BUILD_TRIES {
            give_up(pool, courier.id, courier.last_failure.as_deref()).await?;
            continue;
        }
        if courier.waiting {
            continue;
        }
        let mut hosts = courier.hosts.clone();
        if let Some(host) = courier.tracking_url.as_deref().and_then(host_of)
            && !hosts.contains(&host)
        {
            hosts.insert(0, host);
        }
        hosts.retain(|h| valid_host(h));
        hosts.truncate(5);
        if hosts.is_empty() || !valid_name(&courier.name) || !valid_code(&courier.code) {
            give_up(
                pool,
                courier.id,
                Some("the courier has no usable tracking address, name or code"),
            )
            .await?;
            continue;
        }
        let notes = courier
            .notes
            .filter(|n| !n.trim().is_empty())
            .or_else(|| {
                courier
                    .tracking_url
                    .as_ref()
                    .map(|u| format!("Tracked at {u}"))
            })
            .unwrap_or_default();
        let spec = NewProvider {
            code: courier.code.clone(),
            name: courier.name.trim().to_string(),
            method: courier.method,
            hosts,
            needs_credential: courier.requires_credential,
            notes,
            sample: courier.sample,
            tracking_url: courier.tracking_url,
            login: courier.login,
        };
        match repairs::open_as(pool, "new_provider", &spec.code, json!(spec), "engine").await {
            Ok(_) => opened += 1,
            // Another Engine instance was quicker.
            Err(RequestError::AlreadyOpen) => {}
            Err(RequestError::Database(error)) => return Err(error),
            Err(RequestError::Invalid(_)) => {}
        }
    }
    Ok(opened)
}

async fn give_up(pool: &PgPool, id: Uuid, reason: Option<&str>) -> Result<(), sqlx::Error> {
    let reason = reason.unwrap_or("the AI could not build an adapter");
    sqlx::query(
        "UPDATE providers SET status = 'failed', build_failure = $2, updated_at = now()
          WHERE id = $1 AND status = 'building'",
    )
    .bind(id)
    .bind(reason.chars().take(2000).collect::<String>())
    .execute(pool)
    .await?;
    tracing::warn!(provider_id = %id, "no adapter could be built; the courier is marked failed");
    Ok(())
}

/// Puts a failed courier back to 'building' for a new round of tries, and lets its waiting
/// requests wait again. Returns false when there is no failed courier with this code.
pub async fn retry(pool: &PgPool, code: &str) -> Result<bool, sqlx::Error> {
    let mut tx = pool.begin().await?;
    let id: Option<Uuid> = sqlx::query_scalar(
        "UPDATE providers SET status = 'building', build_failure = NULL, build_started_at = now(),
                              updated_at = now()
          WHERE code = $1 AND status = 'failed' RETURNING id",
    )
    .bind(code)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(id) = id else {
        return Ok(false);
    };
    requeue_waiting(&mut tx, id).await?;
    tx.commit().await?;
    open_build_tasks(pool).await?;
    Ok(true)
}

/// Shipments with a subscriber whose lookup never started are looked at again soon.
async fn requeue_waiting(conn: &mut PgConnection, provider_id: Uuid) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE shipments s SET next_poll_at = clock_timestamp() + interval '60 seconds'
          WHERE s.provider_id = $1 AND s.archived_at IS NULL AND s.next_poll_at IS NULL
            AND EXISTS (SELECT 1 FROM tracking_requests r
                         WHERE r.shipment_id = s.id AND r.status = 'active')",
    )
    .bind(provider_id)
    .execute(conn)
    .await?;
    Ok(())
}
