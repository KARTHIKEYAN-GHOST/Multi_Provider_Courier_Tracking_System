//! Phase 8: one-time-code and CAPTCHA challenges — waiting, answering, resuming, expiry.
//!
//! As in `lifecycle.rs`, worker messages are handed to the handlers directly, so every case
//! is deterministic and needs no broker. Deadlines are moved with SQL instead of sleeping.

mod common;

use axum::{Router, http::Method};
use common::{call, get, track, user};
use courier_engine::{
    api,
    config::Config,
    db,
    services::{
        close_attempt::RetryPolicy,
        credentials::PgSecretStore,
        crypto::MasterKey,
        events::{Handled, handle_event},
        interventions::{self, ResponseRequest, ResponseResolved},
        reaper::reap_expired,
        worker_tokens,
    },
    state::AppState,
};
use serde_json::{Value, json};
use sqlx::PgPool;
use uuid::Uuid;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;
const POLICY: RetryPolicy = RetryPolicy {
    max_dead_letter_retries: 1,
};
const ANSWER: &str = "424242";
/// A 1x1 PNG.
const PNG: &str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

fn master() -> MasterKey {
    MasterKey::from_hex(&"7c".repeat(32), 1).unwrap()
}

fn state(pool: &PgPool) -> AppState {
    AppState::new(pool.clone(), Config::for_tests()).with_secrets(PgSecretStore::new(master()))
}

fn app(pool: &PgPool) -> Router {
    api::router(state(pool))
}

fn envelope(message_type: &str, payload: Value) -> Vec<u8> {
    json!({
        "message_id": Uuid::now_v7(), "type": message_type, "schema_version": "1.0",
        "occurred_at": "2026-11-24T10:00:00Z", "correlation_id": Uuid::now_v7(),
        "causation_id": null, "producer": "adapter-dummy@0.1.0", "payload": payload,
    })
    .to_string()
    .into_bytes()
}

fn identity(job_id: Uuid, execution_id: Uuid) -> Value {
    json!({
        "job_id": job_id, "attempt": 1, "execution_id": execution_id, "delivery_count": 0,
        "worker_id": "dummy-test", "adapter_key": "dummy", "adapter_version": "0.1.0",
        "started_at": "2026-11-24T10:00:00.100Z",
    })
}

fn started(job_id: Uuid, execution_id: Uuid) -> Vec<u8> {
    envelope("job.started", identity(job_id, execution_id))
}

fn challenge(job_id: Uuid, execution_id: Uuid, extra: Value) -> Vec<u8> {
    let mut payload = identity(job_id, execution_id);
    payload["finished_at"] = json!("2026-11-24T10:00:00.300Z");
    payload["kind"] = json!("otp");
    payload["prompt"] = json!("Enter the 6-digit code sent to your phone.");
    payload["session_ref"] = json!(Uuid::now_v7());
    payload["response_format"] = json!({ "min_length": 6, "max_length": 6, "charset": "digits" });
    for (key, value) in extra.as_object().unwrap() {
        payload[key] = value.clone();
    }
    envelope("job.intervention_required", payload)
}

fn delivered(job_id: Uuid, execution_id: Uuid) -> Vec<u8> {
    let mut payload = identity(job_id, execution_id);
    payload["finished_at"] = json!("2026-11-24T10:00:00.300Z");
    payload["outcome"] = json!("SUCCESS");
    payload["raw_response"] = json!({ "content_type": "application/json", "body": {} });
    payload["events"] = json!([{ "raw_status": "PKG_DELIVERED", "raw_description": "Delivered",
                                 "occurred_at": "2026-11-24T09:45:00Z" }]);
    payload["metrics"] = json!({ "duration_ms": 200 });
    envelope("job.result", payload)
}

async fn handle(pool: &PgPool, body: &[u8]) -> Handled {
    handle_event(pool, body, POLICY).await.unwrap()
}

async fn text(pool: &PgPool, sql: &'static str) -> String {
    sqlx::query_scalar::<_, Option<String>>(sql)
        .fetch_one(pool)
        .await
        .unwrap()
        .unwrap_or_default()
}

