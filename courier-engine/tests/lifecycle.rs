//! Job lifecycle driven by worker messages, dead-letters, the reaper and the retry scheduler
//! (acceptance criteria 16–21, 27–31, 33–35, 37–40).
//!
//! These tests call the handlers directly with message bodies, exactly as the RabbitMQ
//! consumers do, so every ordering and duplication case is reproduced deterministically and
//! without a broker. `tests/broker.rs` covers the RabbitMQ wiring itself.
//! Deadlines are moved into the past with SQL instead of sleeping.

mod common;

use std::sync::{Arc, Mutex};

use common::{app, track, user};
use courier_engine::{
    db,
    messaging::relay::{OutboxMessage, Publish, relay_once},
    services::{
        close_attempt::RetryPolicy,
        dead_letter::{DeadLetterHandled, handle_dead_letter},
        events::{Handled, handle_event},
        reaper::reap_expired,
        retry_scheduler::open_due_attempts,
    },
};
use serde_json::{Value, json};
use sqlx::PgPool;
use uuid::Uuid;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;
const POLICY: RetryPolicy = RetryPolicy {
    max_dead_letter_retries: 1,
};

// ----------------------------------------------------------------------------- helpers

/// Creates a tracking request through the API and returns the queued job's ID.
async fn new_job(pool: &PgPool, tracking_number: &str) -> Uuid {
    let key = match sqlx::query_scalar::<_, Uuid>("SELECT id FROM users LIMIT 1")
        .fetch_optional(pool)
        .await
        .unwrap()
    {
        Some(user_id) => courier_engine::services::api_keys::issue(pool, user_id, None)
            .await
            .unwrap(),
        None => user(pool, "a@example.com").await.1,
    };
    let reply = track(&app(pool), &key, tracking_number).await;
    Uuid::parse_str(reply.body["latest_job"]["id"].as_str().unwrap()).unwrap()
}

fn envelope(message_type: &str, payload: Value) -> Vec<u8> {
    json!({
        "message_id": Uuid::now_v7(),
        "type": message_type,
        "schema_version": "1.0",
        "occurred_at": "2026-11-24T10:00:00Z",
        "correlation_id": Uuid::now_v7(),
        "causation_id": null,
        "producer": "adapter-dummy@0.1.0",
        "payload": payload,
    })
    .to_string()
    .into_bytes()
}

fn identity(job_id: Uuid, attempt: i32, execution_id: Uuid) -> Value {
    json!({
        "job_id": job_id, "attempt": attempt, "execution_id": execution_id, "delivery_count": 0,
        "worker_id": "dummy-test", "adapter_key": "dummy", "adapter_version": "0.1.0",
        "started_at": "2026-11-24T10:00:00.100Z",
    })
}

fn started(job_id: Uuid, attempt: i32, execution_id: Uuid) -> Vec<u8> {
    envelope("job.started", identity(job_id, attempt, execution_id))
}

fn result_with(job_id: Uuid, attempt: i32, execution_id: Uuid, extra: Value) -> Vec<u8> {
    let mut payload = identity(job_id, attempt, execution_id);
    payload["finished_at"] = json!("2026-11-24T10:00:00.300Z");
    for (key, value) in extra.as_object().unwrap() {
        payload[key] = value.clone();
    }
    envelope("job.result", payload)
}

fn event(raw_status: &str, occurred_at: &str) -> Value {
    json!({ "raw_status": raw_status, "raw_description": raw_status, "occurred_at": occurred_at,
            "location": { "city": "Chennai", "country": "IN" } })
}

fn success(job_id: Uuid, attempt: i32, execution_id: Uuid, events: Vec<Value>) -> Vec<u8> {
    result_with(
        job_id,
        attempt,
        execution_id,
        json!({ "outcome": "SUCCESS",
                "raw_response": { "content_type": "application/json", "body": { "execution": execution_id } },
                "events": events, "metrics": { "duration_ms": 200 } }),
    )
}

fn delivered(job_id: Uuid, attempt: i32, execution_id: Uuid) -> Vec<u8> {
    success(
        job_id,
        attempt,
        execution_id,
        vec![
            event("PKG_CREATED", "2026-11-22T08:00:00Z"),
            event("PKG_IN_TRANSIT", "2026-11-23T02:30:00Z"),
            event("PKG_DELIVERED", "2026-11-24T09:45:00Z"),
        ],
    )
}

fn failure(
    job_id: Uuid,
    attempt: i32,
    execution_id: Uuid,
    class: &str,
    retryable: bool,
) -> Vec<u8> {
    result_with(
        job_id,
        attempt,
        execution_id,
        json!({ "outcome": "FAILURE",
                "error": { "class": class, "message": "simulated", "retryable": retryable, "retry_after_ms": null } }),
    )
}

async fn handle(pool: &PgPool, body: &[u8]) -> Handled {
    handle_event(pool, body, POLICY).await.unwrap()
}

#[derive(Debug, sqlx::FromRow)]
struct Job {
    status: String,
    attempt: i32,
    dead_letter_count: i32,
    outcome: Option<String>,
    error: Option<String>,
    has_execution_deadline: bool,
    retry_in_ms: Option<f64>,
}

async fn job(pool: &PgPool, job_id: Uuid) -> Job {
    sqlx::query_as(
        "SELECT status::text AS status, attempt, dead_letter_count, outcome::text AS outcome,
                last_error_class::text AS error, execution_deadline_at IS NOT NULL AS has_execution_deadline,
                (extract(epoch FROM next_attempt_at - updated_at) * 1000)::float8 AS retry_in_ms
         FROM tracking_jobs WHERE id = $1",
    )
    .bind(job_id)
    .fetch_one(pool)
    .await
    .unwrap()
}

async fn scalar(pool: &PgPool, sql: &'static str) -> i64 {
    sqlx::query_scalar(sql).fetch_one(pool).await.unwrap()
}

