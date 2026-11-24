//! Outbox relay: publishes pending `outbox_messages` rows to the broker, at least once.
//!
//! A row is marked `sent` only after the broker has confirmed the message. If the relay
//! stops between the confirm and the mark, the row is still pending and is published again
//! with the same `message_id`; consumers deduplicate on that ID. A message is therefore
//! never lost, and may occasionally be delivered twice.

use std::future::Future;

use serde_json::Value;
use sqlx::PgPool;
use uuid::Uuid;

/// One message to publish.
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct OutboxMessage {
    pub message_id: Uuid,
    pub message_type: String,
    pub exchange: String,
    pub routing_key: String,
    /// The complete envelope.
    pub payload: Value,
    pub attempts: i32,
}

/// Something that can deliver a message to the broker and wait for its confirmation.
/// The real implementation is `broker::BrokerPublisher`; tests substitute their own.
pub trait Publish {
    /// `Ok` only when the broker has taken responsibility for the message.
    fn publish(&self, message: &OutboxMessage) -> impl Future<Output = Result<(), String>> + Send;
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct RelayStats {
    pub sent: usize,
    pub failed: usize,
}

/// Publishes up to `batch` pending messages.
///
/// Rows are claimed with `FOR UPDATE SKIP LOCKED`, so several relays can run without
/// publishing the same row at the same moment. A failed publish leaves the row pending with
/// a later `available_at` (exponential back-off, capped at one minute) and ends the batch,
/// because the broker is most likely unreachable.
pub async fn relay_once<P: Publish>(
    pool: &PgPool,
    publisher: &P,
    batch: i64,
) -> Result<RelayStats, sqlx::Error> {
    let mut tx = pool.begin().await?;
    let claimed = sqlx::query_as::<_, OutboxMessage>(
        "SELECT message_id, message_type, exchange, routing_key, payload, attempts
         FROM outbox_messages
         WHERE status = 'pending' AND available_at <= clock_timestamp()
         ORDER BY available_at, message_id
         LIMIT $1
         FOR UPDATE SKIP LOCKED",
    )
    .bind(batch)
    .fetch_all(&mut *tx)
    .await?;

    let mut stats = RelayStats::default();
    for message in claimed {
        match publisher.publish(&message).await {
            Ok(()) => {
                sqlx::query(
                    "UPDATE outbox_messages SET status = 'sent', sent_at = clock_timestamp(),
                         attempts = attempts + 1, last_error = NULL
                     WHERE message_id = $1",
                )
                .bind(message.message_id)
                .execute(&mut *tx)
                .await?;
                stats.sent += 1;
            }
            Err(error) => {
                tracing::warn!(message_id = %message.message_id, attempts = message.attempts + 1, %error, "publish failed; will retry");
                sqlx::query(
                    "UPDATE outbox_messages SET attempts = attempts + 1, last_error = $2,
                         available_at = clock_timestamp()
                             + LEAST(60000, 500 * power(2, LEAST(attempts, 10))) * interval '1 millisecond'
                     WHERE message_id = $1",
                )
                .bind(message.message_id)
                .bind(&error)
                .execute(&mut *tx)
                .await?;
                stats.failed += 1;
                break;
            }
        }
    }
    tx.commit().await?;
    Ok(stats)
}
