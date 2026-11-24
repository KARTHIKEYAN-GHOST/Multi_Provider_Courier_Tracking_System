//! Provider credentials: storing them encrypted and handing them to a worker for one job.
//!
//! Secrets never travel through RabbitMQ. A `job.track` message carries only an opaque
//! `credential_ref`; the worker exchanges it for the secret over the Engine's internal HTTP
//! API. That exchange is allowed only for a worker whose token belongs to the job's adapter,
//! and only while the named attempt of the job is in flight. Every exchange, allowed or
//! denied, is written to the audit log — without the secret.

use std::future::Future;

use chrono::{DateTime, Utc};
use serde_json::{Map, Value, json};
use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use super::crypto::{CryptoError, MasterKey, Sealed};
use super::worker_tokens::WorkerIdentity;

/// A secret is a small JSON object of named text values, e.g. `{"api_key": "..."}`.
pub type SecretFields = Map<String, Value>;

const MAX_SECRET_BYTES: usize = 8192;
const MAX_SECRET_FIELDS: usize = 20;

#[derive(Debug, thiserror::Error)]
pub enum CredentialError {
    #[error("unknown provider")]
    UnknownProvider,
    #[error("invalid secret: {0}")]
    InvalidSecret(&'static str),
    #[error(transparent)]
    Crypto(#[from] CryptoError),
    #[error(transparent)]
    Database(#[from] sqlx::Error),
}

/// Where secrets are kept. The Engine's own implementation is [`PgSecretStore`]; an external
/// secrets manager (for example Vault) can replace it by implementing this trait.
pub trait SecretStore: Send + Sync {
    /// Stores a secret under the given credential ID.
    fn put(
        &self,
        conn: &mut PgConnection,
        credential_id: Uuid,
        provider_id: Uuid,
        label: &str,
        secret: &SecretFields,
    ) -> impl Future<Output = Result<(), CredentialError>> + Send;

    /// Reads a secret back. `None` if the credential does not exist or is revoked.
    fn get(
        &self,
        conn: &mut PgConnection,
        credential_id: Uuid,
    ) -> impl Future<Output = Result<Option<SecretFields>, CredentialError>> + Send;
}

/// Secrets in PostgreSQL, envelope-encrypted with the master key from the environment.
#[derive(Clone, Debug)]
pub struct PgSecretStore {
    master: MasterKey,
}

/// Ties a ciphertext to its row: it cannot be decrypted after being copied to another one.
fn binding(credential_id: Uuid, provider_id: Uuid) -> Vec<u8> {
    format!("credential:{credential_id}:provider:{provider_id}").into_bytes()
}

#[derive(sqlx::FromRow)]
struct StoredSecret {
    provider_id: Uuid,
    key_version: i32,
    wrapped_key: Vec<u8>,
    key_nonce: Vec<u8>,
    ciphertext: Vec<u8>,
    nonce: Vec<u8>,
}

impl PgSecretStore {
    pub fn new(master: MasterKey) -> Self {
        PgSecretStore { master }
    }
}

impl SecretStore for PgSecretStore {
    async fn put(
        &self,
        conn: &mut PgConnection,
        credential_id: Uuid,
        provider_id: Uuid,
        label: &str,
        secret: &SecretFields,
    ) -> Result<(), CredentialError> {
        let plaintext = serde_json::to_vec(secret).expect("a JSON map always serializes");
        let sealed = self
            .master
            .seal(&plaintext, &binding(credential_id, provider_id));
        sqlx::query(
            "INSERT INTO provider_credentials
                 (id, provider_id, label, key_version, wrapped_key, key_nonce, ciphertext, nonce)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        )
        .bind(credential_id)
        .bind(provider_id)
        .bind(label)
        .bind(sealed.key_version)
        .bind(&sealed.wrapped_key)
        .bind(sealed.key_nonce.as_slice())
        .bind(&sealed.ciphertext)
        .bind(sealed.nonce.as_slice())
        .execute(conn)
        .await?;
        Ok(())
    }

    async fn get(
        &self,
        conn: &mut PgConnection,
        credential_id: Uuid,
    ) -> Result<Option<SecretFields>, CredentialError> {
        let Some(row) = sqlx::query_as::<_, StoredSecret>(
            "SELECT provider_id, key_version, wrapped_key, key_nonce, ciphertext, nonce
             FROM provider_credentials WHERE id = $1 AND revoked_at IS NULL",
        )
        .bind(credential_id)
        .fetch_optional(conn)
        .await?
        else {
            return Ok(None);
        };
        let sealed = Sealed {
            key_version: row.key_version,
            wrapped_key: row.wrapped_key,
            key_nonce: row.key_nonce.try_into().map_err(|_| CryptoError::Decrypt)?,
            ciphertext: row.ciphertext,
            nonce: row.nonce.try_into().map_err(|_| CryptoError::Decrypt)?,
        };
        let plaintext = self
            .master
            .open(&sealed, &binding(credential_id, row.provider_id))?;
        let secret = serde_json::from_slice(&plaintext).map_err(|_| CryptoError::Decrypt)?;
        Ok(Some(secret))
    }
}

/// Checks the shape of a secret before it is stored.
pub fn validate_secret(secret: &SecretFields) -> Result<(), CredentialError> {
    let is_name = |name: &str| {
        let mut chars = name.chars();
        chars.next().is_some_and(|c| c.is_ascii_lowercase())
            && name.len() <= 50
            && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
    };
    if secret.is_empty() {
        return Err(CredentialError::InvalidSecret("it has no fields"));
    }
    if secret.len() > MAX_SECRET_FIELDS {
        return Err(CredentialError::InvalidSecret("it has too many fields"));
    }
    if !secret.keys().all(|name| is_name(name)) {
        return Err(CredentialError::InvalidSecret(
            "field names must be lowercase letters, digits and underscores",
        ));
    }
    if !secret
        .values()
        .all(|v| v.as_str().is_some_and(|s| !s.is_empty()))
    {
        return Err(CredentialError::InvalidSecret(
            "every field must be non-empty text",
        ));
    }
    if serde_json::to_vec(secret).map_or(true, |bytes| bytes.len() > MAX_SECRET_BYTES) {
        return Err(CredentialError::InvalidSecret("it is too large"));
    }
    Ok(())
}

/// One audit-log entry. Never give it a secret.
pub struct Audit<'a> {
    pub actor_type: &'a str,
    pub actor_id: Option<String>,
    pub action: &'a str,
    pub allowed: bool,
    pub reason: Option<&'a str>,
    pub provider_id: Option<Uuid>,
    pub credential_id: Option<Uuid>,
    pub job_id: Option<Uuid>,
    pub attempt: Option<i32>,
    pub details: Option<Value>,
}

impl Default for Audit<'_> {
    fn default() -> Self {
        Audit {
            actor_type: "engine",
            actor_id: None,
            action: "",
            allowed: true,
            reason: None,
            provider_id: None,
            credential_id: None,
            job_id: None,
            attempt: None,
            details: None,
        }
    }
}

pub async fn audit(conn: &mut PgConnection, entry: Audit<'_>) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO audit_log (id, actor_type, actor_id, action, outcome, reason, provider_id,
                                credential_id, job_id, attempt, details)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
    )
    .bind(Uuid::now_v7())
    .bind(entry.actor_type)
    .bind(entry.actor_id)
    .bind(entry.action)
    .bind(if entry.allowed { "allowed" } else { "denied" })
    .bind(entry.reason)
    .bind(entry.provider_id)
    .bind(entry.credential_id)
    .bind(entry.job_id)
    .bind(entry.attempt)
    .bind(entry.details)
    .execute(conn)
    .await?;
    Ok(())
}