async fn text(pool: &PgPool, sql: &'static str) -> String {
    sqlx::query_scalar::<_, Option<String>>(sql)
        .fetch_one(pool)
        .await
        .unwrap()
        .unwrap_or_default()
}

/// "disposition" of every execution of the job, ordered, e.g. "accepted,duplicate".
async fn dispositions(pool: &PgPool) -> String {
    text(pool, "SELECT string_agg(disposition::text, ',' ORDER BY disposition::text) FROM adapter_execution_logs").await
}

async fn exec(pool: &PgPool, sql: &'static str) {
    sqlx::query(sql).execute(pool).await.unwrap();
}

/// Makes every waiting retry due now, then runs the scheduler.
async fn run_due_retries(pool: &PgPool) -> usize {
    exec(pool, "UPDATE tracking_jobs SET next_attempt_at = clock_timestamp() - interval '1 second' WHERE status = 'RETRYING'").await;
    open_due_attempts(pool, 100).await.unwrap()
}

async fn expire_queue_deadlines(pool: &PgPool) {
    exec(pool, "UPDATE tracking_jobs SET queue_deadline_at = clock_timestamp() - interval '1 second' WHERE status = 'QUEUED'").await;
}

async fn expire_execution_deadlines(pool: &PgPool) {
    exec(pool, "UPDATE tracking_jobs SET execution_deadline_at = clock_timestamp() - interval '1 second' WHERE status = 'PROCESSING'").await;
}

/// The `job.track` message the Engine wrote for an attempt, as RabbitMQ would hand it back
/// from a dead-letter queue.
async fn job_track_body(pool: &PgPool, job_id: Uuid, attempt: i32) -> Vec<u8> {
    let payload: Value = sqlx::query_scalar(
        "SELECT payload FROM outbox_messages
         WHERE payload -> 'payload' ->> 'job_id' = $1::text AND (payload -> 'payload' ->> 'attempt')::int = $2",
    )
    .bind(job_id)
    .bind(attempt)
    .fetch_one(pool)
    .await
    .unwrap();
    payload.to_string().into_bytes()
}

async fn dead_letter(pool: &PgPool, body: &[u8]) -> DeadLetterHandled {
    handle_dead_letter(
        pool,
        body,
        "adapter.dummy.jobs",
        Some("delivery_limit"),
        &json!({ "x-first-death-reason": "delivery_limit" }),
        POLICY,
    )
    .await
    .unwrap()
}

// ----------------------------------------------------------------------------- happy path, normalization

#[sqlx::test(migrator = "MIGRATOR")]
async fn started_then_result_completes_the_job_and_updates_the_shipment(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let execution = Uuid::now_v7();
    assert!(
        !job(&pool, job_id).await.has_execution_deadline,
        "no execution deadline while QUEUED"
    );

    assert_eq!(
        handle(&pool, &started(job_id, 1, execution)).await,
        Handled::Processed
    );
    let processing = job(&pool, job_id).await;
    assert_eq!(processing.status, "PROCESSING");
    assert!(
        processing.has_execution_deadline,
        "set when job.started is processed"
    );

    assert_eq!(
        handle(&pool, &delivered(job_id, 1, execution)).await,
        Handled::Processed
    );
    let done = job(&pool, job_id).await;
    assert_eq!(
        (done.status.as_str(), done.outcome.as_deref()),
        ("COMPLETED", Some("SUCCESS"))
    );
    assert_eq!(done.error, None);

    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM raw_tracking_responses").await,
        1
    );
    assert_eq!(
        text(&pool, "SELECT string_agg(raw_status || '=' || normalized_status::text, ',' ORDER BY occurred_at) FROM tracking_events").await,
        "PKG_CREATED=CREATED,PKG_IN_TRANSIT=IN_TRANSIT,PKG_DELIVERED=DELIVERED"
    );
    assert_eq!(
        text(&pool, "SELECT current_status::text FROM shipments").await,
        "DELIVERED"
    );
    assert_eq!(scalar(&pool, "SELECT count(*) FROM shipments WHERE terminal_at IS NOT NULL AND last_event_at IS NOT NULL").await, 1);
    assert_eq!(dispositions(&pool).await, "accepted");
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM job_attempts a JOIN adapter_execution_logs l ON l.execution_id = a.accepted_execution_id WHERE a.closed_by = 'result' AND a.execution_count = 1").await,
        1
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_in_transit_result_leaves_the_shipment_in_transit(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-TRANSIT-1").await;
    let events = vec![
        event("PKG_CREATED", "2026-11-22T08:00:00Z"),
        event("PKG_IN_TRANSIT", "2026-11-23T02:30:00Z"),
    ];
    handle(&pool, &success(job_id, 1, Uuid::now_v7(), events)).await;
    assert_eq!(job(&pool, job_id).await.status, "COMPLETED");
    assert_eq!(
        text(&pool, "SELECT current_status::text FROM shipments").await,
        "IN_TRANSIT"
    );
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM shipments WHERE terminal_at IS NULL"
        )
        .await,
        1
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn not_found_completes_without_retry_and_leaves_the_shipment_unknown(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-NOTFOUND-1").await;
    handle(
        &pool,
        &result_with(job_id, 1, Uuid::now_v7(), json!({ "outcome": "NOT_FOUND" })),
    )
    .await;
    let done = job(&pool, job_id).await;
    assert_eq!(
        (done.status.as_str(), done.outcome.as_deref(), done.attempt),
        ("COMPLETED", Some("NOT_FOUND"), 1)
    );
    assert_eq!(
        text(&pool, "SELECT current_status::text FROM shipments").await,
        "UNKNOWN"
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM tracking_events").await,
        0
    );
    assert_eq!(run_due_retries(&pool).await, 0);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_unmapped_status_is_stored_as_unknown_with_the_raw_value_kept(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-ODD-1").await;
    let events = vec![
        event("PKG_IN_TRANSIT", "2026-11-23T02:30:00Z"),
        event("PKG_HELD_BY_ALIENS", "2026-11-24T02:30:00Z"),
    ];
    handle(&pool, &success(job_id, 1, Uuid::now_v7(), events)).await;
    assert_eq!(
        text(&pool, "SELECT normalized_status::text FROM tracking_events WHERE raw_status = 'PKG_HELD_BY_ALIENS'").await,
        "UNKNOWN"
    );
    // The newest event is unmapped, so the shipment keeps its last known status.
    assert_eq!(
        text(&pool, "SELECT current_status::text FROM shipments").await,
        "IN_TRANSIT"
    );
}

/// The other half of the refresh rule: a refreshed DELIVERED shipment keeps its status
/// until the new result is processed, and then follows that result.
#[sqlx::test(migrator = "MIGRATOR")]
async fn a_shipment_status_changes_only_when_a_result_is_processed(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);
    let request = track(&app, &key, "DUMMY-OK-1").await;
    let first = Uuid::parse_str(request.body["latest_job"]["id"].as_str().unwrap()).unwrap();
    handle(&pool, &delivered(first, 1, Uuid::now_v7())).await;

    let refreshed = common::call(
        &app,
        axum::http::Method::POST,
        &format!("/v1/tracking-requests/{}/refresh", request.id()),
        Some(&key),
        None,
    )
    .await;
    assert_eq!(refreshed.body["result"], "job_created");
    assert_eq!(
        text(&pool, "SELECT current_status::text FROM shipments").await,
        "DELIVERED"
    );

    let second = Uuid::parse_str(refreshed.body["job"]["id"].as_str().unwrap()).unwrap();
    handle(&pool, &started(second, 1, Uuid::now_v7())).await;
    assert_eq!(
        text(&pool, "SELECT current_status::text FROM shipments").await,
        "DELIVERED",
        "still pending"
    );

    // The provider corrects itself: the parcel was actually returned later.
    handle(
        &pool,
        &success(
            second,
            1,
            Uuid::now_v7(),
            vec![event("PKG_RETURNED", "2026-11-25T09:00:00Z")],
        ),
    )
    .await;
    assert_eq!(
        text(&pool, "SELECT current_status::text FROM shipments").await,
        "RETURNED"
    );
    // Events already known from the first lookup were not duplicated.
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM tracking_events").await,
        4
    );
}

