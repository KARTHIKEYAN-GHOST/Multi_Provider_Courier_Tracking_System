//! Phase 9: failure samples, repair tasks and the repair service's internal endpoints.

mod common;

use axum::{Router, http::Method};
use common::{app, call, track, user};
use courier_engine::{
    api,
    config::Config,
    db,
    services::{
        close_attempt::RetryPolicy,
        events::handle_event,
        polling::poll_due,
        repairs::{self, Finish, NewProvider, RequestError},
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
const TOKEN: &str = "repair-token-0123456789-0123456789-abcdef";
const PAGE: &str = "<html><body><div class='new'>layout changed</div></body></html>";

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

/// A failed result as a worker sends it, optionally with the answer it could not read.
async fn fail(pool: &PgPool, job_id: Uuid, version: &str, class: &str, evidence: Option<&str>) {
    let mut payload = json!({
        "job_id": job_id, "attempt": 1, "execution_id": Uuid::now_v7(), "delivery_count": 0,
        "worker_id": "dummy-test", "adapter_key": "dummy", "adapter_version": version,
        "started_at": "2026-11-24T10:00:00.100Z", "finished_at": "2026-11-24T10:00:00.300Z",
        "outcome": "FAILURE",
        "error": { "class": class, "message": "no history table", "retryable": false, "retry_after_ms": null },
    });
    if let Some(body) = evidence {
        payload["raw_response"] = json!({ "content_type": "text/html", "body": body });
    }
    let body = json!({
        "message_id": Uuid::now_v7(), "type": "job.result", "schema_version": "1.0",
        "occurred_at": "2026-11-24T10:00:00Z", "correlation_id": Uuid::now_v7(), "causation_id": null,
        "producer": "adapter-dummy@0.1.0", "payload": payload,
    })
    .to_string();
    handle_event(pool, body.as_bytes(), POLICY).await.unwrap();
}

/// `count` lookups that all end with an unreadable answer.
async fn drift(pool: &PgPool, prefix: &str, version: &str, count: usize) {
    for i in 0..count {
        let job_id = new_job(pool, &format!("{prefix}-{i}")).await;
        fail(pool, job_id, version, "SCHEMA_DRIFT", Some(PAGE)).await;
    }
}

async fn samples(pool: &PgPool) -> i64 {
    sqlx::query_scalar("SELECT count(*) FROM adapter_failure_samples")
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn tasks(pool: &PgPool) -> Vec<(String, String, String, Option<String>)> {
    sqlx::query_as(
        "SELECT kind, adapter_key, status, adapter_version FROM repair_tasks ORDER BY created_at, id",
    )
    .fetch_all(pool)
    .await
    .unwrap()
}

fn internal(pool: &PgPool, token: Option<&str>) -> Router {
    let mut config = Config::for_tests();
    config.engine_repair_token = token.map(str::to_string);
    api::internal::router(AppState::new(pool.clone(), config))
}

type Change = Box<dyn Fn(&mut NewProvider)>;

fn spec() -> NewProvider {
    NewProvider {
        code: "bluedart".into(),
        name: "Blue Dart".into(),
        method: "api".into(),
        hosts: vec!["api.bluedart.example".into()],
        needs_credential: true,
        notes: "GET /track?awb=<number> returns JSON with a scans list".into(),
        sample: Some("{\"scans\": []}".into()),
    }
}

// ----------------------------------------------------------------------------- samples and tasks

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_unreadable_answer_is_kept_and_the_third_lookup_opens_a_task(pool: PgPool) {
    drift(&pool, "DUMMY-A", "0.1.0", 2).await;
    assert_eq!(samples(&pool).await, 2);
    assert!(
        tasks(&pool).await.is_empty(),
        "two odd shipments are not a broken adapter"
    );

    drift(&pool, "DUMMY-B", "0.1.0", 1).await;
    assert_eq!(
        tasks(&pool).await,
        [(
            "repair".to_string(),
            "dummy".to_string(),
            "pending".to_string(),
            Some("0.1.0".to_string())
        )]
    );
    let (class, number, body): (String, String, Value) = sqlx::query_as(
        "SELECT error_class, tracking_number, body FROM adapter_failure_samples ORDER BY created_at DESC LIMIT 1",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        (class.as_str(), number.as_str(), body),
        ("SCHEMA_DRIFT", "DUMMY-B-0", json!(PAGE))
    );

    // More failures do not pile up tasks, and old samples are dropped. (A task that has been
    // open for longer than the pause no longer holds lookups back.)
    sqlx::query("UPDATE repair_tasks SET created_at = created_at - interval '7 hours'")
        .execute(&pool)
        .await
        .unwrap();
    drift(&pool, "DUMMY-C", "0.1.0", 12).await;
    assert_eq!(tasks(&pool).await.len(), 1);
    assert_eq!(samples(&pool).await, 10);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn other_failures_and_failures_without_the_answer_leave_no_sample(pool: PgPool) {
    for (i, (class, evidence)) in [
        ("HTTP_5XX", Some(PAGE)),
        ("NETWORK_TIMEOUT", Some(PAGE)),
        ("SCHEMA_DRIFT", None),
        ("PARSE_ERROR", None),
    ]
    .into_iter()
    .enumerate()
    {
        let job_id = new_job(&pool, &format!("DUMMY-X-{i}")).await;
        fail(&pool, job_id, "0.1.0", class, evidence).await;
    }
    assert_eq!(samples(&pool).await, 0);
    assert!(tasks(&pool).await.is_empty());
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_published_repair_is_not_reopened_by_workers_still_running_the_old_version(pool: PgPool) {
    drift(&pool, "DUMMY-A", "0.1.0", 3).await;
    let task = repairs::claim(&pool).await.unwrap().unwrap();
    assert_eq!(
        (task.kind.as_str(), task.adapter_key.as_str(), task.attempts),
        ("repair", "dummy", 1)
    );
    assert_eq!(task.samples.len(), 3);
    assert!(
        repairs::claim(&pool).await.unwrap().is_none(),
        "a running task is not handed out twice"
    );

    let done = Finish::Published {
        commit_sha: "0123abc".into(),
        summary: "reads the new layout".into(),
    };
    assert!(repairs::finish(&pool, task.id, &done).await.unwrap());
    assert!(
        !repairs::finish(&pool, task.id, &done).await.unwrap(),
        "only once"
    );

    drift(&pool, "DUMMY-B", "0.1.0", 4).await;
    assert_eq!(
        tasks(&pool).await.len(),
        1,
        "same version: the fix is already published"
    );
    drift(&pool, "DUMMY-C", "0.1.1", 3).await;
    let all = tasks(&pool).await;
    assert_eq!(all.len(), 2, "the repaired version fails too: a new task");
    assert_eq!(all[1].3.as_deref(), Some("0.1.1"));
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_failed_repair_is_retried_only_after_a_pause_and_a_silent_claim_is_handed_out_again(
    pool: PgPool,
) {
    drift(&pool, "DUMMY-A", "0.1.0", 3).await;
    let task = repairs::claim(&pool).await.unwrap().unwrap();
    let failed = Finish::Failed {
        reason: "tests did not pass".into(),
    };
    assert!(repairs::finish(&pool, task.id, &failed).await.unwrap());
    drift(&pool, "DUMMY-B", "0.1.0", 3).await;
    assert_eq!(tasks(&pool).await.len(), 1, "not again right away");

    sqlx::query("UPDATE repair_tasks SET finished_at = now() - interval '7 hours'")
        .execute(&pool)
        .await
        .unwrap();
    drift(&pool, "DUMMY-C", "0.1.0", 1).await;
    assert_eq!(tasks(&pool).await.len(), 2);

    let second = repairs::claim(&pool).await.unwrap().unwrap();
    sqlx::query("UPDATE repair_tasks SET claimed_at = now() - interval '3 hours' WHERE id = $1")
        .bind(second.id)
        .execute(&pool)
        .await
        .unwrap();
    let again = repairs::claim(&pool).await.unwrap().unwrap();
    assert_eq!((again.id, again.attempts), (second.id, 2));
}

// ----------------------------------------------------------------------------- operator requests

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_operator_can_ask_for_an_adapter_for_a_new_provider(pool: PgPool) {
    let id = repairs::request_new_provider(&pool, &spec()).await.unwrap();
    assert!(matches!(
        repairs::request_new_provider(&pool, &spec()).await,
        Err(RequestError::AlreadyOpen)
    ));
    let task = repairs::claim(&pool).await.unwrap().unwrap();
    assert_eq!(
        (task.id, task.kind.as_str(), task.adapter_key.as_str()),
        (id, "new_provider", "bluedart")
    );
    assert_eq!(task.request["hosts"], json!(["api.bluedart.example"]));
    assert_eq!(task.request["needs_credential"], json!(true));
    assert!(task.samples.is_empty());
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_request_that_could_harm_the_generated_files_is_refused(pool: PgPool) {
    let bad: Vec<Change> = vec![
        Box::new(|s| s.code = "dhl".into()),
        Box::new(|s| s.code = "Blue-Dart".into()),
        Box::new(|s| s.code = "../etc".into()),
        Box::new(|s| s.name = "Blue'); DROP TABLE providers; --".into()),
        Box::new(|s| s.method = "browser".into()),
        Box::new(|s| s.hosts = vec![]),
        Box::new(|s| s.hosts = vec!["https://api.example.com/x".into()]),
        Box::new(|s| s.hosts = vec!["localhost".into()]),
        Box::new(|s| s.notes = " ".into()),
        Box::new(|s| s.sample = Some("x".repeat(100_001))),
    ];
    for change in bad {
        let mut request = spec();
        change(&mut request);
        assert!(
            matches!(
                repairs::request_new_provider(&pool, &request).await,
                Err(RequestError::Invalid(_))
            ),
            "{request:?}"
        );
    }
    assert!(tasks(&pool).await.is_empty());
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_repair_can_be_requested_by_hand_once_there_is_something_to_repair_from(pool: PgPool) {
    assert!(matches!(
        repairs::request_repair(&pool, "dummy").await,
        Err(RequestError::Invalid(_))
    ));
    drift(&pool, "DUMMY-A", "0.1.0", 1).await;
    repairs::request_repair(&pool, "dummy").await.unwrap();
    assert_eq!(
        tasks(&pool).await,
        [(
            "repair".to_string(),
            "dummy".to_string(),
            "pending".to_string(),
            Some("0.1.0".to_string())
        )]
    );
    assert_eq!(repairs::list(&pool, 10).await.unwrap().len(), 1);
}

// ----------------------------------------------------------------------------- internal endpoints

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_repair_endpoints_accept_the_repair_token_and_nothing_else(pool: PgPool) {
    drift(&pool, "DUMMY-A", "0.1.0", 3).await;
    let worker = worker_tokens::issue(&pool, "dummy", Some("test"))
        .await
        .unwrap()
        .unwrap();
    let claim = "/internal/v1/repairs/claim";

    let unset = internal(&pool, None);
    assert_eq!(
        call(&unset, Method::POST, claim, Some(TOKEN), None)
            .await
            .status,
        401
    );
    let short = internal(&pool, Some("short"));
    assert_eq!(
        call(&short, Method::POST, claim, Some("short"), None)
            .await
            .status,
        401
    );

    let api = internal(&pool, Some(TOKEN));
    for wrong in [
        None,
        Some("wrong-token-0123456789-0123456789-abcdef"),
        Some(worker.as_str()),
    ] {
        assert_eq!(
            call(&api, Method::POST, claim, wrong, None).await.status,
            401,
            "{wrong:?}"
        );
    }
    // The repair token opens nothing else on the internal API.
    let body = json!({ "credential_ref": Uuid::now_v7(), "job_id": Uuid::now_v7(), "attempt": 1 });
    let resolve = call(
        &api,
        Method::POST,
        "/internal/v1/credentials/resolve",
        Some(TOKEN),
        Some(body),
    )
    .await;
    assert_eq!(resolve.status, 401);
    assert_eq!(tasks(&pool).await[0].2, "pending", "nobody has claimed it");

    let claimed = call(&api, Method::POST, claim, Some(TOKEN), None).await;
    assert_eq!(claimed.status, 200, "{:?}", claimed.body);
    assert_eq!(claimed.body["adapter_key"], "dummy");
    assert_eq!(claimed.body["samples"][0]["body"], json!(PAGE));
    assert_eq!(
        call(&api, Method::POST, claim, Some(TOKEN), None)
            .await
            .status,
        204
    );

    let path = format!(
        "/internal/v1/repairs/{}/result",
        claimed.body["id"].as_str().unwrap()
    );
    let bad = call(
        &api,
        Method::POST,
        &path,
        Some(TOKEN),
        Some(json!({ "status": "merged" })),
    )
    .await;
    assert!(bad.status.is_client_error());
    let done = json!({ "status": "published", "commit_sha": "abc123", "summary": "fixed" });
    assert_eq!(
        call(&api, Method::POST, &path, Some(TOKEN), Some(done.clone()))
            .await
            .status,
        204
    );
    assert_eq!(
        call(&api, Method::POST, &path, Some(TOKEN), Some(done))
            .await
            .status,
        404
    );

    let (status, sha): (String, Option<String>) =
        sqlx::query_as("SELECT status, commit_sha FROM repair_tasks")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        (status.as_str(), sha.as_deref()),
        ("published", Some("abc123"))
    );
    let actions: Vec<String> =
        sqlx::query_scalar("SELECT action FROM audit_log WHERE action LIKE 'repair.%' ORDER BY id")
            .fetch_all(&pool)
            .await
            .unwrap();
    assert_eq!(
        actions,
        ["repair.opened", "repair.claimed", "repair.finished"]
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_provider_under_repair_starts_no_lookup_until_the_repair_has_ended(pool: PgPool) {
    drift(&pool, "DUMMY-P", "0.1.0", 3).await;
    assert_eq!(tasks(&pool).await[0].2, "pending");
    let jobs_before: i64 = sqlx::query_scalar("SELECT count(*) FROM tracking_jobs")
        .fetch_one(&pool)
        .await
        .unwrap();

    // A new request is accepted, but its lookup is held back.
    let (_, key) = user(&pool, "later@example.com").await;
    let reply = track(&app(&pool), &key, "DUMMY-OK-LATER").await;
    assert_eq!(reply.status, 201, "{:?}", reply.body);
    assert_eq!(reply.body["latest_job"], Value::Null);
    let request_id = reply.body["id"].as_str().unwrap().to_string();
    let waits: i64 = sqlx::query_scalar(
        "SELECT (extract(epoch FROM next_poll_at - clock_timestamp()))::bigint FROM shipments
         WHERE tracking_number = 'DUMMY-OK-LATER'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(
        (200..=300).contains(&waits),
        "asked again in five minutes, not {waits} s"
    );

    // A manual refresh says why nothing starts.
    let refresh = call(
        &app(&pool),
        Method::POST,
        &format!("/v1/tracking-requests/{request_id}/refresh"),
        Some(&key),
        None,
    )
    .await;
    assert_eq!(refresh.status, 409);
    assert_eq!(refresh.body["error"]["code"], "provider_under_repair");

    // The scheduler looks again, finds the repair still open and waits on.
    let due = "UPDATE shipments SET next_poll_at = clock_timestamp() - interval '1 second'
               WHERE tracking_number = 'DUMMY-OK-LATER'";
    sqlx::query(due).execute(&pool).await.unwrap();
    let stats = poll_due(&pool, 10).await.unwrap();
    assert_eq!((stats.started, stats.postponed), (0, 1));
    let jobs_now: i64 = sqlx::query_scalar("SELECT count(*) FROM tracking_jobs")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(jobs_now, jobs_before, "no lookup was started meanwhile");

    // The repair ends: the held lookup starts by itself.
    let task = repairs::claim(&pool).await.unwrap().expect("the open task");
    let done = Finish::Published {
        commit_sha: "0123abc".into(),
        summary: "reads the new layout".into(),
    };
    assert!(repairs::finish(&pool, task.id, &done).await.unwrap());
    sqlx::query(due).execute(&pool).await.unwrap();
    let stats = poll_due(&pool, 10).await.unwrap();
    assert_eq!((stats.started, stats.postponed), (1, 0));
    let (trigger, status): (String, String) = sqlx::query_as(
        "SELECT j.trigger::text, j.status::text FROM tracking_jobs j
         JOIN shipments s ON s.id = j.shipment_id WHERE s.tracking_number = 'DUMMY-OK-LATER'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!((trigger.as_str(), status.as_str()), ("poll", "QUEUED"));
    let refresh = call(
        &app(&pool),
        Method::POST,
        &format!("/v1/tracking-requests/{request_id}/refresh"),
        Some(&key),
        None,
    )
    .await;
    assert_eq!(refresh.status, 200, "{:?}", refresh.body);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_task_for_a_new_provider_or_another_adapter_pauses_nothing(pool: PgPool) {
    repairs::request_new_provider(&pool, &spec()).await.unwrap();
    let (_, key) = user(&pool, "a@example.com").await;
    let reply = track(&app(&pool), &key, "DUMMY-OK-1").await;
    assert_eq!(reply.status, 201);
    assert_ne!(reply.body["latest_job"], Value::Null);
}
