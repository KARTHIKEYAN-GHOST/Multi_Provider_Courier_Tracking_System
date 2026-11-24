//! Transactional outbox (write side).
//!
//! A message is inserted in the same database transaction as the state change that causes
//! it. Either both are committed or neither is, so a job can never exist without its message
//! and a message can never be sent for a job that was rolled back. The relay that publishes
//! pending rows to RabbitMQ (at least once) is added in Phase 3.

use serde::Serialize;
use sqlx::PgConnection;

use super::contracts::Envelope;

pub async fn enqueue<P: Serialize>(
    conn: &mut PgConnection,
    exchange: &str,
    routing_key: &str,
    envelope: &Envelope<P>,
) -> Result<(), sqlx::Error> {
    let payload = serde_json::to_value(envelope).map_err(|e| sqlx::Error::Encode(Box::new(e)))?;
    sqlx::query(
        "INSERT INTO outbox_messages (message_id, message_type, exchange, routing_key, payload, available_at)
         VALUES ($1, $2, $3, $4, $5, $6)",
    )
    .bind(envelope.message_id)
    .bind(&envelope.message_type)
    .bind(exchange)
    .bind(routing_key)
    .bind(payload)
    .bind(envelope.occurred_at)
    .execute(conn)
    .await?;
    Ok(())
}