async fn scalar(pool: &PgPool, sql: &'static str) -> i64 {
    sqlx::query_scalar(sql).fetch_one(pool).await.unwrap()
}

async fn exec(pool: &PgPool, sql: &'static str) {
    sqlx::query(sql).execute(pool).await.unwrap();
}

async fn job_status(pool: &PgPool) -> String {
    text(pool, "SELECT status::text FROM tracking_jobs").await
}

/// A user, a tracking request, and its job stopped at a challenge.
/// Returns (user key, job id, intervention id).
async fn waiting_job(pool: &PgPool, extra: Value) -> (String, Uuid, String) {
    let (_, key) = user(pool, "owner@example.com").await;
    let reply = track(&app(pool), &key, "DUMMY-OK-1").await;
    let job_id = Uuid::parse_str(reply.body["latest_job"]["id"].as_str().unwrap()).unwrap();
    let execution = Uuid::now_v7();
    assert_eq!(
        handle(pool, &started(job_id, execution)).await,
        Handled::Processed
    );
    assert_eq!(
        handle(pool, &challenge(job_id, execution, extra)).await,
        Handled::Processed
    );
    let list = get(&app(pool), "/v1/interventions", &key).await;
    let id = list.body["items"][0]["id"]
        .as_str()
        .expect("one open challenge")
        .to_string();
    (key, job_id, id)
}

async fn answer(pool: &PgPool, key: &str, id: &str, response: &str) -> common::Reply {
    call(
        &app(pool),
        Method::POST,
        &format!("/v1/interventions/{id}/response"),
        Some(key),
        Some(json!({ "response": response })),
    )
    .await
}

async fn worker(pool: &PgPool, adapter: &str) -> worker_tokens::WorkerIdentity {
    let token = worker_tokens::issue(pool, adapter, Some("test"))
        .await
        .unwrap()
        .unwrap();
    worker_tokens::authenticate(pool, &token)
        .await
        .unwrap()
        .unwrap()
}

/// The session reference stored with a challenge (what `job.resume` carries to the worker).
async fn session_of(pool: &PgPool, id: &str) -> Uuid {
    sqlx::query_scalar("SELECT session_ref FROM human_interventions WHERE id = $1")
        .bind(Uuid::parse_str(id).unwrap())
        .fetch_one(pool)
        .await
        .unwrap()
}

/// A worker of `adapter` asks for the answer, naming the session it holds.
async fn fetch_with_session(
    pool: &PgPool,
    adapter: &str,
    job_id: Uuid,
    id: &str,
    session_ref: Uuid,
) -> Result<String, &'static str> {
    let request = ResponseRequest {
        intervention_id: Uuid::parse_str(id).unwrap(),
        session_ref,
        job_id,
        attempt: 1,
    };
    match interventions::resolve_response(pool, &master(), &worker(pool, adapter).await, request)
        .await
        .unwrap()
    {
        ResponseResolved::Response(answer) => Ok(answer),
        ResponseResolved::Denied(reason) => Err(reason),
    }
}

/// The honest worker: it names the session the challenge was raised with.
async fn fetch_answer(pool: &PgPool, adapter: &str, job_id: Uuid, id: &str) -> Option<String> {
    let session_ref = session_of(pool, id).await;
    fetch_with_session(pool, adapter, job_id, id, session_ref)
        .await
        .ok()
}