// ----------------------------------------------------------------------------- idempotency

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_identical_result_delivered_twice_is_applied_once(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-DUP-1").await;
    let body = delivered(job_id, 1, Uuid::now_v7());
    assert_eq!(handle(&pool, &body).await, Handled::Processed);
    assert_eq!(handle(&pool, &body).await, Handled::Duplicate);

    assert_eq!(job(&pool, job_id).await.status, "COMPLETED");
    assert_eq!(scalar(&pool, "SELECT count(*) FROM job_attempts").await, 1);
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM adapter_execution_logs").await,
        1
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM raw_tracking_responses").await,
        1
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM tracking_events").await,
        3
    );
    assert_eq!(
        scalar(&pool, "SELECT execution_count::bigint FROM job_attempts").await,
        1
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn two_executions_of_one_attempt_give_one_accepted_and_one_duplicate(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let (a, b) = (Uuid::now_v7(), Uuid::now_v7());
    handle(&pool, &delivered(job_id, 1, a)).await;
    handle(&pool, &delivered(job_id, 1, b)).await;

    assert_eq!(scalar(&pool, "SELECT count(*) FROM job_attempts").await, 1);
    assert_eq!(
        scalar(&pool, "SELECT execution_count::bigint FROM job_attempts").await,
        2
    );
    assert_eq!(dispositions(&pool).await, "accepted,duplicate");
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM raw_tracking_responses").await,
        1
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM tracking_events").await,
        3
    );
    let accepted: Uuid = sqlx::query_scalar("SELECT accepted_execution_id FROM job_attempts")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(accepted, a, "the first result wins");
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_result_for_an_attempt_the_reaper_closed_is_stale_and_changes_nothing(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let old_execution = Uuid::now_v7();
    handle(&pool, &started(job_id, 1, old_execution)).await;
    expire_execution_deadlines(&pool).await;
    assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 1);
    assert_eq!(run_due_retries(&pool).await, 1);
    handle(&pool, &started(job_id, 2, Uuid::now_v7())).await;
    let before = job(&pool, job_id).await;
    assert_eq!((before.status.as_str(), before.attempt), ("PROCESSING", 2));

    // The lost worker's result for attempt 1 finally arrives.
    assert_eq!(
        handle(&pool, &delivered(job_id, 1, old_execution)).await,
        Handled::Processed
    );

    let after = job(&pool, job_id).await;
    assert_eq!((after.status.as_str(), after.attempt), ("PROCESSING", 2));
    assert_eq!(
        text(
            &pool,
            "SELECT disposition::text FROM adapter_execution_logs WHERE attempt = 1"
        )
        .await,
        "stale"
    );
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM job_attempts WHERE accepted_execution_id IS NOT NULL"
        )
        .await,
        0
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM raw_tracking_responses").await,
        0
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM tracking_events").await,
        0
    );
    assert_eq!(
        text(
            &pool,
            "SELECT closed_by::text FROM job_attempts WHERE attempt = 1"
        )
        .await,
        "reaper"
    );
}

