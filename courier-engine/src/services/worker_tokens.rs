//! Worker tokens: how an adapter worker identifies itself to the Engine's internal API.
//!
//! A token looks like `cwt_<12 hex prefix>_<64 hex secret>` and is bound to one adapter.
//! Like user API keys, only the prefix and the SHA-256 hash are stored.

use sqlx::PgPool;
use subtle::ConstantTimeEq;
use uuid::Uuid;

use super::api_keys::{generate_for, hash, parse_prefix_for};

const SCHEME: &str = "cwt";

/// The authenticated worker.
#[derive(Debug, Clone)]
pub struct WorkerIdentity {
    pub token_id: Uuid,
    pub adapter_id: Uuid,
    pub adapter_key: String,
}

/// Creates a token for an adapter and returns it (the only time it is available).
/// `None` if no adapter has this key.
pub async fn issue(
    pool: &PgPool,
    adapter_key: &str,
    name: Option<&str>,
) -> Result<Option<String>, sqlx::Error> {
    let Some(adapter_id) = sqlx::query_scalar::<_, Uuid>("SELECT id FROM adapters WHERE key = $1")
        .bind(adapter_key)
        .fetch_optional(pool)
        .await?
    else {
        return Ok(None);
    };
    let token = generate_for(SCHEME);
    sqlx::query(
        "INSERT INTO worker_tokens (id, adapter_id, prefix, token_hash, name) VALUES ($1, $2, $3, $4, $5)",
    )
    .bind(Uuid::now_v7())
    .bind(adapter_id)
    .bind(&token.prefix)
    .bind(token.hash.as_slice())
    .bind(name)
    .execute(pool)
    .await?;
    Ok(Some(token.full))
}

#[derive(sqlx::FromRow)]
struct TokenRecord {
    id: Uuid,
    adapter_id: Uuid,
    adapter_key: String,
    token_hash: Vec<u8>,
}

/// `None` for malformed, unknown and revoked tokens alike.
pub async fn authenticate(
    pool: &PgPool,
    presented: &str,
) -> Result<Option<WorkerIdentity>, sqlx::Error> {
    let Some(prefix) = parse_prefix_for(SCHEME, presented) else {
        return Ok(None);
    };
    let Some(record) = sqlx::query_as::<_, TokenRecord>(
        "SELECT t.id, t.adapter_id, a.key AS adapter_key, t.token_hash
         FROM worker_tokens t JOIN adapters a ON a.id = t.adapter_id
         WHERE t.prefix = $1 AND t.revoked_at IS NULL",
    )
    .bind(prefix)
    .fetch_optional(pool)
    .await?
    else {
        return Ok(None);
    };
    if !bool::from(hash(presented).ct_eq(record.token_hash.as_slice())) {
        return Ok(None);
    }
    sqlx::query(
        "UPDATE worker_tokens SET last_used_at = now()
         WHERE id = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
    )
    .bind(record.id)
    .execute(pool)
    .await?;
    Ok(Some(WorkerIdentity {
        token_id: record.id,
        adapter_id: record.adapter_id,
        adapter_key: record.adapter_key,
    }))
}

pub async fn revoke_by_prefix(pool: &PgPool, prefix: &str) -> Result<bool, sqlx::Error> {
    let result = sqlx::query(
        "UPDATE worker_tokens SET revoked_at = now() WHERE prefix = $1 AND revoked_at IS NULL",
    )
    .bind(prefix)
    .execute(pool)
    .await?;
    Ok(result.rows_affected() > 0)
}
