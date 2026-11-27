use sqlx::PgPool;
use uuid::Uuid;

/// What a user may do. Stored as the Postgres enum `user_role`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, sqlx::Type)]
#[sqlx(type_name = "user_role", rename_all = "snake_case")]
pub enum Role {
    /// Belongs to one organization; tracks shipments and sees only its own requests.
    OrgUser,
    /// Runs the platform; answers every organization's challenges. Has no organization.
    Admin,
}

/// Where a new user belongs. An admin belongs to no organization, everyone else to one.
#[derive(Debug, Clone, Copy)]
pub enum Membership {
    Admin,
    Organization(Uuid),
}

impl Membership {
    fn parts(self) -> (Role, Option<Uuid>) {
        match self {
            Membership::Admin => (Role::Admin, None),
            Membership::Organization(id) => (Role::OrgUser, Some(id)),
        }
    }
}

pub async fn create_user(
    pool: &PgPool,
    email: &str,
    membership: Membership,
) -> Result<Uuid, sqlx::Error> {
    let id = Uuid::now_v7();
    let (role, organization_id) = membership.parts();
    sqlx::query("INSERT INTO users (id, email, role, organization_id) VALUES ($1, $2, $3, $4)")
        .bind(id)
        .bind(email)
        .bind(role)
        .bind(organization_id)
        .execute(pool)
        .await?;
    Ok(id)
}

/// Changes a user's role. Returns false when there is no user with this email.
pub async fn set_membership(
    pool: &PgPool,
    email: &str,
    membership: Membership,
) -> Result<bool, sqlx::Error> {
    let (role, organization_id) = membership.parts();
    let changed = sqlx::query(
        "UPDATE users SET role = $2, organization_id = $3, updated_at = now()
         WHERE lower(email) = lower($1)",
    )
    .bind(email)
    .bind(role)
    .bind(organization_id)
    .execute(pool)
    .await?
    .rows_affected();
    Ok(changed > 0)
}

pub async fn create_organization(pool: &PgPool, name: &str) -> Result<Uuid, sqlx::Error> {
    let id = Uuid::now_v7();
    sqlx::query("INSERT INTO organizations (id, name) VALUES ($1, $2)")
        .bind(id)
        .bind(name)
        .execute(pool)
        .await?;
    Ok(id)
}

pub async fn find_organization_id(pool: &PgPool, name: &str) -> Result<Option<Uuid>, sqlx::Error> {
    sqlx::query_scalar::<_, Uuid>("SELECT id FROM organizations WHERE lower(name) = lower($1)")
        .bind(name)
        .fetch_optional(pool)
        .await
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
    pub role: Role,
    pub organization_id: Option<Uuid>,
}

/// A key is usable only if it is not revoked, its user is active and, for an organization
/// user, the organization is active too.
pub async fn find_usable_by_prefix(
    pool: &PgPool,
    prefix: &str,
) -> Result<Option<KeyRecord>, sqlx::Error> {
    sqlx::query_as::<_, KeyRecord>(
        "SELECT k.id, k.user_id, k.key_hash, u.role, u.organization_id
         FROM api_keys k
         JOIN users u ON u.id = k.user_id
         LEFT JOIN organizations o ON o.id = u.organization_id
         WHERE k.prefix = $1 AND k.revoked_at IS NULL AND u.status = 'active'
           AND (u.organization_id IS NULL OR o.status = 'active')",
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