// ----------------------------------------------------------------------------- message order

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_result_processed_before_its_started_message_is_complete_and_stays_untouched(
    pool: PgPool,
) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let execution = Uuid::now_v7();

    assert_eq!(
        handle(&pool, &delivered(job_id, 1, execution)).await,
        Handled::Processed
    );
    assert_eq!(job(&pool, job_id).await.status, "COMPLETED");
    let row_before = text(&pool, "SELECT started_at::text || finished_at::text || outcome::text || disposition::text FROM adapter_execution_logs").await;
    assert!(row_before.ends_with("SUCCESSaccepted"), "{row_before}");

    // The late job.started.
    assert_eq!(
        handle(&pool, &started(job_id, 1, execution)).await,
        Handled::Processed
    );

    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM adapter_execution_logs").await,
        1
    );
    assert_eq!(
        text(&pool, "SELECT started_at::text || finished_at::text || outcome::text || disposition::text FROM adapter_execution_logs").await,
        row_before
    );
    assert_eq!(
        scalar(&pool, "SELECT execution_count::bigint FROM job_attempts").await,
        1
    );
    let after = job(&pool, job_id).await;
    assert_eq!(after.status, "COMPLETED", "not moved back to PROCESSING");
    assert!(!after.has_execution_deadline);
    let accepted: Uuid = sqlx::query_scalar("SELECT accepted_execution_id FROM job_attempts")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(accepted, execution);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_result_without_any_started_message_gives_the_same_final_rows(pool: PgPool) {
    let with_started = new_job(&pool, "DUMMY-OK-1").await;
    let e1 = Uuid::now_v7();
    handle(&pool, &started(with_started, 1, e1)).await;
    handle(&pool, &delivered(with_started, 1, e1)).await;

    let without_started = new_job(&pool, "DUMMY-OK-2").await;
    handle(&pool, &delivered(without_started, 1, Uuid::now_v7())).await;

    let summary = "SELECT string_agg(DISTINCT j.status::text || '/' || j.outcome::text || '/' || a.execution_count
                       || '/' || a.closed_by::text || '/' || l.disposition::text || '/' || (l.started_at IS NOT NULL)::text
                       || '/' || (l.finished_at IS NOT NULL)::text, ' | ')
                   FROM tracking_jobs j
                   JOIN job_attempts a ON a.job_id = j.id
                   JOIN adapter_execution_logs l ON l.execution_id = a.accepted_execution_id";
    assert_eq!(
        text(&pool, summary).await,
        "COMPLETED/SUCCESS/1/result/accepted/true/true"
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM raw_tracking_responses").await,
        2
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn interleaved_messages_of_two_executions_still_accept_the_first_result(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let (a, b) = (Uuid::now_v7(), Uuid::now_v7());
    handle(&pool, &delivered(job_id, 1, a)).await; // result(A)
    handle(&pool, &started(job_id, 1, b)).await; // started(B)
    handle(&pool, &started(job_id, 1, a)).await; // started(A)
    handle(&pool, &delivered(job_id, 1, b)).await; // result(B)

    let disposition_of = |id: Uuid| {
        let pool = pool.clone();
        async move {
            sqlx::query_scalar::<_, String>(
                "SELECT disposition::text FROM adapter_execution_logs WHERE execution_id = $1",
            )
            .bind(id)
            .fetch_one(&pool)
            .await
            .unwrap()
        }
    };
    assert_eq!(disposition_of(a).await, "accepted");
    assert_eq!(disposition_of(b).await, "duplicate");
    let accepted: Uuid = sqlx::query_scalar("SELECT accepted_execution_id FROM job_attempts")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(accepted, a);
    assert_eq!(
        scalar(&pool, "SELECT execution_count::bigint FROM job_attempts").await,
        2
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM raw_tracking_responses").await,
        1
    );
    assert_eq!(job(&pool, job_id).await.status, "COMPLETED");
}

// ----------------------------------------------------------------------------- rejection

#[sqlx::test(migrator = "MIGRATOR")]
async fn malformed_unsupported_and_unknown_messages_are_rejected_without_side_effects(
    pool: PgPool,
) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let good = String::from_utf8(delivered(job_id, 1, Uuid::now_v7())).unwrap();

    let cases: Vec<(&str, Vec<u8>)> = vec![
        ("not JSON", b"{not json".to_vec()),
        (
            "unknown major version",
            good.replace("\"schema_version\":\"1.0\"", "\"schema_version\":\"2.0\"")
                .into_bytes(),
        ),
        (
            "schema-invalid",
            good.replace("\"outcome\":\"SUCCESS\"", "\"outcome\":\"MAYBE\"")
                .into_bytes(),
        ),
        (
            "unknown type",
            good.replace("\"type\":\"job.result\"", "\"type\":\"job.exploded\"")
                .into_bytes(),
        ),
        (
            "engine-only error class",
            failure(job_id, 1, Uuid::now_v7(), "WORKER_LOST", true),
        ),
        ("unknown job", delivered(Uuid::now_v7(), 1, Uuid::now_v7())),
        ("unknown attempt", delivered(job_id, 7, Uuid::now_v7())),
        (
            "started for unknown job",
            started(Uuid::now_v7(), 1, Uuid::now_v7()),
        ),
        ("oversized", vec![b' '; 262_145]),
    ];
    for (name, body) in cases {
        assert!(
            matches!(handle(&pool, &body).await, Handled::Rejected(_)),
            "{name}"
        );
    }
    assert_eq!(job(&pool, job_id).await.status, "QUEUED");
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM processed_messages").await,
        0
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM adapter_execution_logs").await,
        0
    );

    // A later valid message is processed normally.
    assert_eq!(handle(&pool, good.as_bytes()).await, Handled::Processed);
    assert_eq!(job(&pool, job_id).await.status, "COMPLETED");
}

