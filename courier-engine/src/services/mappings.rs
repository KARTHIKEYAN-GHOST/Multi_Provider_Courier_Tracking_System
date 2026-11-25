//! Status-mapping management.
//!
//! Adapters report the provider's own wording (`raw_status`); the Engine's mapping table turns
//! it into a normalized status. A wording nobody has mapped yet is stored as UNKNOWN and
//! never guessed. This module lets an operator see those wordings and map them — and, when a
//! mapping is added, changed or removed, brings the events already stored and the affected
//! shipments' statuses in line with it, in the same transaction. Every change is audited.

use chrono::{DateTime, Utc};
use sqlx::{PgConnection, PgPool};
use uuid::Uuid;

use super::credentials::{Audit, audit};
use super::{events::recompute_shipment_status, polling};
use crate::domain::ShipmentStatus;

#[derive(Debug, thiserror::Error)]
pub enum MappingError {
    #[error("no provider with this code")]
    UnknownProvider,
    #[error("the raw status must be 1 to 200 characters")]
    InvalidRawStatus,
    #[error(transparent)]
    Database(#[from] sqlx::Error),
}

#[derive(Debug, sqlx::FromRow)]
pub struct MappingRow {
    pub provider_code: String,
    pub raw_status: String,
    pub normalized_status: ShipmentStatus,
    pub source: String,
    pub updated_at: DateTime<Utc>,
}

pub async fn list_mappings(
    pool: &PgPool,
    provider_code: Option<&str>,
) -> Result<Vec<MappingRow>, sqlx::Error> {
    sqlx::query_as::<_, MappingRow>(
        "SELECT p.code AS provider_code, m.raw_status, m.normalized_status, m.source, m.updated_at
         FROM provider_status_mappings m
         JOIN providers p ON p.id = m.provider_id
         WHERE $1::text IS NULL OR p.code = $1
         ORDER BY p.code, m.raw_status",
    )
    .bind(provider_code)
    .fetch_all(pool)
    .await
}

/// A provider wording that events carry but no mapping covers.
#[derive(Debug, sqlx::FromRow)]
pub struct UnmappedStatus {
    pub provider_code: String,
    pub raw_status: String,
    pub events: i64,
    pub shipments: i64,
    pub last_seen_at: DateTime<Utc>,
    /// One description the provider sent with it. Provider text: untrusted, for display only.
    pub sample_description: Option<String>,
}

/// Wordings without a mapping, most frequent first. A wording deliberately mapped to UNKNOWN
/// is not listed: somebody has looked at it.
pub async fn list_unmapped(
    pool: &PgPool,
    provider_code: Option<&str>,
) -> Result<Vec<UnmappedStatus>, sqlx::Error> {
    sqlx::query_as::<_, UnmappedStatus>(
        "SELECT p.code AS provider_code, e.raw_status,
                count(*) AS events, count(DISTINCT e.shipment_id) AS shipments,
                max(e.created_at) AS last_seen_at, max(e.raw_description) AS sample_description
         FROM tracking_events e
         JOIN shipments s ON s.id = e.shipment_id
         JOIN providers p ON p.id = s.provider_id
         WHERE e.normalized_status = 'UNKNOWN'
           AND ($1::text IS NULL OR p.code = $1)
           AND NOT EXISTS (SELECT 1 FROM provider_status_mappings m
                           WHERE m.provider_id = p.id AND m.raw_status = e.raw_status)
         GROUP BY p.code, e.raw_status
         ORDER BY count(*) DESC, p.code, e.raw_status",
    )
    .bind(provider_code)
    .fetch_all(pool)
    .await
}

/// What a mapping change did to the data already stored.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Applied {
    pub events_updated: u64,
    pub shipments_changed: u64,
}

