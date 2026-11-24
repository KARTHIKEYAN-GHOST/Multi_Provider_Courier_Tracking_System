//! User API keys.
//!
//! A key looks like `ctk_<12 hex prefix>_<64 hex secret>`. The prefix is a public lookup
//! handle; the secret is 256 bits of randomness. Only the SHA-256 hash of the whole key is
//! stored, so a database leak does not reveal usable keys. (A fast hash is appropriate here
//! because the secret is high-entropy random data, not a human-chosen password.)

use rand::{TryRng, rngs::SysRng};
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use subtle::ConstantTimeEq;
use uuid::Uuid;

use crate::db;

const SCHEME: &str = "ctk";
const PREFIX_HEX_LEN: usize = 12;
const SECRET_HEX_LEN: usize = 64;
const MAX_ISSUE_ATTEMPTS: u32 = 5;

/// The authenticated caller.
#[derive(Debug, Clone, Copy)]
pub struct Caller {
    pub user_id: Uuid,
    pub api_key_id: Uuid,
}

#[derive(Clone)]
pub struct GeneratedKey {
    /// The full key. Shown to the user once and never stored.
    pub full: String,
    pub prefix: String,
    pub hash: [u8; 32],
}

/// Random bytes straight from the operating system's cryptographic generator (`getrandom`),
/// not from a userspace generator. This is the recommended source for long-lived secrets and
/// stays correct across process forks and VM snapshots.
fn os_random<const N: usize>() -> [u8; N] {
    let mut bytes = [0u8; N];
    SysRng
        .try_fill_bytes(&mut bytes)
        .expect("the operating system random generator is unavailable");
    bytes
}

pub fn generate() -> GeneratedKey {
    let prefix = hex::encode(os_random::<{ PREFIX_HEX_LEN / 2 }>());
    let secret = hex::encode(os_random::<{ SECRET_HEX_LEN / 2 }>());
    let full = format!("{SCHEME}_{prefix}_{secret}");
    let hash = hash(&full);
    GeneratedKey { full, prefix, hash }
}

pub fn hash(full_key: &str) -> [u8; 32] {
    Sha256::digest(full_key.as_bytes()).into()
}

/// Returns the prefix if the text has the shape of a key. Checked before touching the database.
pub fn parse_prefix(presented: &str) -> Option<&str> {
    let mut parts = presented.split('_');
    let (scheme, prefix, secret) = (parts.next()?, parts.next()?, parts.next()?);
    let is_hex = |s: &str| {
        s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    };
    let well_formed = parts.next().is_none()
        && scheme == SCHEME
        && prefix.len() == PREFIX_HEX_LEN
        && secret.len() == SECRET_HEX_LEN
        && is_hex(prefix)
        && is_hex(secret);
    well_formed.then_some(prefix)
}

/// Creates a key for a user and returns the full key (the only time it is available).
pub async fn issue(
    pool: &PgPool,
    user_id: Uuid,
    name: Option<&str>,
) -> Result<String, sqlx::Error> {
    issue_with(pool, user_id, name, generate).await
}

/// As [`issue`], with the key generator supplied by the caller (tests use this to force a
/// prefix collision).
///
/// The 48-bit prefix is unique in the database. If a freshly generated key happens to reuse an
/// existing prefix, a new key is generated and the insert retried. Nothing about the existing
/// key is revealed or changed, and any other database error is returned immediately.
pub async fn issue_with(
    pool: &PgPool,
    user_id: Uuid,
    name: Option<&str>,
    mut make_key: impl FnMut() -> GeneratedKey,
) -> Result<String, sqlx::Error> {
    let mut attempt = 1;
    loop {
        let key = make_key();
        match db::api_keys::insert_key(pool, user_id, &key.prefix, &key.hash, name).await {
            Ok(_) => return Ok(key.full),
            Err(e) if is_prefix_collision(&e) && attempt < MAX_ISSUE_ATTEMPTS => {
                tracing::warn!(attempt, "API key prefix collision; generating a new key");
                attempt += 1;
            }
            Err(e) => return Err(e),
        }
    }
}

fn is_prefix_collision(error: &sqlx::Error) -> bool {
    db::is_unique_violation(error)
        && matches!(error, sqlx::Error::Database(e) if e.constraint() == Some("api_keys_prefix_key"))
}

/// Looks the key up by prefix and compares hashes in constant time.
/// Returns `None` for malformed, unknown and revoked keys and for disabled users alike.
pub async fn authenticate(pool: &PgPool, presented: &str) -> Result<Option<Caller>, sqlx::Error> {
    let Some(prefix) = parse_prefix(presented) else {
        return Ok(None);
    };
    let Some(record) = db::api_keys::find_usable_by_prefix(pool, prefix).await? else {
        return Ok(None);
    };
    let presented_hash = hash(presented);
    if !bool::from(presented_hash.ct_eq(record.key_hash.as_slice())) {
        return Ok(None);
    }
    db::api_keys::touch_last_used(pool, record.id).await?;
    Ok(Some(Caller {
        user_id: record.user_id,
        api_key_id: record.id,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_keys_have_the_documented_shape() {
        let key = generate();
        assert_eq!(parse_prefix(&key.full), Some(key.prefix.as_str()));
        assert_eq!(key.full.len(), 3 + 1 + 12 + 1 + 64);
        assert_eq!(key.hash, hash(&key.full));
    }

    #[test]
    fn generated_keys_are_unique() {
        assert_ne!(generate().full, generate().full);
    }

    #[test]
    fn malformed_keys_are_rejected_without_a_database_lookup() {
        let good = generate().full;
        let cases = [
            String::new(),
            "ctk".to_string(),
            good.replace("ctk_", "xyz_"),
            good.to_uppercase(),
            format!("{good}_extra"),
            good[..good.len() - 1].to_string(),
            "ctk_zzzzzzzzzzzz_".to_string() + &"a".repeat(64),
        ];
        for case in cases {
            assert_eq!(parse_prefix(&case), None, "{case:?}");
        }
    }
}