// ----------------------------------------------------------------------------- failures and retries

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_retryable_failure_is_retried_and_then_succeeds(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-FLAKY-1").await;
    handle(&pool, &started(job_id, 1, Uuid::now_v7())).await;
    handle(
        &pool,
        &failure(job_id, 1, Uuid::now_v7(), "NETWORK_TIMEOUT", true),
    )
    .await;
    let retrying = job(&pool, job_id).await;
    assert_eq!(
        (retrying.status.as_str(), retrying.attempt),
        ("RETRYING", 1)
    );
    assert_eq!(retrying.error.as_deref(), Some("NETWORK_TIMEOUT"));

    assert_eq!(
        open_due_attempts(&pool, 100).await.unwrap(),
        0,
        "not due yet"
    );
    assert_eq!(run_due_retries(&pool).await, 1);
    let queued = job(&pool, job_id).await;
    assert_eq!((queued.status.as_str(), queued.attempt), ("QUEUED", 2));
    assert!(
        !queued.has_execution_deadline,
        "cleared for the new attempt"
    );

    let e2 = Uuid::now_v7();
    handle(&pool, &started(job_id, 2, e2)).await;
    assert_eq!(job(&pool, job_id).await.status, "PROCESSING");
    handle(&pool, &delivered(job_id, 2, e2)).await;
    assert_eq!(job(&pool, job_id).await.status, "COMPLETED");

    assert_eq!(scalar(&pool, "SELECT count(*) FROM job_attempts").await, 2);
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM outbox_messages").await,
        2,
        "one message per attempt"
    );
    assert_eq!(
        text(&pool, "SELECT string_agg((payload -> 'payload' ->> 'attempt'), ',' ORDER BY created_at) FROM outbox_messages").await,
        "1,2"
    );
    // The retry keeps the correlation ID of the original request.
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(DISTINCT payload ->> 'correlation_id') FROM outbox_messages"
        )
        .await,
        1
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_job_that_always_fails_ends_failed_after_max_attempts_with_growing_delays(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-FAIL-1").await;
    let mut delays = Vec::new();
    for attempt in 1..=3 {
        handle(
            &pool,
            &failure(job_id, attempt, Uuid::now_v7(), "HTTP_5XX", true),
        )
        .await;
        let state = job(&pool, job_id).await;
        if attempt < 3 {
            assert_eq!(state.status, "RETRYING", "attempt {attempt}");
            delays.push(state.retry_in_ms.unwrap());
            run_due_retries(&pool).await;
        } else {
            assert_eq!(
                (state.status.as_str(), state.outcome.as_deref()),
                ("FAILED", Some("FAILURE"))
            );
            assert_eq!(state.error.as_deref(), Some("HTTP_5XX"));
        }
    }
    // base 1000 ms, doubling, ±20 % jitter.
    assert!((800.0..=1200.0).contains(&delays[0]), "{delays:?}");
    assert!((1600.0..=2400.0).contains(&delays[1]), "{delays:?}");
    assert!(delays[1] > delays[0]);
    assert_eq!(scalar(&pool, "SELECT count(*) FROM job_attempts").await, 3);
    assert_eq!(
        run_due_retries(&pool).await,
        0,
        "a failed job is never retried"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_non_retryable_failure_fails_after_one_attempt(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-FATAL-1").await;
    handle(
        &pool,
        &failure(job_id, 1, Uuid::now_v7(), "AUTH_INVALID", false),
    )
    .await;
    let state = job(&pool, job_id).await;
    assert_eq!((state.status.as_str(), state.attempt), ("FAILED", 1));
    assert_eq!(scalar(&pool, "SELECT count(*) FROM job_attempts").await, 1);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_provider_requested_wait_delays_the_retry(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-FAIL-1").await;
    let body = result_with(
        job_id,
        1,
        Uuid::now_v7(),
        json!({ "outcome": "FAILURE",
                "error": { "class": "RATE_LIMITED", "message": "slow down", "retryable": true, "retry_after_ms": 30000 } }),
    );
    handle(&pool, &body).await;
    let delay = job(&pool, job_id).await.retry_in_ms.unwrap();
    assert!((29_900.0..=30_100.0).contains(&delay), "{delay}");
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_worker_crash_followed_by_a_redelivery_completes_on_the_same_attempt(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-CRASHONCE-1").await;
    handle(&pool, &started(job_id, 1, Uuid::now_v7())).await; // the worker dies here
    let second = Uuid::now_v7();
    handle(&pool, &started(job_id, 1, second)).await; // redelivery, new execution
    handle(&pool, &delivered(job_id, 1, second)).await;

    let state = job(&pool, job_id).await;
    assert_eq!((state.status.as_str(), state.attempt), ("COMPLETED", 1));
    assert_eq!(
        scalar(&pool, "SELECT execution_count::bigint FROM job_attempts").await,
        2
    );
    assert_eq!(dispositions(&pool).await, "accepted,incomplete");
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_redelivery_moves_the_execution_deadline_forward(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-CRASHONCE-1").await;
    handle(&pool, &started(job_id, 1, Uuid::now_v7())).await;
    exec(
        &pool,
        "UPDATE tracking_jobs SET execution_deadline_at = clock_timestamp() + interval '1 second'",
    )
    .await;
    handle(&pool, &started(job_id, 1, Uuid::now_v7())).await;
    // dummy provider: timeout 10 s + grace 30 s
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM tracking_jobs WHERE execution_deadline_at > clock_timestamp() + interval '35 seconds'").await,
        1
    );
}

// ----------------------------------------------------------------------------- reaper

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_attempt_nobody_started_is_closed_as_queue_wait_exceeded_until_the_job_fails(
    pool: PgPool,
) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    assert_eq!(
        reap_expired(&pool, 100, POLICY).await.unwrap(),
        0,
        "deadline not reached"
    );

    for attempt in 1..=3 {
        expire_queue_deadlines(&pool).await;
        assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 1);
        let state = job(&pool, job_id).await;
        assert_eq!(
            state.error.as_deref(),
            Some("QUEUE_WAIT_EXCEEDED"),
            "never ADAPTER_TIMEOUT or WORKER_LOST"
        );
        if attempt < 3 {
            assert_eq!(state.status, "RETRYING");
            run_due_retries(&pool).await;
        } else {
            assert_eq!(state.status, "FAILED");
        }
    }
    assert_eq!(scalar(&pool, "SELECT count(*) FROM tracking_jobs WHERE status IN ('QUEUED', 'PROCESSING', 'RETRYING')").await, 0);
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM adapter_execution_logs").await,
        0
    );
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM job_attempts WHERE closed_by = 'reaper'"
        )
        .await,
        3
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_started_execution_that_never_reports_is_closed_as_worker_lost(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-HANG-1").await;
    handle(&pool, &started(job_id, 1, Uuid::now_v7())).await;

    // The queue-wait deadline no longer applies to a PROCESSING job, however old it is.
    exec(
        &pool,
        "UPDATE tracking_jobs SET queue_deadline_at = clock_timestamp() - interval '1 hour'",
    )
    .await;
    assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 0);

    expire_execution_deadlines(&pool).await;
    assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 1);
    let state = job(&pool, job_id).await;
    assert_eq!(
        (state.status.as_str(), state.error.as_deref()),
        ("RETRYING", Some("WORKER_LOST"))
    );
    assert_eq!(dispositions(&pool).await, "incomplete");
}

