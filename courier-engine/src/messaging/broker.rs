//! The RabbitMQ side of the Engine: connecting, declaring the topology, publishing outbox
//! messages with confirms, and consuming worker events and dead-letters.
//!
//! All decisions live in `services`; this module only moves bytes and translates each
//! handler's verdict into ack / reject / requeue. A message is acknowledged only after the
//! handler's database transaction has committed, which is what makes delivery at-least-once.

use std::{
    sync::Arc,
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use futures_util::StreamExt;
use lapin::{
    BasicProperties, Channel, Confirmation, Connection, ConnectionProperties, ExchangeKind,
    message::Delivery,
    options::{
        BasicAckOptions, BasicConsumeOptions, BasicNackOptions, BasicPublishOptions,
        BasicQosOptions, BasicRejectOptions, ConfirmSelectOptions, ExchangeDeclareOptions,
        QueueBindOptions, QueueDeclareOptions,
    },
    types::{AMQPValue, FieldTable},
};
use serde_json::{Value, json};
use sqlx::PgPool;
use tokio_util::sync::CancellationToken;

use super::relay::{OutboxMessage, Publish, relay_once};
use super::topology::{Argument, DEAD_LETTER_CONSUMER, EVENT_CONSUMER, Topology};
use crate::services::{
    close_attempt::RetryPolicy,
    dead_letter::handle_dead_letter,
    events::{Handled, handle_event},
};

const PUBLISH_TIMEOUT: Duration = Duration::from_secs(10);

/// Whether the Engine currently has a working broker session. Read by `/readyz`.
#[derive(Clone, Default)]
pub struct BrokerHealth(Arc<AtomicBool>);

impl BrokerHealth {
    pub fn is_connected(&self) -> bool {
        self.0.load(Ordering::Relaxed)
    }
    fn set(&self, connected: bool) {
        self.0.store(connected, Ordering::Relaxed);
    }
}

#[derive(Debug, Clone)]
pub struct BrokerSettings {
    pub amqp_url: String,
    pub topology: Topology,
    pub policy: RetryPolicy,
    pub event_prefetch: u16,
    pub outbox_batch: i64,
    pub outbox_poll: Duration,
}

pub async fn connect(amqp_url: &str) -> Result<Connection, lapin::Error> {
    Connection::connect(amqp_url, ConnectionProperties::default()).await
}

/// Declares every exchange, queue and binding. Declaring is idempotent: running it again
/// with the same definitions changes nothing, so it is safe at every start.
pub async fn declare_topology(channel: &Channel, topology: &Topology) -> Result<(), lapin::Error> {
    for exchange in &topology.exchanges {
        let kind = match exchange.kind.as_str() {
            "topic" => ExchangeKind::Topic,
            "direct" => ExchangeKind::Direct,
            "fanout" => ExchangeKind::Fanout,
            other => ExchangeKind::Custom(other.to_string()),
        };
        channel
            .exchange_declare(
                topology.physical(&exchange.name).into(),
                kind,
                ExchangeDeclareOptions {
                    durable: exchange.durable,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await?;
    }
    for queue in &topology.queues {
        let mut arguments = FieldTable::default();
        for (key, value) in topology.arguments(queue) {
            let value = match value {
                Argument::Text(text) => AMQPValue::LongString(text.into()),
                Argument::Number(number) => AMQPValue::LongLongInt(number),
            };
            arguments.insert(key.into(), value);
        }
        let name = topology.physical(&queue.name);
        channel
            .queue_declare(
                name.clone().into(),
                QueueDeclareOptions {
                    durable: queue.durable,
                    ..Default::default()
                },
                arguments,
            )
            .await?;
        for binding in &queue.bindings {
            channel
                .queue_bind(
                    name.clone().into(),
                    topology.physical(&binding.exchange).into(),
                    binding.routing_key.clone().into(),
                    QueueBindOptions::default(),
                    FieldTable::default(),
                )
                .await?;
        }
    }
    Ok(())
}

// ----------------------------------------------------------------------------- publishing

/// Publishes outbox messages on a channel in confirm mode.
pub struct BrokerPublisher {
    channel: Channel,
    topology: Topology,
}

impl BrokerPublisher {
    /// Puts the channel into confirm mode: the broker acknowledges every message it has
    /// safely stored.
    pub async fn new(channel: Channel, topology: Topology) -> Result<Self, lapin::Error> {
        channel
            .confirm_select(ConfirmSelectOptions::default())
            .await?;
        Ok(BrokerPublisher { channel, topology })
    }
}

impl Publish for BrokerPublisher {
    async fn publish(&self, message: &OutboxMessage) -> Result<(), String> {
        let body = serde_json::to_vec(&message.payload).map_err(|e| e.to_string())?;
        let mut properties = BasicProperties::default()
            .with_content_type("application/json".into())
            .with_delivery_mode(2) // persistent: survives a broker restart
            .with_message_id(message.message_id.to_string().into())
            .with_type(message.message_type.clone().into());
        if let Some(correlation_id) = message
            .payload
            .get("correlation_id")
            .and_then(Value::as_str)
        {
            properties = properties.with_correlation_id(correlation_id.into());
        }

        let publish = async {
            let confirm = self
                .channel
                .basic_publish(
                    self.topology.physical(&message.exchange).into(),
                    message.routing_key.clone().into(),
                    // mandatory: a message no queue is bound for is returned, not silently dropped.
                    BasicPublishOptions {
                        mandatory: true,
                        ..Default::default()
                    },
                    &body,
                    properties,
                )
                .await
                .map_err(|e| e.to_string())?
                .await
                .map_err(|e| e.to_string())?;
            match confirm {
                Confirmation::Ack(None) => Ok(()),
                Confirmation::Ack(Some(returned)) | Confirmation::Nack(Some(returned)) => {
                    Err(format!("unroutable: {}", returned.reply_text))
                }
                Confirmation::Nack(None) => Err("the broker refused the message".to_string()),
                Confirmation::NotRequested => Err("publisher confirms are not enabled".to_string()),
            }
        };
        tokio::time::timeout(PUBLISH_TIMEOUT, publish)
            .await
            .unwrap_or_else(|_| Err("timed out waiting for the broker's confirmation".to_string()))
    }
}

// ----------------------------------------------------------------------------- consuming

/// Converts AMQP headers (such as `x-death`) to JSON so they can be stored and inspected.
pub fn headers_to_json(headers: Option<&FieldTable>) -> Value {
    fn convert(value: &AMQPValue) -> Value {
        match value {
            AMQPValue::Boolean(v) => json!(v),
            AMQPValue::ShortShortInt(v) => json!(v),
            AMQPValue::ShortShortUInt(v) => json!(v),
            AMQPValue::ShortInt(v) => json!(v),
            AMQPValue::ShortUInt(v) => json!(v),
            AMQPValue::LongInt(v) => json!(v),
            AMQPValue::LongUInt(v) => json!(v),
            AMQPValue::LongLongInt(v) => json!(v),
            AMQPValue::Float(v) => json!(v),
            AMQPValue::Double(v) => json!(v),
            AMQPValue::Timestamp(v) => json!(v),
            AMQPValue::ShortString(v) => json!(v.as_str()),
            AMQPValue::LongString(v) => json!(String::from_utf8_lossy(v.as_bytes())),
            AMQPValue::FieldArray(v) => Value::Array(v.as_slice().iter().map(convert).collect()),
            AMQPValue::FieldTable(v) => table(v),
            AMQPValue::Void => Value::Null,
            other => json!(format!("{other:?}")),
        }
    }
    fn table(fields: &FieldTable) -> Value {
        Value::Object(
            fields
                .inner()
                .iter()
                .map(|(k, v)| (k.as_str().to_string(), convert(v)))
                .collect(),
        )
    }
    headers.map(table).unwrap_or(Value::Null)
}

/// Why and from where RabbitMQ dead-lettered a message, from its `x-death` header.
fn death_details(headers: &Value) -> (Option<String>, Option<String>) {
    let first = headers.get("x-death").and_then(|deaths| deaths.get(0));
    let field = |name: &str| {
        first
            .and_then(|d| d.get(name))
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    (
        field("reason").or_else(|| {
            headers
                .get("x-first-death-reason")
                .and_then(Value::as_str)
                .map(str::to_string)
        }),
        field("queue").or_else(|| {
            headers
                .get("x-first-death-queue")
                .and_then(Value::as_str)
                .map(str::to_string)
        }),
    )
}

async fn consume(
    channel: &Channel,
    queue: &str,
    tag: &str,
) -> Result<lapin::Consumer, lapin::Error> {
    channel
        .basic_consume(
            queue.into(),
            tag.into(),
            BasicConsumeOptions::default(),
            FieldTable::default(),
        )
        .await
}

/// Requeues a delivery after a transient failure. The queue's delivery limit bounds how
/// often this can happen before the message is dead-lettered.
async fn requeue(delivery: &Delivery) -> Result<(), lapin::Error> {
    tokio::time::sleep(Duration::from_millis(200)).await;
    delivery
        .nack(BasicNackOptions {
            requeue: true,
            ..Default::default()
        })
        .await?;
    Ok(())
}

/// Consumes `engine.events` until cancelled or the connection fails.
pub async fn run_event_consumer(
    channel: Channel,
    queue: String,
    pool: PgPool,
    policy: RetryPolicy,
    shutdown: CancellationToken,
) -> Result<(), lapin::Error> {
    let mut consumer = consume(&channel, &queue, "engine-events").await?;
    loop {
        let delivery = tokio::select! {
            _ = shutdown.cancelled() => return Ok(()),
            next = consumer.next() => match next {
                Some(delivery) => delivery?,
                None => return Err(lapin::Error::from(std::io::Error::other("event consumer was cancelled by the broker"))),
            },
        };
        match handle_event(&pool, &delivery.data, policy).await {
            // Acknowledge only now: the handler's transaction has committed.
            Ok(Handled::Processed | Handled::Duplicate) => {
                delivery.ack(BasicAckOptions::default()).await?;
            }
            Ok(Handled::Rejected(reason)) => {
                let message_id = delivery
                    .properties
                    .message_id()
                    .as_ref()
                    .map(|id| id.as_str().to_string());
                tracing::warn!(?message_id, %reason, queue = %queue, "event rejected; parked in the dead-letter queue");
                delivery
                    .reject(BasicRejectOptions { requeue: false })
                    .await?;
            }
            Err(error) => {
                tracing::error!(%error, "event handling failed; requeueing");
                requeue(&delivery).await?;
            }
        }
    }
}

/// Consumes one job dead-letter queue until cancelled or the connection fails.
pub async fn run_dead_letter_consumer(
    channel: Channel,
    queue: String,
    logical_source: String,
    pool: PgPool,
    policy: RetryPolicy,
    shutdown: CancellationToken,
) -> Result<(), lapin::Error> {
    let mut consumer = consume(&channel, &queue, &format!("engine-dlq-{logical_source}")).await?;
    loop {
        let delivery = tokio::select! {
            _ = shutdown.cancelled() => return Ok(()),
            next = consumer.next() => match next {
                Some(delivery) => delivery?,
                None => return Err(lapin::Error::from(std::io::Error::other("dead-letter consumer was cancelled by the broker"))),
            },
        };
        let headers = headers_to_json(delivery.properties.headers().as_ref());
        let (reason, _) = death_details(&headers);
        match handle_dead_letter(
            &pool,
            &delivery.data,
            &logical_source,
            reason.as_deref(),
            &headers,
            policy,
        )
        .await
        {
            Ok(outcome) => {
                tracing::warn!(?outcome, ?reason, source_queue = %logical_source, "dead-lettered job message handled");
                // The database now holds the record; the DLQ can be emptied.
                delivery.ack(BasicAckOptions::default()).await?;
            }
            Err(error) => {
                tracing::error!(%error, "dead-letter handling failed; requeueing");
                requeue(&delivery).await?;
            }
        }
    }
}

/// Publishes pending outbox rows until cancelled or the connection fails.
async fn run_relay(
    connection: &Connection,
    publisher: BrokerPublisher,
    pool: PgPool,
    settings: &BrokerSettings,
    shutdown: CancellationToken,
) -> Result<(), lapin::Error> {
    loop {
        if !connection.status().connected() {
            return Err(lapin::Error::from(std::io::Error::other("connection lost")));
        }
        let idle = match relay_once(&pool, &publisher, settings.outbox_batch).await {
            Ok(stats) => stats.sent == 0,
            Err(error) => {
                tracing::error!(%error, "outbox relay could not reach the database");
                true
            }
        };
        if idle {
            tokio::select! {
                _ = shutdown.cancelled() => return Ok(()),
                _ = tokio::time::sleep(settings.outbox_poll) => {}
            }
        } else if shutdown.is_cancelled() {
            return Ok(());
        }
    }
}

/// One broker session: connect, declare, then run the relay and the consumers until one of
/// them fails or shutdown is requested.
async fn session(
    settings: &BrokerSettings,
    pool: &PgPool,
    health: &BrokerHealth,
    shutdown: &CancellationToken,
) -> Result<(), lapin::Error> {
    let connection = connect(&settings.amqp_url).await?;
    let topology = &settings.topology;
    declare_topology(&connection.create_channel().await?, topology).await?;

    let publisher =
        BrokerPublisher::new(connection.create_channel().await?, topology.clone()).await?;

    let mut consumers = tokio::task::JoinSet::new();
    for queue in topology.queues_for(EVENT_CONSUMER) {
        let channel = connection.create_channel().await?;
        channel
            .basic_qos(settings.event_prefetch, BasicQosOptions::default())
            .await?;
        consumers.spawn(run_event_consumer(
            channel,
            topology.physical(&queue.name),
            pool.clone(),
            settings.policy,
            shutdown.clone(),
        ));
    }
    for queue in topology.queues_for(DEAD_LETTER_CONSUMER) {
        let channel = connection.create_channel().await?;
        channel
            .basic_qos(settings.event_prefetch, BasicQosOptions::default())
            .await?;
        // The queue the message died in is the DLQ's name without the ".dlq" suffix.
        let source = queue
            .name
            .strip_suffix(".dlq")
            .unwrap_or(&queue.name)
            .to_string();
        consumers.spawn(run_dead_letter_consumer(
            channel,
            topology.physical(&queue.name),
            source,
            pool.clone(),
            settings.policy,
            shutdown.clone(),
        ));
    }

    health.set(true);
    tracing::info!("broker session established; topology declared");

    let result = tokio::select! {
        relay = run_relay(&connection, publisher, pool.clone(), settings, shutdown.clone()) => relay,
        Some(consumer) = consumers.join_next() => consumer.unwrap_or_else(|join| {
            Err(lapin::Error::from(std::io::Error::other(format!("consumer task failed: {join}"))))
        }),
    };
    health.set(false);
    // Let in-flight handlers finish their current message before the connection closes.
    if shutdown.is_cancelled() {
        while consumers.join_next().await.is_some() {}
    } else {
        consumers.shutdown().await;
    }
    let _ = connection.close(200, "bye".into()).await;
    result
}

/// Keeps a broker session running until shutdown, reconnecting with a capped back-off.
/// While the broker is unreachable the API keeps working: jobs and their messages
/// accumulate in the outbox and are published when the connection returns.
pub async fn run(
    settings: BrokerSettings,
    pool: PgPool,
    health: BrokerHealth,
    shutdown: CancellationToken,
) {
    let mut delay = Duration::from_millis(500);
    while !shutdown.is_cancelled() {
        let started = std::time::Instant::now();
        match session(&settings, &pool, &health, &shutdown).await {
            Ok(()) => {}
            Err(error) => {
                tracing::error!(%error, retry_in_ms = delay.as_millis() as u64, "broker session ended; reconnecting")
            }
        }
        health.set(false);
        if shutdown.is_cancelled() {
            break;
        }
        // A session that lasted a while was healthy: start the back-off over.
        if started.elapsed() > Duration::from_secs(30) {
            delay = Duration::from_millis(500);
        }
        tokio::select! {
            _ = shutdown.cancelled() => break,
            _ = tokio::time::sleep(delay) => {}
        }
        delay = (delay * 2).min(Duration::from_secs(15));
    }
}
