//! RabbitMQ integration: the Engine against a real broker (acceptance criteria 15, 21, 31).
//!
//! Needs the Compose RabbitMQ (`make up-deps`) and `ENGINE_AMQP_URL`. Every test declares the
//! whole topology under its own unique name prefix and deletes it afterwards, so tests never
//! touch the real queues or each other. The test plays the worker's part by hand; the real
//! Python worker arrives in Phase 4.

mod common;

use std::time::Duration;

use axum::http::StatusCode;
use common::{app, get, track, user};
use courier_engine::{
    api,
    config::Config,
    db,
    messaging::{
        broker::{self, BrokerHealth, BrokerPublisher, BrokerSettings},
        relay::relay_once,
        topology::Topology,
    },
    services::close_attempt::RetryPolicy,
    state::AppState,
};
use lapin::{
    BasicProperties, Channel, Connection,
    options::{
        BasicAckOptions, BasicGetOptions, BasicPublishOptions, ExchangeDeleteOptions,
        QueueDeclareOptions, QueueDeleteOptions,
    },
    types::FieldTable,
};
use serde_json::{Value, json};
use sqlx::PgPool;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;

fn amqp_url() -> String {
    std::env::var("ENGINE_AMQP_URL")
        .expect("ENGINE_AMQP_URL must be set (see .env); run `make up-deps`")
}

/// A private copy of the topology on the real broker.
struct Broker {
    connection: Connection,
    channel: Channel,
    topology: Topology,
}

impl Broker {
    async fn start() -> Self {
        let prefix = format!("test-{}.", &Uuid::now_v7().simple().to_string()[20..]);
        let topology = Topology::load().unwrap().with_prefix(&prefix);
        let connection = broker::connect(&amqp_url())
            .await
            .expect("RabbitMQ is reachable");
        let channel = connection.create_channel().await.unwrap();
        broker::declare_topology(&channel, &topology).await.unwrap();
        Broker {
            connection,
            channel,
            topology,
        }
    }

    fn settings(&self) -> BrokerSettings {
        BrokerSettings {
            amqp_url: amqp_url(),
            topology: self.topology.clone(),
            policy: RetryPolicy::default(),
            event_prefetch: 10,
            outbox_batch: 50,
            outbox_poll: Duration::from_millis(50),
        }
    }

    fn name(&self, logical: &str) -> String {
        self.topology.physical(logical)
    }

    /// Takes one message from a queue, waiting up to ten seconds for it.
    async fn take(&self, queue: &str) -> Option<lapin::message::Delivery> {
        self.take_within(queue, Duration::from_secs(10)).await
    }