/// Queue time is not execution time: a job that waited far longer than the adapter timeout
/// and then ran normally completes on attempt 1 with no error.
#[sqlx::test(migrator = "MIGRATOR")]
async fn a_long_wait_in_the_queue_is_not_a_timeout(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    // Dispatched 10 minutes ago: past timeout (10 s) + grace (30 s), inside the 15-minute queue wait.
    exec(&pool, "UPDATE tracking_jobs SET created_at = created_at - interval '10 minutes', queue_deadline_at = queue_deadline_at - interval '10 minutes'").await;
    assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 0);

    let execution = Uuid::now_v7();
    handle(&pool, &started(job_id, 1, execution)).await;
    assert_eq!(
        reap_expired(&pool, 100, POLICY).await.unwrap(),
        0,
        "the execution deadline starts at job.started"
    );
    handle(&pool, &delivered(job_id, 1, execution)).await;

    let state = job(&pool, job_id).await;
    assert_eq!(
        (state.status.as_str(), state.attempt, state.error),
        ("COMPLETED", 1, None)
    );
    assert_eq!(
        scalar(&pool, "SELECT execution_count::bigint FROM job_attempts").await,
        1
    );
}

// ----------------------------------------------------------------------------- dead letters

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_poisonous_job_is_retried_once_after_a_dead_letter_and_then_fails(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-CRASH-1").await;

    assert_eq!(
        dead_letter(&pool, &job_track_body(&pool, job_id, 1).await).await,
        DeadLetterHandled::Applied
    );
    let first = job(&pool, job_id).await;
    assert_eq!(
        (first.status.as_str(), first.dead_letter_count),
        ("RETRYING", 1)
    );
    assert_eq!(first.error.as_deref(), Some("DELIVERY_LIMIT_EXCEEDED"));

    assert_eq!(run_due_retries(&pool).await, 1);
    assert_eq!(
        dead_letter(&pool, &job_track_body(&pool, job_id, 2).await).await,
        DeadLetterHandled::Applied
    );
    let second = job(&pool, job_id).await;
    assert_eq!(
        (
            second.status.as_str(),
            second.dead_letter_count,
            second.attempt
        ),
        ("FAILED", 2, 2)
    );
    assert_eq!(second.error.as_deref(), Some("DELIVERY_LIMIT_EXCEEDED"));

    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM dead_letters d JOIN tracking_jobs j ON j.id = d.job_id WHERE d.applied AND d.source_queue = 'adapter.dummy.jobs' AND d.reason = 'delivery_limit'").await,
        2
    );
    assert_eq!(
        text(
            &pool,
            "SELECT string_agg(attempt::text, ',' ORDER BY attempt) FROM dead_letters"
        )
        .await,
        "1,2"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_dead_letter_for_the_current_attempt_counts_once_even_if_delivered_twice(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-CRASH-1").await;
    let body = job_track_body(&pool, job_id, 1).await;
    assert_eq!(dead_letter(&pool, &body).await, DeadLetterHandled::Applied);
    assert_eq!(
        dead_letter(&pool, &body).await,
        DeadLetterHandled::Duplicate
    );

    let state = job(&pool, job_id).await;
    assert_eq!(
        (state.status.as_str(), state.dead_letter_count),
        ("RETRYING", 1)
    );
    assert_eq!(scalar(&pool, "SELECT count(*) FROM dead_letters").await, 1);
    assert_eq!(
        text(&pool, "SELECT closed_by::text FROM job_attempts").await,
        "dead_letter"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_stale_dead_letter_does_not_count_and_does_not_disturb_the_running_attempt(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let old_message = job_track_body(&pool, job_id, 1).await;

    // The reaper closes attempt 1; attempt 2 is opened and running.
    expire_queue_deadlines(&pool).await;
    reap_expired(&pool, 100, POLICY).await.unwrap();
    run_due_retries(&pool).await;
    let execution = Uuid::now_v7();
    handle(&pool, &started(job_id, 2, execution)).await;

    // Only now does the dead-letter for attempt 1 arrive.
    assert_eq!(
        dead_letter(&pool, &old_message).await,
        DeadLetterHandled::RecordedOnly
    );
    let state = job(&pool, job_id).await;
    assert_eq!(
        (
            state.status.as_str(),
            state.attempt,
            state.dead_letter_count
        ),
        ("PROCESSING", 2, 0)
    );
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM dead_letters WHERE NOT applied AND attempt = 1"
        )
        .await,
        1
    );
    assert_eq!(
        text(
            &pool,
            "SELECT closed_by::text FROM job_attempts WHERE attempt = 1"
        )
        .await,
        "reaper"
    );

    handle(&pool, &delivered(job_id, 2, execution)).await;
    assert_eq!(job(&pool, job_id).await.status, "COMPLETED");
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_dead_letter_after_a_result_changes_nothing(pool: PgPool) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let message = job_track_body(&pool, job_id, 1).await;
    handle(&pool, &delivered(job_id, 1, Uuid::now_v7())).await;
    assert_eq!(
        dead_letter(&pool, &message).await,
        DeadLetterHandled::RecordedOnly
    );
    let state = job(&pool, job_id).await;
    assert_eq!(
        (state.status.as_str(), state.dead_letter_count),
        ("COMPLETED", 0)
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_unreadable_dead_letter_is_recorded_for_operators(pool: PgPool) {
    let handled = handle_dead_letter(
        &pool,
        b"\xff not json",
        "adapter.dummy.jobs",
        Some("rejected"),
        &json!({}),
        POLICY,
    )
    .await
    .unwrap();
    assert_eq!(handled, DeadLetterHandled::RecordedOnly);
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM dead_letters WHERE NOT applied AND job_id IS NULL"
        )
        .await,
        1
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn dead_letter_and_reaper_on_the_same_attempt_produce_exactly_one_retry(pool: PgPool) {
    // Order 1: dead-letter first, reaper later.
    let first = new_job(&pool, "DUMMY-CRASH-1").await;
    dead_letter(&pool, &job_track_body(&pool, first, 1).await).await;
    exec(
        &pool,
        "UPDATE tracking_jobs SET queue_deadline_at = clock_timestamp() - interval '1 second'",
    )
    .await;
    assert_eq!(
        reap_expired(&pool, 100, POLICY).await.unwrap(),
        0,
        "already closed"
    );

    // Order 2: reaper first, dead-letter later.
    let second = new_job(&pool, "DUMMY-CRASH-2").await;
    let message = job_track_body(&pool, second, 1).await;
    expire_queue_deadlines(&pool).await;
    assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 1);
    assert_eq!(
        dead_letter(&pool, &message).await,
        DeadLetterHandled::RecordedOnly
    );

    assert_eq!(run_due_retries(&pool).await, 2);
    assert_eq!(run_due_retries(&pool).await, 0);
    for job_id in [first, second] {
        let attempts: i64 =
            sqlx::query_scalar("SELECT count(*) FROM job_attempts WHERE job_id = $1")
                .bind(job_id)
                .fetch_one(&pool)
                .await
                .unwrap();
        assert_eq!(attempts, 2, "exactly one new attempt");
        let messages: i64 = sqlx::query_scalar("SELECT count(*) FROM outbox_messages WHERE payload -> 'payload' ->> 'job_id' = $1::text")
            .bind(job_id)
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(messages, 2, "exactly one new outbox row");
    }
    assert_eq!(job(&pool, first).await.dead_letter_count, 1);
    assert_eq!(job(&pool, second).await.dead_letter_count, 0);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_concurrent_dead_letter_and_reaper_never_count_without_closing(pool: PgPool) {
    for round in 0..15 {
        let job_id = new_job(&pool, &format!("DUMMY-RACE-{round}")).await;
        let message = job_track_body(&pool, job_id, 1).await;
        expire_queue_deadlines(&pool).await;

        let barrier = Arc::new(tokio::sync::Barrier::new(2));
        let reaper = tokio::spawn({
            let (pool, barrier) = (pool.clone(), barrier.clone());
            async move {
                barrier.wait().await;
                reap_expired(&pool, 100, POLICY).await.unwrap()
            }
        });
        let dead = tokio::spawn({
            let (pool, barrier) = (pool.clone(), barrier.clone());
            async move {
                barrier.wait().await;
                dead_letter(&pool, &message).await
            }
        });
        let (reaped, dead) = (reaper.await.unwrap(), dead.await.unwrap());

        let state = job(&pool, job_id).await;
        assert_eq!(state.status, "RETRYING", "round {round}");
        let closed_by: String =
            sqlx::query_scalar("SELECT closed_by::text FROM job_attempts WHERE job_id = $1")
                .bind(job_id)
                .fetch_one(&pool)
                .await
                .unwrap();
        match closed_by.as_str() {
            "dead_letter" => {
                assert_eq!(
                    (state.dead_letter_count, dead, reaped),
                    (1, DeadLetterHandled::Applied, 0),
                    "round {round}"
                );
            }
            "reaper" => {
                assert_eq!(
                    (state.dead_letter_count, dead, reaped),
                    (0, DeadLetterHandled::RecordedOnly, 1),
                    "round {round}"
                );
            }
            other => panic!("unexpected closer {other}"),
        }
        // Finish the job so the next round's deadline update touches only its own job.
        exec(&pool, "UPDATE tracking_jobs SET status = 'FAILED', completed_at = clock_timestamp(), next_attempt_at = NULL WHERE status = 'RETRYING'").await;
    }
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn concurrent_reapers_and_schedulers_never_duplicate_an_attempt(pool: PgPool) {
    let mut jobs = Vec::new();
    for n in 0..12 {
        jobs.push(new_job(&pool, &format!("DUMMY-MANY-{n}")).await);
    }
    expire_queue_deadlines(&pool).await;

    let reapers: Vec<_> = (0..2)
        .map(|_| {
            let pool = pool.clone();
            tokio::spawn(async move { reap_expired(&pool, 100, POLICY).await.unwrap() })
        })
        .collect();
    let mut reaped = 0;
    for task in reapers {
        reaped += task.await.unwrap();
    }
    assert_eq!(reaped, 12, "each attempt closed by exactly one reaper");

    exec(
        &pool,
        "UPDATE tracking_jobs SET next_attempt_at = clock_timestamp() - interval '1 second'",
    )
    .await;
    let schedulers: Vec<_> = (0..2)
        .map(|_| {
            let pool = pool.clone();
            tokio::spawn(async move { open_due_attempts(&pool, 100).await.unwrap() })
        })
        .collect();
    let mut opened = 0;
    for task in schedulers {
        opened += task.await.unwrap();
    }
    assert_eq!(opened, 12, "each retry opened by exactly one scheduler");

    assert_eq!(scalar(&pool, "SELECT count(*) FROM job_attempts").await, 24);
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM outbox_messages").await,
        24
    );
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM tracking_jobs WHERE status = 'QUEUED' AND attempt = 2"
        )
        .await,
        12
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM (SELECT payload -> 'payload' ->> 'job_id', payload -> 'payload' ->> 'attempt' FROM outbox_messages GROUP BY 1, 2 HAVING count(*) > 1) d").await,
        0,
        "no attempt has two messages"
    );
}

// ----------------------------------------------------------------------------- outbox relay

/// Stands in for RabbitMQ: records what was published and can be told to fail.
#[derive(Clone, Default)]
struct FakeBroker {
    published: Arc<Mutex<Vec<Uuid>>>,
    fail: Arc<Mutex<bool>>,
    panic_after_publish: Arc<Mutex<bool>>,
}

impl FakeBroker {
    fn published(&self) -> Vec<Uuid> {
        self.published.lock().unwrap().clone()
    }
}

impl Publish for FakeBroker {
    async fn publish(&self, message: &OutboxMessage) -> Result<(), String> {
        if *self.fail.lock().unwrap() {
            return Err("broker unreachable".into());
        }
        // The envelope in the row is what goes on the wire.
        assert_eq!(message.payload["message_id"], json!(message.message_id));
        assert_eq!(message.exchange, "courier.jobs");
        assert_eq!(message.routing_key, "job.track.v1.dummy");
        self.published.lock().unwrap().push(message.message_id);
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        if *self.panic_after_publish.lock().unwrap() {
            panic!("relay killed after the broker confirmed");
        }
        Ok(())
    }
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_relay_publishes_each_pending_message_and_marks_it_sent(pool: PgPool) {
    for n in 0..3 {
        new_job(&pool, &format!("DUMMY-OK-{n}")).await;
    }
    let broker = FakeBroker::default();
    let stats = relay_once(&pool, &broker, 50).await.unwrap();
    assert_eq!((stats.sent, stats.failed), (3, 0));
    assert_eq!(broker.published().len(), 3);
    assert_eq!(scalar(&pool, "SELECT count(*) FROM outbox_messages WHERE status = 'sent' AND sent_at IS NOT NULL AND attempts = 1").await, 3);
    assert_eq!(
        relay_once(&pool, &broker, 50).await.unwrap().sent,
        0,
        "nothing left"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn messages_stay_pending_while_the_broker_is_down_and_go_out_when_it_returns(pool: PgPool) {
    new_job(&pool, "DUMMY-OK-1").await;
    new_job(&pool, "DUMMY-OK-2").await;
    let broker = FakeBroker::default();
    *broker.fail.lock().unwrap() = true;

    let stats = relay_once(&pool, &broker, 50).await.unwrap();
    assert_eq!(
        (stats.sent, stats.failed),
        (0, 1),
        "the batch stops at the first failure"
    );
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM outbox_messages WHERE status = 'pending'"
        )
        .await,
        2
    );
    assert_eq!(scalar(&pool, "SELECT count(*) FROM outbox_messages WHERE last_error = 'broker unreachable' AND available_at > clock_timestamp()").await, 1);

    *broker.fail.lock().unwrap() = false;
    exec(
        &pool,
        "UPDATE outbox_messages SET available_at = clock_timestamp() - interval '1 second'",
    )
    .await;
    assert_eq!(relay_once(&pool, &broker, 50).await.unwrap().sent, 2);
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM outbox_messages WHERE status = 'sent' AND last_error IS NULL"
        )
        .await,
        2
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_relay_that_dies_after_the_confirm_publishes_again_with_the_same_message_id(
    pool: PgPool,
) {
    let job_id = new_job(&pool, "DUMMY-OK-1").await;
    let broker = FakeBroker::default();
    *broker.panic_after_publish.lock().unwrap() = true;

    let crashed = tokio::spawn({
        let (pool, broker) = (pool.clone(), broker.clone());
        async move { relay_once(&pool, &broker, 50).await }
    })
    .await;
    assert!(crashed.is_err(), "the relay task died");
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM outbox_messages WHERE status = 'pending'"
        )
        .await,
        1,
        "never marked sent"
    );

    *broker.panic_after_publish.lock().unwrap() = false;
    assert_eq!(relay_once(&pool, &broker, 50).await.unwrap().sent, 1);
    let published = broker.published();
    assert_eq!(published.len(), 2, "published twice");
    assert_eq!(published[0], published[1], "with the same message_id");

    // A worker that received both copies reports twice for the same attempt; the final
    // state equals that of a single publish.
    let (a, b) = (Uuid::now_v7(), Uuid::now_v7());
    handle(&pool, &delivered(job_id, 1, a)).await;
    handle(&pool, &delivered(job_id, 1, b)).await;
    assert_eq!(job(&pool, job_id).await.status, "COMPLETED");
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM raw_tracking_responses").await,
        1
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM tracking_events").await,
        3
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn two_relays_running_together_publish_each_message_once(pool: PgPool) {
    for n in 0..10 {
        new_job(&pool, &format!("DUMMY-OK-{n}")).await;
    }
    let broker = FakeBroker::default();
    let relays: Vec<_> = (0..2)
        .map(|_| {
            let (pool, broker) = (pool.clone(), broker.clone());
            tokio::spawn(async move {
                let mut sent = 0;
                loop {
                    let stats = relay_once(&pool, &broker, 3).await.unwrap();
                    if stats.sent == 0 {
                        // Rows locked by the other relay are skipped; stop when none are pending.
                        let pending: i64 = sqlx::query_scalar(
                            "SELECT count(*) FROM outbox_messages WHERE status = 'pending'",
                        )
                        .fetch_one(&pool)
                        .await
                        .unwrap();
                        if pending == 0 {
                            return sent;
                        }
                    }
                    sent += stats.sent;
                }
            })
        })
        .collect();
    let mut sent = 0;
    for relay in relays {
        sent += relay.await.unwrap();
    }
    assert_eq!(sent, 10);
    let mut published = broker.published();
    published.sort();
    published.dedup();
    assert_eq!(published.len(), 10);
    assert_eq!(broker.published().len(), 10, "no message published twice");
}
