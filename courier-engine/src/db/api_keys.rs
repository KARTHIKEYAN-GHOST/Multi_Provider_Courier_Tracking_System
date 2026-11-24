use sqlx::PgPool;
use uuid::Uuid;

pub async fn create_user(pool: &PgPool, email: &str) -> Result<Uuid, sqlx::Error> {
    let id = Uuid::now_v7();
    sqlx::query("INSERT INTO users (id, email) VALUES ($1, $2)")
        .bind(id)
        .bind(email)
        .execute(pool)
        .await?;
    Ok(id)
}

pub async fn find_user_id_by_email(
    pool: &PgPool,
    email: &str,
) -> Result<Option<Uuid>, sqlx::Error> {
    sqlx::query_scalar::<_, Uuid>("SELECT id FROM users WHERE lower(email) = lower($1)")
        .bind(email)
        .fetch_optional(pool)
        .await
}

pub async fn insert_key(
    pool: &PgPool,
    user_id: Uuid,
    prefix: &str,
    key_hash: &[u8],
    name: Option<&str>,
) -> Result<Uuid, sqlx::Error> {
    let id = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO api_keys (id, user_id, prefix, key_hash, name) VALUES ($1, $2, $3, $4, $5)",
    )
    .bind(id)
    .bind(user_id)
    .bind(prefix)
    .bind(key_hash)
    .bind(name)
    .execute(pool)
    .await?;
    Ok(id)
}

#[derive(Debug, sqlx::FromRow)]
pub struct KeyRecord {
    pub id: Uuid,
    pub user_id: Uuid,
    pub key_hash: Vec<u8>,
}

/// A key is usable only if it is not revoked and its user is active.
pub async fn find_usable_by_prefix(
    pool: &PgPool,
    prefix: &str,
) -> Result<Option<KeyRecord>, sqlx::Error> {
    sqlx::query_as::<_, KeyRecord>(
        "SELECT k.id, k.user_id, k.key_hash
         FROM api_keys k
         JOIN users u ON u.id = k.user_id
         WHERE k.prefix = $1 AND k.revoked_at IS NULL AND u.status = 'active'",
    )
    .bind(prefix)
    .fetch_optional(pool)
    .await
}

/// Records use at most once a minute, so authentication does not write on every request.
pub async fn touch_last_used(pool: &PgPool, key_id: Uuid) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE api_keys SET last_used_at = now()
         WHERE id = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
    )
    .bind(key_id)
    .execute(pool)
    .await?;
    Ok(())
}

pub async fn revoke_by_prefix(pool: &PgPool, prefix: &str) -> Result<bool, sqlx::Error> {
    let result = sqlx::query(
        "UPDATE api_keys SET revoked_at = now() WHERE prefix = $1 AND revoked_at IS NULL",
    )
    .bind(prefix)
    .execute(pool)
    .await?;
    Ok(result.rows_affected() > 0)
}