/// Stores a provider's credential, replacing (revoking) the one in use. Returns the new ID.
pub async fn set_credential(
    pool: &PgPool,
    store: &impl SecretStore,
    provider_code: &str,
    label: &str,
    secret: &SecretFields,
) -> Result<Uuid, CredentialError> {
    validate_secret(secret)?;
    let mut tx = pool.begin().await?;
    // Locking the provider row serializes two simultaneous replacements.
    let provider_id =
        sqlx::query_scalar::<_, Uuid>("SELECT id FROM providers WHERE code = $1 FOR UPDATE")
            .bind(provider_code)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or(CredentialError::UnknownProvider)?;
    sqlx::query(
        "UPDATE provider_credentials SET revoked_at = now() WHERE provider_id = $1 AND revoked_at IS NULL",
    )
    .bind(provider_id)
    .execute(&mut *tx)
    .await?;
    let credential_id = Uuid::now_v7();
    store
        .put(&mut tx, credential_id, provider_id, label, secret)
        .await?;
    audit(
        &mut tx,
        Audit {
            actor_type: "admin",
            action: "credential.set",
            provider_id: Some(provider_id),
            credential_id: Some(credential_id),
            // Field names help an operator; values are never recorded.
            details: Some(json!({ "label": label, "fields": secret.keys().collect::<Vec<_>>() })),
            ..Audit::default()
        },
    )
    .await?;
    tx.commit().await?;
    Ok(credential_id)
}

/// Revokes the provider's credential in use. Returns whether there was one.
pub async fn revoke_credential(
    pool: &PgPool,
    provider_code: &str,
) -> Result<bool, CredentialError> {
    let mut tx = pool.begin().await?;
    let provider_id = sqlx::query_scalar::<_, Uuid>("SELECT id FROM providers WHERE code = $1")
        .bind(provider_code)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or(CredentialError::UnknownProvider)?;
    let revoked = sqlx::query_scalar::<_, Uuid>(
        "UPDATE provider_credentials SET revoked_at = now()
         WHERE provider_id = $1 AND revoked_at IS NULL RETURNING id",
    )
    .bind(provider_id)
    .fetch_optional(&mut *tx)
    .await?;
    if let Some(credential_id) = revoked {
        audit(
            &mut tx,
            Audit {
                actor_type: "admin",
                action: "credential.revoke",
                provider_id: Some(provider_id),
                credential_id: Some(credential_id),
                ..Audit::default()
            },
        )
        .await?;
    }
    tx.commit().await?;
    Ok(revoked.is_some())
}