    async fn take_within(&self, queue: &str, wait: Duration) -> Option<lapin::message::Delivery> {
        for _ in 0..(wait.as_millis() / 100) {
            let got = self
                .channel
                .basic_get(self.name(queue).into(), BasicGetOptions::default())
                .await
                .unwrap();
            if let Some(message) = got {
                return Some(message.delivery);
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        None
    }

    async fn depth(&self, queue: &str) -> u32 {
        self.channel
            .queue_declare(
                self.name(queue).into(),
                QueueDeclareOptions {
                    passive: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await
            .unwrap()
            .message_count()
    }

    /// Publishes as a worker would: to the events exchange.
    async fn publish_event(&self, routing_key: &str, body: &[u8]) {
        self.channel
            .basic_publish(
                self.name("courier.events").into(),
                routing_key.into(),
                BasicPublishOptions::default(),
                body,
                BasicProperties::default().with_delivery_mode(2),
            )
            .await
            .unwrap()
            .await
            .unwrap();
    }

    async fn delete(self) {
        for queue in &self.topology.queues {
            let _ = self
                .channel
                .queue_delete(self.name(&queue.name).into(), QueueDeleteOptions::default())
                .await;
        }
        for exchange in &self.topology.exchanges {
            let _ = self
                .channel
                .exchange_delete(
                    self.name(&exchange.name).into(),
                    ExchangeDeleteOptions::default(),
                )
                .await;
        }
        let _ = self.connection.close(200, "done".into()).await;
    }
}

/// The Engine's broker session running in the background.
struct Session {
    shutdown: CancellationToken,
    health: BrokerHealth,
    task: tokio::task::JoinHandle<()>,
}

impl Session {
    async fn start(settings: BrokerSettings, pool: &PgPool) -> Self {
        let shutdown = CancellationToken::new();
        let health = BrokerHealth::default();
        let task = tokio::spawn(broker::run(
            settings,
            pool.clone(),
            health.clone(),
            shutdown.clone(),
        ));
        let session = Session {
            shutdown,
            health,
            task,
        };
        for _ in 0..100 {
            if session.health.is_connected() {
                return session;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        panic!("the broker session did not come up");
    }

    async fn stop(self) {
        self.shutdown.cancel();
        tokio::time::timeout(Duration::from_secs(10), self.task)
            .await
            .expect("the session stops promptly")
            .unwrap();
    }
}

async fn new_job(pool: &PgPool, tracking_number: &str) -> Uuid {
    let (_, key) = user(pool, &format!("{}@example.com", Uuid::now_v7().simple())).await;
    let reply = track(&app(pool), &key, tracking_number).await;
    Uuid::parse_str(reply.body["latest_job"]["id"].as_str().unwrap()).unwrap()
}

/// Polls until the query returns the expected text, for up to fifteen seconds.
async fn eventually(pool: &PgPool, sql: &'static str, expected: &str) {
    let mut last = String::new();
    for _ in 0..150 {
        last = sqlx::query_scalar::<_, Option<String>>(sql)
            .fetch_one(pool)
            .await
            .unwrap()
            .unwrap_or_default();
        if last == expected {
            return;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!("expected {expected:?} from `{sql}`, last saw {last:?}");
}

fn worker_message(message_type: &str, job: &Value, extra: Value) -> Vec<u8> {
    let mut payload = json!({
        "job_id": job["payload"]["job_id"], "attempt": job["payload"]["attempt"],
        "execution_id": Uuid::now_v7(), "delivery_count": 0, "worker_id": "test-worker",
        "adapter_key": "dummy", "adapter_version": "0.1.0", "started_at": "2026-11-24T10:00:00.100Z",
    });
    for (key, value) in extra.as_object().unwrap() {
        payload[key] = value.clone();
    }
    json!({
        "message_id": Uuid::now_v7(), "type": message_type, "schema_version": "1.0",
        "occurred_at": "2026-11-24T10:00:00Z", "correlation_id": job["correlation_id"],
        "causation_id": job["message_id"], "producer": "test-worker@0.1.0", "payload": payload,
    })
    .to_string()
    .into_bytes()
}

fn success_extra() -> Value {
    json!({
        "finished_at": "2026-11-24T10:00:00.300Z", "outcome": "SUCCESS",
        "raw_response": { "content_type": "application/json", "body": {} },
        "events": [{ "raw_status": "PKG_DELIVERED", "occurred_at": "2026-11-24T09:45:00Z" }],
    })
}

// ----------------------------------------------------------------------------- tests

#[sqlx::test(migrator = "MIGRATOR")]
async fn declaring_the_topology_twice_changes_nothing(_pool: PgPool) {
    let broker = Broker::start().await;
    // Re-declaring with identical arguments succeeds; RabbitMQ would close the channel with
    // PRECONDITION_FAILED if any argument (queue type, delivery limit, DLX) differed.
    broker::declare_topology(&broker.channel, &broker.topology)
        .await
        .unwrap();
    for queue in [
        "adapter.dummy.jobs",
        "engine.events",
        "adapter.dummy.jobs.dlq",
        "engine.events.dlq",
    ] {
        assert_eq!(broker.depth(queue).await, 0, "{queue} exists and is empty");
    }
    broker.delete().await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_relay_delivers_the_job_message_to_the_adapter_queue_with_its_properties(pool: PgPool) {
    let broker = Broker::start().await;
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let publisher = BrokerPublisher::new(
        broker.connection.create_channel().await.unwrap(),
        broker.topology.clone(),
    )
    .await
    .unwrap();

    let stats = relay_once(&pool, &publisher, 50).await.unwrap();
    assert_eq!((stats.sent, stats.failed), (1, 0));

    let delivery = broker
        .take("adapter.dummy.jobs")
        .await
        .expect("the message is in the worker's queue");
    let body: Value = serde_json::from_slice(&delivery.data).unwrap();
    let stored: Value = sqlx::query_scalar("SELECT payload FROM outbox_messages")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(body, stored, "the wire message is the outbox envelope");
    assert_eq!(body["payload"]["job_id"], json!(job_id));

    let p = &delivery.properties;
    assert_eq!(
        p.message_id().as_ref().unwrap().as_str(),
        body["message_id"].as_str().unwrap()
    );
    assert_eq!(
        p.correlation_id().as_ref().unwrap().as_str(),
        body["correlation_id"].as_str().unwrap()
    );
    assert_eq!(p.kind().as_ref().unwrap().as_str(), "job.track");
    assert_eq!(
        p.content_type().as_ref().unwrap().as_str(),
        "application/json"
    );
    assert_eq!(*p.delivery_mode(), Some(2), "persistent");
    delivery.ack(BasicAckOptions::default()).await.unwrap();

    eventually(&pool, "SELECT status::text FROM outbox_messages", "sent").await;
    broker.delete().await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_unroutable_message_is_not_marked_sent(pool: PgPool) {
    let broker = Broker::start().await;
    new_job(&pool, "DUMMY-OK-1").await;
    sqlx::query("UPDATE outbox_messages SET routing_key = 'job.track.v1.no_such_adapter'")
        .execute(&pool)
        .await
        .unwrap();
    let publisher = BrokerPublisher::new(
        broker.connection.create_channel().await.unwrap(),
        broker.topology.clone(),
    )
    .await
    .unwrap();

    let stats = relay_once(&pool, &publisher, 50).await.unwrap();
    assert_eq!((stats.sent, stats.failed), (0, 1));
    eventually(
        &pool,
        "SELECT status::text || ':' || (last_error LIKE 'unroutable%')::text FROM outbox_messages",
        "pending:true",
    )
    .await;
    broker.delete().await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_job_travels_to_the_worker_queue_and_its_result_completes_it(pool: PgPool) {
    let broker = Broker::start().await;
    let session = Session::start(broker.settings(), &pool).await;
    new_job(&pool, "DUMMY-OK-1").await;

    // The worker's side, by hand.
    let delivery = broker
        .take("adapter.dummy.jobs")
        .await
        .expect("job.track reaches the worker queue");
    let job: Value = serde_json::from_slice(&delivery.data).unwrap();
    delivery.ack(BasicAckOptions::default()).await.unwrap();

    let started = worker_message("job.started", &job, json!({}));
    broker.publish_event("job.started.v1", &started).await;
    eventually(
        &pool,
        "SELECT status::text FROM tracking_jobs",
        "PROCESSING",
    )
    .await;

    let mut result: Value =
        serde_json::from_slice(&worker_message("job.result", &job, success_extra())).unwrap();
    result["payload"]["execution_id"] =
        serde_json::from_slice::<Value>(&started).unwrap()["payload"]["execution_id"].clone();
    let result = result.to_string().into_bytes();
    broker.publish_event("job.result.v1", &result).await;
    // The same result again: at-least-once delivery must not change the outcome.
    broker.publish_event("job.result.v1", &result).await;

    eventually(&pool, "SELECT status::text FROM tracking_jobs", "COMPLETED").await;
    eventually(
        &pool,
        "SELECT current_status::text FROM shipments",
        "DELIVERED",
    )
    .await;
    eventually(
        &pool,
        "SELECT count(*)::text FROM processed_messages WHERE consumer = 'engine.events'",
        "2",
    )
    .await;
    assert_eq!(
        broker.depth("engine.events").await,
        0,
        "every event was acknowledged"
    );
    let counts: String = sqlx::query_scalar(
        "SELECT (SELECT count(*) FROM adapter_execution_logs) || '/' || (SELECT count(*) FROM tracking_events)
             || '/' || (SELECT count(*) FROM raw_tracking_responses)",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(counts, "1/1/1");

    session.stop().await;
    broker.delete().await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn invalid_events_are_parked_in_the_events_dlq_and_do_not_block_later_ones(pool: PgPool) {
    let broker = Broker::start().await;
    let session = Session::start(broker.settings(), &pool).await;
    new_job(&pool, "DUMMY-OK-1").await;
    let delivery = broker.take("adapter.dummy.jobs").await.unwrap();
    let job: Value = serde_json::from_slice(&delivery.data).unwrap();
    delivery.ack(BasicAckOptions::default()).await.unwrap();

    let valid = worker_message("job.result", &job, success_extra());
    let text = String::from_utf8(valid.clone()).unwrap();
    broker
        .publish_event(
            "job.result.v1",
            text.replace("\"outcome\":\"SUCCESS\"", "\"outcome\":\"MAYBE\"")
                .as_bytes(),
        )
        .await;
    broker
        .publish_event(
            "job.result.v1",
            text.replace("\"schema_version\":\"1.0\"", "\"schema_version\":\"2.0\"")
                .as_bytes(),
        )
        .await;
    broker.publish_event("job.result.v1", &valid).await;

    eventually(&pool, "SELECT status::text FROM tracking_jobs", "COMPLETED").await;
    let first = broker
        .take("engine.events.dlq")
        .await
        .expect("first invalid event is parked");
    let second = broker
        .take("engine.events.dlq")
        .await
        .expect("second invalid event is parked");
    let parked: Vec<String> = [&first, &second]
        .iter()
        .map(|d| String::from_utf8_lossy(&d.data).into_owned())
        .collect();
    assert!(parked.iter().any(|m| m.contains("MAYBE")));
    assert!(
        parked
            .iter()
            .any(|m| m.contains("\"schema_version\":\"2.0\""))
    );
    first.ack(BasicAckOptions::default()).await.unwrap();
    second.ack(BasicAckOptions::default()).await.unwrap();
    assert_eq!(broker.depth("engine.events").await, 0);

    session.stop().await;
    broker.delete().await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_job_message_that_exceeds_the_delivery_limit_is_dead_lettered_and_retried(pool: PgPool) {
    let broker = Broker::start().await;
    let session = Session::start(broker.settings(), &pool).await;
    new_job(&pool, "DUMMY-CRASH-1").await;

    // A worker that dies on every delivery: it receives the message and its channel closes
    // without an acknowledgement. RabbitMQ returns the message to the queue and counts the
    // failed delivery (x-delivery-limit: 3 → the fourth failure dead-letters it).
    // An explicit nack-with-requeue would NOT be counted by RabbitMQ 4 quorum queues.
    let mut deliveries = 0;
    loop {
        let worker = broker.connection.create_channel().await.unwrap();
        let mut received = None;
        for _ in 0..30 {
            received = worker
                .basic_get(
                    broker.name("adapter.dummy.jobs").into(),
                    BasicGetOptions::default(),
                )
                .await
                .unwrap();
            if received.is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        if received.is_none() {
            break;
        }
        deliveries += 1;
        worker.close(200, "worker crashed".into()).await.unwrap();
        assert!(deliveries <= 10, "the delivery limit must stop the loop");
    }
    assert_eq!(deliveries, 4, "first delivery plus three redeliveries");

    eventually(&pool, "SELECT status::text || '/' || dead_letter_count || '/' || last_error_class::text FROM tracking_jobs", "RETRYING/1/DELIVERY_LIMIT_EXCEEDED").await;
    eventually(
        &pool,
        "SELECT source_queue || '/' || reason || '/' || applied::text || '/' || attempt || '/' || (headers ? 'x-death')::text FROM dead_letters",
        "adapter.dummy.jobs/delivery_limit/true/1/true",
    )
    .await;
    assert_eq!(
        broker.depth("adapter.dummy.jobs.dlq").await,
        0,
        "the DLQ is emptied; the database holds the record"
    );

    session.stop().await;
    broker.delete().await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn readiness_follows_the_broker_session(pool: PgPool) {
    let broker = Broker::start().await;
    let session = Session::start(broker.settings(), &pool).await;
    let (_, key) = user(&pool, "a@example.com").await;
    let app = api::router(
        AppState::new(pool.clone(), Config::for_tests()).with_broker(session.health.clone()),
    );

    let ready = get(&app, "/readyz", &key).await;
    assert_eq!(ready.status, StatusCode::OK);
    assert_eq!(
        ready.body["checks"],
        json!({ "database": "ok", "broker": "ok" })
    );

    session.stop().await;
    let not_ready = get(&app, "/readyz", &key).await;
    assert_eq!(not_ready.status, StatusCode::SERVICE_UNAVAILABLE);
    // Liveness is unaffected, and the API still accepts work while the broker is away.
    assert_eq!(get(&app, "/healthz", &key).await.status, StatusCode::OK);
    assert_eq!(
        track(&app, &key, "DUMMY-OK-1").await.status,
        StatusCode::CREATED
    );
    broker.delete().await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn messages_wait_in_the_outbox_while_the_broker_is_unreachable_and_flow_when_it_returns(
    pool: PgPool,
) {
    new_job(&pool, "DUMMY-OK-1").await;
    new_job(&pool, "DUMMY-OK-2").await;

    // Nothing listens on this port.
    let broker = Broker::start().await;
    let mut unreachable = broker.settings();
    unreachable.amqp_url = "amqp://engine:wrong@127.0.0.1:1/courier".to_string();
    let shutdown = CancellationToken::new();
    let health = BrokerHealth::default();
    let task = tokio::spawn(broker::run(
        unreachable,
        pool.clone(),
        health.clone(),
        shutdown.clone(),
    ));
    tokio::time::sleep(Duration::from_millis(1500)).await;
    assert!(!health.is_connected());
    eventually(
        &pool,
        "SELECT count(*)::text FROM outbox_messages WHERE status = 'pending'",
        "2",
    )
    .await;
    shutdown.cancel();
    tokio::time::timeout(Duration::from_secs(10), task)
        .await
        .unwrap()
        .unwrap();

    // The broker "returns".
    let session = Session::start(broker.settings(), &pool).await;
    eventually(
        &pool,
        "SELECT count(*)::text FROM outbox_messages WHERE status = 'sent'",
        "2",
    )
    .await;
    assert!(broker.take("adapter.dummy.jobs").await.is_some());
    assert!(broker.take("adapter.dummy.jobs").await.is_some());
    session.stop().await;
    broker.delete().await;
}