/// Maps a provider wording to a normalized status (creating or replacing the mapping) and
/// applies it to every stored event with that wording.
pub async fn set_mapping(
    pool: &PgPool,
    provider_code: &str,
    raw_status: &str,
    normalized: ShipmentStatus,
    source: &str,
) -> Result<Applied, MappingError> {
    if raw_status.is_empty() || raw_status.chars().count() > 200 {
        return Err(MappingError::InvalidRawStatus);
    }
    let mut tx = pool.begin().await?;
    let provider_id = provider_id(&mut tx, provider_code).await?;
    let previous: Option<ShipmentStatus> = sqlx::query_scalar(
        "SELECT normalized_status FROM provider_status_mappings
         WHERE provider_id = $1 AND raw_status = $2 FOR UPDATE",
    )
    .bind(provider_id)
    .bind(raw_status)
    .fetch_optional(&mut *tx)
    .await?;
    sqlx::query(
        "INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status, source)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (provider_id, raw_status) DO UPDATE SET
             normalized_status = EXCLUDED.normalized_status, source = EXCLUDED.source,
             updated_at = now()",
    )
    .bind(Uuid::now_v7())
    .bind(provider_id)
    .bind(raw_status)
    .bind(normalized)
    .bind(source)
    .execute(&mut *tx)
    .await?;

    let applied = apply(&mut tx, provider_id, raw_status, normalized).await?;
    audit(
        &mut tx,
        Audit {
            actor_type: "admin",
            action: "status_mapping.set",
            provider_id: Some(provider_id),
            details: Some(serde_json::json!({
                "raw_status": raw_status, "normalized_status": normalized, "previous": previous,
                "source": source, "events_updated": applied.events_updated,
                "shipments_changed": applied.shipments_changed,
            })),
            ..Audit::default()
        },
    )
    .await?;
    tx.commit().await?;
    Ok(applied)
}

/// Removes a mapping. Stored events with that wording become UNKNOWN again.
/// Returns `None` if there was no such mapping.
pub async fn remove_mapping(
    pool: &PgPool,
    provider_code: &str,
    raw_status: &str,
) -> Result<Option<Applied>, MappingError> {
    let mut tx = pool.begin().await?;
    let provider_id = provider_id(&mut tx, provider_code).await?;
    let previous: Option<ShipmentStatus> = sqlx::query_scalar(
        "DELETE FROM provider_status_mappings WHERE provider_id = $1 AND raw_status = $2
         RETURNING normalized_status",
    )
    .bind(provider_id)
    .bind(raw_status)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(previous) = previous else {
        return Ok(None);
    };
    let applied = apply(&mut tx, provider_id, raw_status, ShipmentStatus::Unknown).await?;
    audit(
        &mut tx,
        Audit {
            actor_type: "admin",
            action: "status_mapping.remove",
            provider_id: Some(provider_id),
            details: Some(serde_json::json!({
                "raw_status": raw_status, "previous": previous,
                "events_updated": applied.events_updated,
                "shipments_changed": applied.shipments_changed,
            })),
            ..Audit::default()
        },
    )
    .await?;
    tx.commit().await?;
    Ok(Some(applied))
}

async fn provider_id(conn: &mut PgConnection, code: &str) -> Result<Uuid, MappingError> {
    sqlx::query_scalar("SELECT id FROM providers WHERE code = $1")
        .bind(code)
        .fetch_optional(conn)
        .await?
        .ok_or(MappingError::UnknownProvider)
}

/// Re-normalizes the provider's stored events that carry `raw_status` and recomputes the
/// status of every shipment that has one. Archived shipments are closed and left untouched.
async fn apply(
    conn: &mut PgConnection,
    provider_id: Uuid,
    raw_status: &str,
    normalized: ShipmentStatus,
) -> Result<Applied, sqlx::Error> {
    // Shipment rows are locked first, in ID order — the same shipment-before-anything-else
    // order every other writer uses, so this cannot deadlock with a result being stored.
    let shipments: Vec<Uuid> = sqlx::query_scalar(
        "SELECT s.id FROM shipments s
         WHERE s.provider_id = $1 AND s.archived_at IS NULL
           AND EXISTS (SELECT 1 FROM tracking_events e
                       WHERE e.shipment_id = s.id AND e.raw_status = $2
                         AND e.normalized_status <> $3)
         ORDER BY s.id
         FOR UPDATE OF s",
    )
    .bind(provider_id)
    .bind(raw_status)
    .bind(normalized)
    .fetch_all(&mut *conn)
    .await?;

    let mut applied = Applied::default();
    for shipment_id in shipments {
        applied.events_updated += sqlx::query(
            "UPDATE tracking_events SET normalized_status = $3
             WHERE shipment_id = $1 AND raw_status = $2 AND normalized_status <> $3",
        )
        .bind(shipment_id)
        .bind(raw_status)
        .bind(normalized)
        .execute(&mut *conn)
        .await?
        .rows_affected();
        if recompute_shipment_status(conn, shipment_id).await? {
            applied.shipments_changed += 1;
        }
        // A shipment that is no longer finished (a wrong mapping was corrected) is polled again.
        polling::schedule_next_poll_if_unset(conn, shipment_id).await?;
    }
    Ok(applied)
}