/// What an operator may see of a credential: everything except the secret.
#[derive(Debug, sqlx::FromRow)]
pub struct CredentialSummary {
    pub id: Uuid,
    pub provider_code: String,
    pub label: String,
    pub key_version: i32,
    pub created_at: DateTime<Utc>,
    pub revoked_at: Option<DateTime<Utc>>,
}

pub async fn list_credentials(pool: &PgPool) -> Result<Vec<CredentialSummary>, sqlx::Error> {
    sqlx::query_as::<_, CredentialSummary>(
        "SELECT c.id, p.code AS provider_code, c.label, c.key_version, c.created_at, c.revoked_at
         FROM provider_credentials c JOIN providers p ON p.id = c.provider_id
         ORDER BY p.code, c.created_at DESC",
    )
    .fetch_all(pool)
    .await
}

/// A worker's request to exchange a credential reference for the secret.
#[derive(Debug, Clone, Copy)]
pub struct ResolveRequest {
    pub credential_ref: Uuid,
    pub job_id: Uuid,
    pub attempt: i32,
}

#[derive(Debug)]
pub enum Resolved {
    Secret(SecretFields),
    /// The reason is for the audit log and the Engine's own log, not for the caller.
    Denied(&'static str),
}

#[derive(sqlx::FromRow)]
struct JobForResolve {
    status: String,
    attempt: i32,
    adapter_id: Uuid,
    provider_id: Uuid,
}

/// Decides whether `worker` may have the secret for this job, and returns it if so.
///
/// Allowed only when all of these hold:
///   1. the job exists and belongs to the adapter the worker's token is bound to;
///   2. the named attempt is the job's current attempt and is in flight (QUEUED or PROCESSING);
///   3. the reference names a credential of the job's provider;
///   4. that provider has a credential in use (if it was replaced since dispatch, the
///      current one is returned).
///
/// Every call writes one audit-log row.
pub async fn resolve(
    pool: &PgPool,
    store: &impl SecretStore,
    worker: &WorkerIdentity,
    request: ResolveRequest,
) -> Result<Resolved, CredentialError> {
    let mut conn = pool.acquire().await?;
    let job = sqlx::query_as::<_, JobForResolve>(
        "SELECT j.status::text AS status, j.attempt, j.adapter_id, s.provider_id
         FROM tracking_jobs j JOIN shipments s ON s.id = j.shipment_id
         WHERE j.id = $1",
    )
    .bind(request.job_id)
    .fetch_optional(&mut *conn)
    .await?;

    let mut provider_id = None;
    let mut credential_id = None;
    let decision: Result<SecretFields, &'static str> = 'decide: {
        let Some(job) = job else {
            break 'decide Err("unknown_job");
        };
        if job.adapter_id != worker.adapter_id {
            break 'decide Err("job_belongs_to_another_adapter");
        }
        provider_id = Some(job.provider_id);
        if job.attempt != request.attempt || !matches!(job.status.as_str(), "QUEUED" | "PROCESSING")
        {
            break 'decide Err("attempt_not_in_flight");
        }
        let reference_is_valid = sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS (SELECT 1 FROM provider_credentials WHERE id = $1 AND provider_id = $2)",
        )
        .bind(request.credential_ref)
        .bind(job.provider_id)
        .fetch_one(&mut *conn)
        .await?;
        if !reference_is_valid {
            break 'decide Err("reference_not_for_this_provider");
        }
        let active = sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM provider_credentials WHERE provider_id = $1 AND revoked_at IS NULL",
        )
        .bind(job.provider_id)
        .fetch_optional(&mut *conn)
        .await?;
        let Some(active) = active else {
            break 'decide Err("no_credential_in_use");
        };
        credential_id = Some(active);
        match store.get(&mut conn, active).await {
            Ok(Some(secret)) => Ok(secret),
            Ok(None) => Err("no_credential_in_use"),
            Err(CredentialError::Crypto(error)) => {
                tracing::error!(%error, credential_id = %active, "stored credential cannot be decrypted");
                Err("decryption_failed")
            }
            Err(other) => return Err(other),
        }
    };

    audit(
        &mut conn,
        Audit {
            actor_type: "worker",
            actor_id: Some(worker.token_id.to_string()),
            action: "credential.resolve",
            allowed: decision.is_ok(),
            reason: decision.as_ref().err().copied(),
            provider_id,
            credential_id,
            job_id: Some(request.job_id),
            attempt: Some(request.attempt),
            details: Some(json!({ "adapter_key": worker.adapter_key })),
        },
    )
    .await?;

    Ok(match decision {
        Ok(secret) => Resolved::Secret(secret),
        Err(reason) => {
            tracing::warn!(reason, job_id = %request.job_id, adapter = %worker.adapter_key, "credential resolve denied");
            Resolved::Denied(reason)
        }
    })
}