// ----------------------------------------------------------------------------- waiting

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_challenge_parks_the_job_and_is_shown_only_to_its_user(pool: PgPool) {
    let (key, _, id) = waiting_job(&pool, json!({})).await;

    assert_eq!(job_status(&pool).await, "WAITING_FOR_OTP");
    assert_eq!(
        text(
            &pool,
            "SELECT disposition::text FROM adapter_execution_logs"
        )
        .await,
        "suspended"
    );
    assert_eq!(scalar(&pool, "SELECT count(*) FROM tracking_jobs WHERE execution_deadline_at IS NULL AND intervention_deadline_at > clock_timestamp()").await, 1);
    // The attempt is still open: nothing was retried or closed.
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM job_attempts WHERE closed_at IS NULL"
        )
        .await,
        1
    );

    let one = get(&app(&pool), &format!("/v1/interventions/{id}"), &key).await;
    assert_eq!(one.status, 200);
    assert_eq!(one.body["kind"], "otp");
    assert_eq!(one.body["status"], "pending");
    assert_eq!(one.body["tracking_number"], "DUMMY-OK-1");
    assert_eq!(one.body["image"], Value::Null);
    // Nothing internal leaks into the view.
    for hidden in ["session_ref", "job_id", "execution_id", "assigned_user_id"] {
        assert!(one.body.get(hidden).is_none(), "{hidden} is exposed");
    }

    let (_, stranger) = user(&pool, "stranger@example.com").await;
    assert_eq!(
        get(&app(&pool), &format!("/v1/interventions/{id}"), &stranger)
            .await
            .status,
        404
    );
    assert_eq!(answer(&pool, &stranger, &id, ANSWER).await.status, 404);
    let theirs = get(&app(&pool), "/v1/interventions", &stranger).await;
    assert_eq!(theirs.body["items"], json!([]));
    assert_eq!(job_status(&pool).await, "WAITING_FOR_OTP");
    assert_eq!(
        get(&app(&pool), "/v1/interventions", "no-key").await.status,
        401
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_captcha_challenge_carries_its_picture(pool: PgPool) {
    let extra = json!({ "kind": "captcha", "image": { "content_type": "image/png", "data": PNG } });
    let (key, _, id) = waiting_job(&pool, extra).await;
    assert_eq!(job_status(&pool).await, "WAITING_FOR_CAPTCHA");
    let one = get(&app(&pool), &format!("/v1/interventions/{id}"), &key).await;
    assert_eq!(one.body["has_image"], true);
    assert_eq!(
        one.body["image"],
        json!({ "content_type": "image/png", "data": PNG })
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_wait_is_never_longer_than_the_provider_allows(pool: PgPool) {
    exec(&pool, "UPDATE providers SET intervention_ttl_s = 60").await;
    waiting_job(&pool, json!({ "expires_in_s": 3000 })).await;
    let wait: f64 = sqlx::query_scalar(
        "SELECT extract(epoch FROM intervention_deadline_at - clock_timestamp())::float8 FROM tracking_jobs",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!((50.0..=60.0).contains(&wait), "{wait}");
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_repeated_or_late_challenge_changes_nothing(pool: PgPool) {
    let (_, job_id, _) = waiting_job(&pool, json!({})).await;
    let execution: Uuid = sqlx::query_scalar("SELECT execution_id FROM adapter_execution_logs")
        .fetch_one(&pool)
        .await
        .unwrap();
    // The same execution reports again (a re-published message with a new message ID).
    assert_eq!(
        handle(&pool, &challenge(job_id, execution, json!({}))).await,
        Handled::Processed
    );
    // Another execution of the same attempt asks while the job is already waiting.
    assert_eq!(
        handle(&pool, &challenge(job_id, Uuid::now_v7(), json!({}))).await,
        Handled::Processed
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM human_interventions").await,
        1
    );
    assert_eq!(
        scalar(&pool, "SELECT challenge_count::bigint FROM tracking_jobs").await,
        1
    );
    assert_eq!(
        text(&pool, "SELECT string_agg(disposition::text, ',' ORDER BY disposition::text) FROM adapter_execution_logs").await,
        "stale,suspended"
    );
    // A challenge for an attempt that does not exist is refused outright.
    let mut unknown: Value =
        serde_json::from_slice(&challenge(job_id, Uuid::now_v7(), json!({}))).unwrap();
    unknown["payload"]["attempt"] = json!(7);
    assert!(matches!(
        handle(&pool, unknown.to_string().as_bytes()).await,
        Handled::Rejected(_)
    ));
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_challenge_of_an_automatic_poll_goes_to_the_first_subscriber(pool: PgPool) {
    let (first, first_key) = user(&pool, "first@example.com").await;
    let reply = track(&app(&pool), &first_key, "DUMMY-OK-1").await;
    let job_id = Uuid::parse_str(reply.body["latest_job"]["id"].as_str().unwrap()).unwrap();
    let (_, second_key) = user(&pool, "second@example.com").await;
    track(&app(&pool), &second_key, "DUMMY-OK-1").await;
    exec(
        &pool,
        "UPDATE tracking_jobs SET initiated_by = NULL, trigger = 'poll'",
    )
    .await;

    handle(&pool, &challenge(job_id, Uuid::now_v7(), json!({}))).await;
    let assigned: Uuid = sqlx::query_scalar("SELECT assigned_user_id FROM human_interventions")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(assigned, first);
    assert_eq!(
        get(&app(&pool), "/v1/interventions", &second_key)
            .await
            .body["items"],
        json!([])
    );
}

// ----------------------------------------------------------------------------- answering

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_answer_requeues_the_job_and_only_its_worker_can_fetch_it(pool: PgPool) {
    let (key, job_id, id) = waiting_job(&pool, json!({})).await;
    let reply = answer(&pool, &key, &id, &format!("  {ANSWER} ")).await;
    assert_eq!(reply.status, 202, "{:?}", reply.body);

    assert_eq!(job_status(&pool).await, "QUEUED");
    assert_eq!(
        scalar(&pool, "SELECT attempt::bigint FROM tracking_jobs").await,
        1
    );
    assert_eq!(scalar(&pool, "SELECT count(*) FROM tracking_jobs WHERE queue_deadline_at > clock_timestamp() AND intervention_deadline_at IS NULL").await, 1);

    // job.resume names the intervention and the session; the answer is not in it.
    let message: Value =
        sqlx::query_scalar("SELECT payload FROM outbox_messages WHERE message_type = 'job.resume'")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        text(
            &pool,
            "SELECT routing_key FROM outbox_messages WHERE message_type = 'job.resume'"
        )
        .await,
        "job.resume.v1.dummy"
    );
    assert_eq!(message["payload"]["intervention_id"], id.as_str());
    assert_eq!(message["payload"]["attempt"], 1);
    assert!(message["payload"]["session_ref"].is_string());
    assert!(!message.to_string().contains(ANSWER));
    courier_engine::messaging::contracts::parse_job_track(message.to_string().as_bytes()).unwrap();

    // Nowhere in the database in readable form.
    let dump = text(
        &pool,
        "SELECT row_to_json(i)::text FROM human_interventions i",
    )
    .await;
    assert!(!dump.contains(ANSWER));
    assert_eq!(scalar(&pool, "SELECT count(*) FROM human_interventions WHERE status = 'answered' AND ciphertext IS NOT NULL").await, 1);

    // The view shows the state but never the answer; a second answer is refused.
    let one = get(&app(&pool), &format!("/v1/interventions/{id}"), &key).await;
    assert_eq!(one.body["status"], "answered");
    assert!(!one.body.to_string().contains(ANSWER));
    assert_eq!(answer(&pool, &key, &id, "111111").await.status, 409);
    assert_eq!(
        get(&app(&pool), "/v1/interventions", &key).await.body["items"],
        json!([])
    );

    // The job's own adapter may fetch it, repeatedly; another adapter may not.
    assert_eq!(
        fetch_answer(&pool, "dummy", job_id, &id).await.as_deref(),
        Some(ANSWER)
    );
    assert_eq!(
        fetch_answer(&pool, "dummy", job_id, &id).await.as_deref(),
        Some(ANSWER)
    );
    assert_eq!(fetch_answer(&pool, "dhl", job_id, &id).await, None);
    assert_eq!(scalar(&pool, "SELECT count(*) FROM audit_log WHERE action = 'intervention.resolve' AND outcome = 'allowed'").await, 2);
    assert_eq!(
        text(&pool, "SELECT reason FROM audit_log WHERE action = 'intervention.resolve' AND outcome = 'denied'").await,
        "job_belongs_to_another_adapter"
    );
    assert!(
        !text(
            &pool,
            "SELECT string_agg(row_to_json(a)::text, ' ') FROM audit_log a"
        )
        .await
        .contains(ANSWER)
    );
}

/// The answer is bound to the session its challenge was raised with: the right worker, the
/// right job and the right attempt are not enough without the right session.
#[sqlx::test(migrator = "MIGRATOR")]
async fn an_answer_is_handed_out_only_for_the_session_of_its_challenge(pool: PgPool) {
    let (key, job_id, id) = waiting_job(&pool, json!({})).await;
    answer(&pool, &key, &id, ANSWER).await;
    let own = session_of(&pool, &id).await;

    // Any other session reference is refused, whoever asks, and the refusal is recorded.
    let stranger = Uuid::now_v7();
    assert_eq!(
        fetch_with_session(&pool, "dummy", job_id, &id, stranger).await,
        Err("session_mismatch")
    );
    assert_eq!(
        text(
            &pool,
            "SELECT reason FROM audit_log WHERE action = 'intervention.resolve' AND outcome = 'denied'"
        )
        .await,
        "session_mismatch"
    );
    assert_eq!(scalar(&pool, "SELECT count(*) FROM audit_log WHERE action = 'intervention.resolve' AND outcome = 'allowed'").await, 0);
    // Nothing changed: the answer is still stored, the job still waits for its worker.
    assert_eq!(job_status(&pool).await, "QUEUED");
    assert_eq!(scalar(&pool, "SELECT count(*) FROM human_interventions WHERE status = 'answered' AND ciphertext IS NOT NULL").await, 1);

    // The challenge's own session still works afterwards.
    assert_eq!(
        fetch_with_session(&pool, "dummy", job_id, &id, own).await,
        Ok(ANSWER.to_string())
    );
    // The other checks come first: a foreign adapter learns nothing about sessions.
    assert_eq!(
        fetch_with_session(&pool, "dhl", job_id, &id, own).await,
        Err("job_belongs_to_another_adapter")
    );
    // Neither the answer nor anything secret is in the audit trail.
    assert!(
        !text(
            &pool,
            "SELECT string_agg(row_to_json(a)::text, ' ') FROM audit_log a"
        )
        .await
        .contains(ANSWER)
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_internal_api_hands_out_the_answer_only_with_a_worker_token(pool: PgPool) {
    let (key, job_id, id) = waiting_job(&pool, json!({})).await;
    answer(&pool, &key, &id, ANSWER).await;
    let internal = api::internal::router(state(&pool));
    let token = worker_tokens::issue(&pool, "dummy", Some("test"))
        .await
        .unwrap()
        .unwrap();
    let session_ref = session_of(&pool, &id).await;
    let body = json!({ "intervention_id": id, "session_ref": session_ref, "job_id": job_id, "attempt": 1 });
    let path = "/internal/v1/interventions/resolve";

    let ok = call(
        &internal,
        Method::POST,
        path,
        Some(&token),
        Some(body.clone()),
    )
    .await;
    assert_eq!(ok.status, 200);
    assert_eq!(ok.body, json!({ "response": ANSWER }));
    assert_eq!(ok.headers["cache-control"], "no-store");

    assert_eq!(
        call(
            &internal,
            Method::POST,
            path,
            Some(&key),
            Some(body.clone())
        )
        .await
        .status,
        401
    );
    let wrong_attempt = json!({ "intervention_id": id, "session_ref": session_ref, "job_id": job_id, "attempt": 2 });
    let denied = call(
        &internal,
        Method::POST,
        path,
        Some(&token),
        Some(wrong_attempt),
    )
    .await;
    assert_eq!(denied.status, 403);
    assert_eq!(denied.body["error"]["code"], "resolve_denied");
    // Another session than the challenge's own: the same refusal, and nothing of the answer.
    let other_session = json!({ "intervention_id": id, "session_ref": Uuid::now_v7(), "job_id": job_id, "attempt": 1 });
    let denied = call(
        &internal,
        Method::POST,
        path,
        Some(&token),
        Some(other_session),
    )
    .await;
    assert_eq!(denied.status, 403);
    assert_eq!(denied.body["error"]["code"], "resolve_denied");
    assert!(!denied.body.to_string().contains(ANSWER));
    // A request that does not name its session at all is not a valid request.
    let unnamed = json!({ "intervention_id": id, "job_id": job_id, "attempt": 1 });
    let refused = call(&internal, Method::POST, path, Some(&token), Some(unnamed)).await;
    assert!(
        refused.status.is_client_error(),
        "status {}",
        refused.status
    );
    assert!(!refused.body.to_string().contains(ANSWER));
    // The public API has no such route.
    assert_eq!(
        call(&app(&pool), Method::POST, path, Some(&key), Some(body))
            .await
            .status,
        404
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_lookup_completes_after_the_answer_and_the_answer_is_deleted(pool: PgPool) {
    let (key, job_id, id) = waiting_job(&pool, json!({})).await;
    answer(&pool, &key, &id, ANSWER).await;
    let resumed = Uuid::now_v7();
    handle(&pool, &started(job_id, resumed)).await;
    assert_eq!(job_status(&pool).await, "PROCESSING");
    assert_eq!(
        fetch_answer(&pool, "dummy", job_id, &id).await.as_deref(),
        Some(ANSWER)
    );
    handle(&pool, &delivered(job_id, resumed)).await;

    assert_eq!(job_status(&pool).await, "COMPLETED");
    assert_eq!(
        text(&pool, "SELECT current_status::text FROM shipments").await,
        "DELIVERED"
    );
    assert_eq!(scalar(&pool, "SELECT count(*) FROM job_attempts").await, 1);
    assert_eq!(
        scalar(&pool, "SELECT execution_count::bigint FROM job_attempts").await,
        2
    );
    assert_eq!(
        scalar(&pool, "SELECT count(*) FROM human_interventions WHERE status = 'completed' AND ciphertext IS NULL AND wrapped_key IS NULL").await,
        1
    );
    assert_eq!(fetch_answer(&pool, "dummy", job_id, &id).await, None);
    assert_eq!(answer(&pool, &key, &id, ANSWER).await.status, 409);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_answer_that_cannot_be_right_is_refused_without_bothering_the_provider(pool: PgPool) {
    let (key, _, id) = waiting_job(&pool, json!({})).await;
    for wrong in ["12345", "1234567", "12a456", "", "      "] {
        let reply = answer(&pool, &key, &id, wrong).await;
        assert_eq!(reply.status, 422, "{wrong:?}");
        assert_eq!(reply.body["error"]["details"]["field"], "response");
    }
    assert_eq!(job_status(&pool).await, "WAITING_FOR_OTP");
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM outbox_messages WHERE message_type = 'job.resume'"
        )
        .await,
        0
    );
    // After five of those the challenge stops accepting answers, even a well-formed one.
    let reply = answer(&pool, &key, &id, ANSWER).await;
    assert_eq!(reply.status, 410);
    assert_eq!(reply.body["error"]["code"], "too_many_invalid_answers");
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn answering_needs_the_master_key(pool: PgPool) {
    let (key, _, id) = waiting_job(&pool, json!({})).await;
    let reply = call(
        &common::app(&pool),
        Method::POST,
        &format!("/v1/interventions/{id}/response"),
        Some(&key),
        Some(json!({ "response": ANSWER })),
    )
    .await;
    assert_eq!(reply.status, 503);
    assert_eq!(job_status(&pool).await, "WAITING_FOR_OTP");
}

// ----------------------------------------------------------------------------- wrong answers, limits

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_refused_answer_leads_to_a_new_challenge_until_the_limit(pool: PgPool) {
    let (key, job_id, mut id) = waiting_job(&pool, json!({})).await;
    // Provider default: three challenges per job.
    for round in 2..=3 {
        assert_eq!(answer(&pool, &key, &id, "000000").await.status, 202);
        let execution = Uuid::now_v7();
        handle(&pool, &started(job_id, execution)).await;
        handle(
            &pool,
            &challenge(
                job_id,
                execution,
                json!({ "prompt": "That code was not accepted. Enter the new code." }),
            ),
        )
        .await;
        assert_eq!(job_status(&pool).await, "WAITING_FOR_OTP");
        let list = get(&app(&pool), "/v1/interventions", &key).await;
        assert_eq!(list.body["items"].as_array().unwrap().len(), 1);
        let next = list.body["items"][0]["id"].as_str().unwrap().to_string();
        assert_ne!(next, id);
        id = next;
        assert_eq!(
            scalar(
                &pool,
                "SELECT max(sequence)::bigint FROM human_interventions"
            )
            .await,
            round
        );
    }
    assert_eq!(scalar(&pool, "SELECT count(*) FROM human_interventions WHERE status = 'rejected' AND ciphertext IS NULL").await, 2);

    assert_eq!(answer(&pool, &key, &id, "000000").await.status, 202);
    let execution = Uuid::now_v7();
    handle(&pool, &started(job_id, execution)).await;
    handle(&pool, &challenge(job_id, execution, json!({}))).await;

    assert_eq!(job_status(&pool).await, "FAILED");
    assert_eq!(
        text(&pool, "SELECT last_error_class::text FROM tracking_jobs").await,
        "CHALLENGE_LIMIT_EXCEEDED"
    );
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM human_interventions WHERE status IN ('pending', 'answered')"
        )
        .await,
        0
    );
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM human_interventions WHERE ciphertext IS NOT NULL"
        )
        .await,
        0
    );
    assert_eq!(
        get(&app(&pool), "/v1/interventions", &key).await.body["items"],
        json!([])
    );
}

// ----------------------------------------------------------------------------- expiry

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_unanswered_challenge_expires_the_job(pool: PgPool) {
    let (key, _, id) = waiting_job(&pool, json!({})).await;
    // Not yet due: the reaper leaves it alone.
    assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 0);
    assert_eq!(job_status(&pool).await, "WAITING_FOR_OTP");

    exec(
        &pool,
        "UPDATE human_interventions SET expires_at = clock_timestamp() - interval '1 second'",
    )
    .await;
    // Overdue but not yet reaped: already shown as expired and no longer answerable.
    assert_eq!(
        get(&app(&pool), &format!("/v1/interventions/{id}"), &key)
            .await
            .body["status"],
        "expired"
    );
    assert_eq!(answer(&pool, &key, &id, ANSWER).await.status, 410);

    exec(&pool, "UPDATE tracking_jobs SET intervention_deadline_at = clock_timestamp() - interval '1 second'").await;
    assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 1);
    assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 0);

    assert_eq!(job_status(&pool).await, "EXPIRED");
    assert_eq!(
        text(&pool, "SELECT last_error_class::text FROM tracking_jobs").await,
        "INTERVENTION_EXPIRED"
    );
    assert_eq!(text(&pool, "SELECT error_class::text || '/' || closed_by::text FROM job_attempts WHERE closed_at IS NOT NULL").await, "INTERVENTION_EXPIRED/reaper");
    assert_eq!(
        text(&pool, "SELECT status FROM human_interventions").await,
        "expired"
    );
    assert_eq!(
        scalar(
            &pool,
            "SELECT count(*) FROM outbox_messages WHERE message_type = 'job.resume'"
        )
        .await,
        0
    );
    let reply = answer(&pool, &key, &id, ANSWER).await;
    assert_eq!(reply.status, 410);
    assert_eq!(reply.body["error"]["code"], "intervention_expired");

    // The shipment is free again: a refresh starts a new job.
    let request: String = text(&pool, "SELECT id::text FROM tracking_requests").await;
    let refresh = call(
        &app(&pool),
        Method::POST,
        &format!("/v1/tracking-requests/{request}/refresh"),
        Some(&key),
        None,
    )
    .await;
    assert_eq!(refresh.status, 202, "{:?}", refresh.body);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_resume_nobody_picks_up_is_closed_by_the_queue_deadline_and_its_answer_deleted(
    pool: PgPool,
) {
    let (key, job_id, id) = waiting_job(&pool, json!({})).await;
    answer(&pool, &key, &id, ANSWER).await;
    exec(
        &pool,
        "UPDATE tracking_jobs SET queue_deadline_at = clock_timestamp() - interval '1 second'",
    )
    .await;
    assert_eq!(reap_expired(&pool, 100, POLICY).await.unwrap(), 1);
    assert_eq!(job_status(&pool).await, "RETRYING");
    assert_eq!(
        text(&pool, "SELECT last_error_class::text FROM tracking_jobs").await,
        "QUEUE_WAIT_EXCEEDED"
    );
    assert_eq!(scalar(&pool, "SELECT count(*) FROM human_interventions WHERE status = 'closed' AND ciphertext IS NULL").await, 1);
    assert_eq!(fetch_answer(&pool, "dummy", job_id, &id).await, None);
}
