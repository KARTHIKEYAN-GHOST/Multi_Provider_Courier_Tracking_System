//! Tracking-request API: data model, access control, validation and atomicity
//! (acceptance criteria 1, 7, 8, 9, 10, 14).

mod common;

use axum::{
    body::Body,
    http::{Method, Request, StatusCode, header},
};
use common::{add_event, app, call, complete_all_jobs, count, get, send, track, user};
use courier_engine::db;
use serde_json::{Value, json};
use sqlx::PgPool;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;

// ----------------------------------------------------------------------------- foundation

#[sqlx::test(migrator = "MIGRATOR")]
async fn migrations_are_a_no_op_when_applied_again(pool: PgPool) {
    // #[sqlx::test] has already applied every migration to this database.
    let seeded: i64 = sqlx::query_scalar("SELECT count(*) FROM providers")
        .fetch_one(&pool)
        .await
        .unwrap();
    db::MIGRATOR.run(&pool).await.unwrap();
    db::MIGRATOR.run(&pool).await.unwrap();

    let applied: i64 = sqlx::query_scalar("SELECT count(*) FROM _sqlx_migrations WHERE success")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(applied, db::MIGRATOR.iter().count() as i64);
    assert!(applied >= 9, "0009_integrity is part of the schema");
    let providers: i64 = sqlx::query_scalar("SELECT count(*) FROM providers")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(seeded >= 8, "dummy, dhl, demosite and five Indian couriers");
    assert_eq!(providers, seeded, "the seeds must not be applied twice");
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn health_endpoints_need_no_key(pool: PgPool) {
    let app = app(&pool);
    let live = call(&app, Method::GET, "/healthz", None, None).await;
    assert_eq!(live.status, StatusCode::OK);
    let ready = call(&app, Method::GET, "/readyz", None, None).await;
    assert_eq!(ready.status, StatusCode::OK);
    assert_eq!(ready.body["checks"]["database"], "ok");
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn every_response_carries_a_correlation_id(pool: PgPool) {
    let app = app(&pool);

    // Generated when the caller sends none — including on errors.
    let reply = call(&app, Method::GET, "/v1/providers", None, None).await;
    assert_eq!(reply.status, StatusCode::UNAUTHORIZED);
    let generated = reply.headers["x-correlation-id"].to_str().unwrap();
    assert!(uuid::Uuid::parse_str(generated).is_ok());

    // Echoed when the caller supplies a UUID.
    let supplied = "019355a0-0000-7000-8000-00000000c0de";
    let request = Request::builder()
        .uri("/healthz")
        .header("x-correlation-id", supplied)
        .body(Body::empty())
        .unwrap();
    assert_eq!(
        send(&app, request).await.headers["x-correlation-id"],
        supplied
    );

    // Replaced when the caller supplies something that is not a UUID.
    let request = Request::builder()
        .uri("/healthz")
        .header("x-correlation-id", "not-a-uuid\"; drop")
        .body(Body::empty())
        .unwrap();
    let replaced = send(&app, request).await;
    assert!(uuid::Uuid::parse_str(replaced.headers["x-correlation-id"].to_str().unwrap()).is_ok());
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn unknown_routes_return_the_standard_error_shape(pool: PgPool) {
    let reply = call(&app(&pool), Method::GET, "/nope", None, None).await;
    assert_eq!(reply.status, StatusCode::NOT_FOUND);
    assert_eq!(reply.body["error"]["code"], "not_found");
}

// ----------------------------------------------------------------------------- create

#[sqlx::test(migrator = "MIGRATOR")]
async fn creating_a_request_writes_shipment_subscription_job_attempt_and_outbox_together(
    pool: PgPool,
) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);

    let reply = call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(json!({ "provider_code": "dummy", "tracking_number": "  dummy-ok-1 ", "reference": "order 42" })),
    )
    .await;

    assert_eq!(reply.status, StatusCode::CREATED);
    assert_eq!(reply.body["provider_code"], "dummy");
    assert_eq!(
        reply.body["tracking_number"], "DUMMY-OK-1",
        "number is trimmed and upper-cased"
    );
    assert_eq!(reply.body["reference"], "order 42");
    assert_eq!(reply.body["shipment_status"], "UNKNOWN");
    assert_eq!(reply.body["latest_job"]["status"], "QUEUED");
    assert_eq!(reply.body["latest_job"]["attempt"], 1);
    assert_eq!(reply.body["latest_job"]["max_attempts"], 3);
    assert_eq!(reply.body["latest_job"]["trigger"], "initial");
    assert_eq!(reply.body["latest_job"]["execution_count"], 0);

    for table in [
        "shipments",
        "tracking_requests",
        "tracking_jobs",
        "job_attempts",
        "outbox_messages",
    ] {
        assert_eq!(count(&pool, table).await, 1, "{table}");
    }
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_outbox_holds_a_job_track_message_that_matches_the_job(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);

    let correlation = "019355a0-0000-7000-8000-00000000c0de";
    let request = Request::builder()
        .method(Method::POST)
        .uri("/v1/tracking-requests")
        .header(header::AUTHORIZATION, format!("Bearer {key}"))
        .header(header::CONTENT_TYPE, "application/json")
        .header("x-correlation-id", correlation)
        .body(Body::from(
            json!({ "provider_code": "dummy", "tracking_number": "DUMMY-OK-1" }).to_string(),
        ))
        .unwrap();
    let reply = send(&app, request).await;
    assert_eq!(reply.status, StatusCode::CREATED);
    let job_id = reply.body["latest_job"]["id"].as_str().unwrap();

    let (message_id, message_type, exchange, routing_key, status, payload): (
        uuid::Uuid,
        String,
        String,
        String,
        String,
        Value,
    ) = sqlx::query_as(
        "SELECT message_id, message_type, exchange, routing_key, status::text, payload FROM outbox_messages",
    )
    .fetch_one(&pool)
    .await
    .unwrap();

    assert_eq!(message_type, "job.track");
    assert_eq!(exchange, "courier.jobs");
    assert_eq!(routing_key, "job.track.v1.dummy");
    assert_eq!(status, "pending");

    assert_eq!(payload["message_id"], message_id.to_string());
    assert_eq!(payload["type"], "job.track");
    assert_eq!(payload["schema_version"], "1.0");
    assert_eq!(
        payload["correlation_id"], correlation,
        "the API correlation ID travels in the message"
    );
    assert_eq!(payload["payload"]["job_id"], job_id);
    assert_eq!(payload["payload"]["attempt"], 1);
    assert_eq!(payload["payload"]["adapter_key"], "dummy");
    assert_eq!(payload["payload"]["tracking_number"], "DUMMY-OK-1");
    assert_eq!(payload["payload"]["timeout_ms"], 10000);

    // The job row carries the same correlation ID and the same queue-wait deadline as the
    // message's expires_at; the execution deadline is not set until a worker starts.
    let (job_correlation, queue_deadline, execution_deadline): (
        uuid::Uuid,
        chrono::DateTime<chrono::Utc>,
        Option<chrono::DateTime<chrono::Utc>>,
    ) = sqlx::query_as(
        "SELECT correlation_id, queue_deadline_at, execution_deadline_at FROM tracking_jobs",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(job_correlation.to_string(), correlation);
    assert!(execution_deadline.is_none());
    let expires_at =
        chrono::DateTime::parse_from_rfc3339(payload["payload"]["expires_at"].as_str().unwrap())
            .unwrap();
    assert_eq!(
        expires_at.timestamp_micros(),
        queue_deadline.timestamp_micros()
    );
    let wait = queue_deadline - chrono::Utc::now();
    assert!(
        wait.num_seconds() > 890 && wait.num_seconds() <= 900,
        "queue-wait budget is 15 minutes"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn repeating_a_request_returns_the_same_subscription_and_no_second_job(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);

    let first = track(&app, &key, "DUMMY-OK-1").await;
    let second = track(&app, &key, "dummy-ok-1").await;

    assert_eq!(first.status, StatusCode::CREATED);
    assert_eq!(second.status, StatusCode::OK);
    assert_eq!(first.id(), second.id());
    assert_eq!(count(&pool, "tracking_requests").await, 1);
    assert_eq!(count(&pool, "tracking_jobs").await, 1);
    assert_eq!(count(&pool, "outbox_messages").await, 1);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_second_user_shares_the_shipment_and_its_job(pool: PgPool) {
    let (_, key_a) = user(&pool, "a@example.com").await;
    let (_, key_b) = user(&pool, "b@example.com").await;
    let app = app(&pool);

    let a = track(&app, &key_a, "DUMMY-OK-1").await;
    let b = track(&app, &key_b, "DUMMY-OK-1").await;

    assert_eq!(b.status, StatusCode::CREATED);
    assert_ne!(a.id(), b.id(), "each user has their own subscription");
    assert_eq!(
        a.body["latest_job"]["id"], b.body["latest_job"]["id"],
        "but one shared job"
    );
    assert_eq!(count(&pool, "shipments").await, 1);
    assert_eq!(count(&pool, "tracking_requests").await, 2);
    assert_eq!(count(&pool, "tracking_jobs").await, 1);
    assert_eq!(count(&pool, "outbox_messages").await, 1);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_failure_after_the_job_insert_rolls_everything_back(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);

    // Make the LAST write of the transaction (the outbox insert) fail for real.
    sqlx::query(
        "ALTER TABLE outbox_messages ADD CONSTRAINT injected_failure CHECK (false) NOT VALID",
    )
    .execute(&pool)
    .await
    .unwrap();

    let reply = track(&app, &key, "DUMMY-OK-1").await;
    assert_eq!(reply.status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(reply.body["error"]["code"], "internal_error");
    assert_eq!(
        reply.body["error"]["message"], "internal error",
        "no internals leak to the client"
    );

    for table in [
        "shipments",
        "tracking_requests",
        "tracking_jobs",
        "job_attempts",
        "outbox_messages",
    ] {
        assert_eq!(
            count(&pool, table).await,
            0,
            "{table} must be empty after rollback"
        );
    }

    // Once the fault is gone the same request succeeds normally.
    sqlx::query("ALTER TABLE outbox_messages DROP CONSTRAINT injected_failure")
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(
        track(&app, &key, "DUMMY-OK-1").await.status,
        StatusCode::CREATED
    );
}

// ----------------------------------------------------------------------------- validation

#[sqlx::test(migrator = "MIGRATOR")]
async fn invalid_input_is_rejected_with_422_and_writes_nothing(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);

    let bodies = [
        json!({ "provider_code": "dummy", "tracking_number": "" }),
        json!({ "provider_code": "dummy", "tracking_number": "   " }),
        json!({ "provider_code": "dummy", "tracking_number": "A".repeat(101) }),
        json!({ "provider_code": "dummy", "tracking_number": "AB 12" }),
        json!({ "provider_code": "dummy", "tracking_number": "AB'; DROP TABLE users;--" }),
        json!({ "provider_code": "dummy", "tracking_number": "-AB12" }),
        json!({ "provider_code": "dummy", "tracking_number": 12345 }),
        json!({ "provider_code": "dummy" }),
        json!({ "tracking_number": "DUMMY-OK-1" }),
        json!({ "provider_code": "Dummy Courier", "tracking_number": "DUMMY-OK-1" }),
        json!({ "provider_code": "", "tracking_number": "DUMMY-OK-1" }),
        json!({ "provider_code": "dummy", "tracking_number": "DUMMY-OK-1", "reference": "r".repeat(201) }),
        json!(["not", "an", "object"]),
    ];
    for body in bodies {
        let reply = call(
            &app,
            Method::POST,
            "/v1/tracking-requests",
            Some(&key),
            Some(body.clone()),
        )
        .await;
        assert_eq!(reply.status, StatusCode::UNPROCESSABLE_ENTITY, "{body}");
        assert_eq!(reply.body["error"]["code"], "validation_failed", "{body}");
    }

    // Body that is not JSON at all.
    let request = Request::builder()
        .method(Method::POST)
        .uri("/v1/tracking-requests")
        .header(header::AUTHORIZATION, format!("Bearer {key}"))
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from("{ this is not json"))
        .unwrap();
    let reply = send(&app, request).await;
    assert_eq!(reply.status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(reply.body["error"]["code"], "validation_failed");

    assert_eq!(count(&pool, "shipments").await, 0);
    assert_eq!(count(&pool, "tracking_requests").await, 0);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_longest_allowed_tracking_number_is_accepted(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let reply = track(&app(&pool), &key, &"A".repeat(100)).await;
    assert_eq!(reply.status, StatusCode::CREATED);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_unknown_or_disabled_provider_is_404(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);
    let body = json!({ "provider_code": "nosuchcourier", "tracking_number": "X1" });
    let reply = call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(body),
    )
    .await;
    assert_eq!(reply.status, StatusCode::NOT_FOUND);

    sqlx::query("UPDATE providers SET enabled = false WHERE code = 'dummy'")
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(
        track(&app, &key, "DUMMY-OK-1").await.status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(count(&pool, "shipments").await, 0);
}

// ----------------------------------------------------------------------------- access control

#[sqlx::test(migrator = "MIGRATOR")]
async fn another_users_request_id_is_404_on_every_endpoint(pool: PgPool) {
    let (_, key_a) = user(&pool, "a@example.com").await;
    let (_, key_b) = user(&pool, "b@example.com").await;
    let app = app(&pool);

    let a = track(&app, &key_a, "DUMMY-OK-1").await;
    let b = track(&app, &key_b, "DUMMY-OTHER-2").await;
    let a_id = a.id();

    let attempts = [
        (Method::GET, format!("/v1/tracking-requests/{a_id}")),
        (Method::GET, format!("/v1/tracking-requests/{a_id}/events")),
        (Method::GET, format!("/v1/tracking-requests/{a_id}/jobs")),
        (
            Method::POST,
            format!("/v1/tracking-requests/{a_id}/refresh"),
        ),
        (Method::DELETE, format!("/v1/tracking-requests/{a_id}")),
    ];
    for (method, path) in attempts {
        let reply = call(&app, method.clone(), &path, Some(&key_b), None).await;
        assert_eq!(
            reply.status,
            StatusCode::NOT_FOUND,
            "{method} {path} as user B"
        );
        assert_eq!(reply.body["error"]["code"], "not_found");
    }

    // A response for someone else's ID is identical to one for an ID that does not exist.
    let missing = get(
        &app,
        "/v1/tracking-requests/019355a0-1c2d-7e3f-8a4b-5c6d7e8f9a01",
        &key_b,
    )
    .await;
    let foreign = get(&app, &format!("/v1/tracking-requests/{a_id}"), &key_b).await;
    assert_eq!(missing.status, foreign.status);
    assert_eq!(missing.body, foreign.body);

    // B's list contains only B's request.
    let list = get(&app, "/v1/tracking-requests", &key_b).await;
    let ids: Vec<&str> = list.body["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, vec![b.id().as_str()]);

    // B's failed DELETE did not cancel A's subscription.
    assert_eq!(
        get(&app, &format!("/v1/tracking-requests/{a_id}"), &key_a)
            .await
            .status,
        StatusCode::OK
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_id_that_is_not_a_uuid_is_404(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let reply = get(&app(&pool), "/v1/tracking-requests/123", &key).await;
    assert_eq!(reply.status, StatusCode::NOT_FOUND);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn subscribers_see_identical_events_and_cancelling_affects_only_the_caller(pool: PgPool) {
    let (_, key_a) = user(&pool, "a@example.com").await;
    let (_, key_b) = user(&pool, "b@example.com").await;
    let app = app(&pool);

    let a_id = track(&app, &key_a, "DUMMY-OK-1").await.id();
    let b_id = track(&app, &key_b, "DUMMY-OK-1").await.id();
    add_event(&pool, "DUMMY-OK-1", "PKG_CREATED", "CREATED").await;
    add_event(&pool, "DUMMY-OK-1", "PKG_IN_TRANSIT", "IN_TRANSIT").await;

    let a_events = get(
        &app,
        &format!("/v1/tracking-requests/{a_id}/events"),
        &key_a,
    )
    .await;
    let b_events = get(
        &app,
        &format!("/v1/tracking-requests/{b_id}/events"),
        &key_b,
    )
    .await;
    assert_eq!(a_events.status, StatusCode::OK);
    assert_eq!(a_events.body["items"].as_array().unwrap().len(), 2);
    assert_eq!(a_events.body, b_events.body);
    assert_eq!(a_events.body["items"][0]["raw_status"], "PKG_CREATED");
    assert_eq!(a_events.body["items"][0]["normalized_status"], "CREATED");

    // A cancels.
    let cancelled = call(
        &app,
        Method::DELETE,
        &format!("/v1/tracking-requests/{a_id}"),
        Some(&key_a),
        None,
    )
    .await;
    assert_eq!(cancelled.status, StatusCode::NO_CONTENT);

    // A loses access immediately, on every endpoint, and cannot cancel twice.
    for path in ["", "/events", "/jobs"] {
        let reply = get(&app, &format!("/v1/tracking-requests/{a_id}{path}"), &key_a).await;
        assert_eq!(
            reply.status,
            StatusCode::NOT_FOUND,
            "A after cancel: {path}"
        );
    }
    let again = call(
        &app,
        Method::DELETE,
        &format!("/v1/tracking-requests/{a_id}"),
        Some(&key_a),
        None,
    )
    .await;
    assert_eq!(again.status, StatusCode::NOT_FOUND);
    assert!(
        get(&app, "/v1/tracking-requests", &key_a).await.body["items"]
            .as_array()
            .unwrap()
            .is_empty()
    );

    // B is unaffected.
    let b_after = get(
        &app,
        &format!("/v1/tracking-requests/{b_id}/events"),
        &key_b,
    )
    .await;
    assert_eq!(b_after.status, StatusCode::OK);
    assert_eq!(b_after.body, b_events.body);
    assert_eq!(count(&pool, "active_requests").await, 1);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_user_can_subscribe_again_after_cancelling(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);

    let first = track(&app, &key, "DUMMY-OK-1").await.id();
    call(
        &app,
        Method::DELETE,
        &format!("/v1/tracking-requests/{first}"),
        Some(&key),
        None,
    )
    .await;
    let second = track(&app, &key, "DUMMY-OK-1").await;

    assert_eq!(second.status, StatusCode::CREATED);
    assert_ne!(second.id(), first);
    assert_eq!(count(&pool, "shipments").await, 1, "same shipment");
    assert_eq!(
        count(&pool, "tracking_jobs").await,
        1,
        "the job from the first subscription is still active"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn responses_do_not_expose_internal_or_other_users_data(pool: PgPool) {
    let (user_a, key_a) = user(&pool, "a@example.com").await;
    let (user_b, key_b) = user(&pool, "b@example.com").await;
    let app = app(&pool);
    track(&app, &key_a, "DUMMY-OK-1").await;
    let b = call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key_b),
        Some(json!({ "provider_code": "dummy", "tracking_number": "DUMMY-OK-1", "reference": "B-private-ref" })),
    )
    .await;
    let shipment_id: uuid::Uuid = sqlx::query_scalar("SELECT id FROM shipments")
        .fetch_one(&pool)
        .await
        .unwrap();

    let a_view = get(&app, "/v1/tracking-requests", &key_a)
        .await
        .body
        .to_string();
    assert!(
        !a_view.contains("B-private-ref"),
        "another subscriber's reference leaked"
    );
    assert!(
        !a_view.contains(&b.id()),
        "another subscriber's request ID leaked"
    );
    assert!(!a_view.contains(&user_a.to_string()) && !a_view.contains(&user_b.to_string()));
    assert!(
        !a_view.contains(&shipment_id.to_string()),
        "internal shipment ID leaked"
    );
}

// ----------------------------------------------------------------------------- generations

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_reused_tracking_number_starts_a_new_shipment_with_its_own_history(pool: PgPool) {
    let (_, key_a) = user(&pool, "a@example.com").await;
    let (_, key_b) = user(&pool, "b@example.com").await;
    let app = app(&pool);

    // First generation: delivered long ago, then archived.
    let old_id = track(&app, &key_a, "DUMMY-OK-1").await.id();
    add_event(&pool, "DUMMY-OK-1", "PKG_DELIVERED", "DELIVERED").await;
    complete_all_jobs(&pool).await;
    sqlx::query("UPDATE shipments SET current_status = 'DELIVERED', terminal_at = now(), archived_at = now()")
        .execute(&pool)
        .await
        .unwrap();

    // The courier reuses the number; a different user tracks it.
    let new = track(&app, &key_b, "DUMMY-OK-1").await;
    assert_eq!(new.status, StatusCode::CREATED);
    assert_eq!(
        new.body["shipment_status"], "UNKNOWN",
        "a new shipment, not the delivered one"
    );
    assert_eq!(new.body["latest_job"]["status"], "QUEUED");
    assert_eq!(count(&pool, "shipments").await, 2);
    assert_eq!(count(&pool, "active_shipments").await, 1);

    let new_events = get(
        &app,
        &format!("/v1/tracking-requests/{}/events", new.id()),
        &key_b,
    )
    .await;
    assert!(
        new_events.body["items"].as_array().unwrap().is_empty(),
        "old history must not leak"
    );

    // The old generation's history is still intact under the old request.
    let old_events = get(
        &app,
        &format!("/v1/tracking-requests/{old_id}/events"),
        &key_a,
    )
    .await;
    assert_eq!(old_events.body["items"].as_array().unwrap().len(), 1);
}

// ----------------------------------------------------------------------------- refresh

#[sqlx::test(migrator = "MIGRATOR")]
async fn refresh_reuses_an_active_job_and_creates_one_when_none_is_active(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);
    let created = track(&app, &key, "DUMMY-TRANSIT-1").await;
    let path = format!("/v1/tracking-requests/{}/refresh", created.id());

    // A job is already queued: nothing new is created.
    let reused = call(&app, Method::POST, &path, Some(&key), None).await;
    assert_eq!(reused.status, StatusCode::OK);
    assert_eq!(reused.body["result"], "job_already_active");
    assert_eq!(reused.body["shipment_terminal"], false);
    assert_eq!(reused.body["job"]["id"], created.body["latest_job"]["id"]);
    assert_eq!(count(&pool, "tracking_jobs").await, 1);

    // After that job finishes, a refresh starts a new one.
    complete_all_jobs(&pool).await;
    let fresh = call(&app, Method::POST, &path, Some(&key), None).await;
    assert_eq!(fresh.status, StatusCode::ACCEPTED);
    assert_eq!(fresh.body["result"], "job_created");
    assert_eq!(fresh.body["job"]["trigger"], "manual");
    assert_eq!(fresh.body["job"]["status"], "QUEUED");
    assert_ne!(fresh.body["job"]["id"], created.body["latest_job"]["id"]);
    assert_eq!(count(&pool, "tracking_jobs").await, 2);
    assert_eq!(count(&pool, "job_attempts").await, 2);
    assert_eq!(count(&pool, "outbox_messages").await, 2);

    let jobs = get(
        &app,
        &format!("/v1/tracking-requests/{}/jobs", created.id()),
        &key,
    )
    .await;
    let items = jobs.body["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["id"], fresh.body["job"]["id"], "newest first");
}

async fn refresh(app: &axum::Router, key: &str, request_id: &str) -> common::Reply {
    call(
        app,
        Method::POST,
        &format!("/v1/tracking-requests/{request_id}/refresh"),
        Some(key),
        None,
    )
    .await
}

async fn shipment_status(pool: &PgPool, tracking_number: &str) -> String {
    sqlx::query_scalar::<_, String>(
        "SELECT current_status::text FROM shipments WHERE tracking_number = $1 AND archived_at IS NULL",
    )
    .bind(tracking_number)
    .fetch_one(pool)
    .await
    .unwrap()
}

/// Automatic lookups stay off for DELIVERED and RETURNED shipments: a new subscriber gets the
/// existing data and no job.
#[sqlx::test(migrator = "MIGRATOR")]
async fn a_terminal_shipment_is_not_looked_up_automatically(pool: PgPool) {
    let (_, key_a) = user(&pool, "a@example.com").await;
    let (_, key_b) = user(&pool, "b@example.com").await;
    let app = app(&pool);
    for (number, terminal) in [("DUMMY-OK-1", "DELIVERED"), ("DUMMY-OK-2", "RETURNED")] {
        track(&app, &key_a, number).await;
        complete_all_jobs(&pool).await;
        sqlx::query("UPDATE shipments SET current_status = $1::shipment_status, terminal_at = now() WHERE tracking_number = $2")
            .bind(terminal)
            .bind(number)
            .execute(&pool)
            .await
            .unwrap();

        let b = track(&app, &key_b, number).await;
        assert_eq!(b.status, StatusCode::CREATED);
        assert_eq!(b.body["shipment_status"], terminal);
        assert_eq!(b.body["latest_job"]["status"], "COMPLETED");
    }
    assert_eq!(
        count(&pool, "tracking_jobs").await,
        2,
        "one per shipment, none added"
    );
    assert_eq!(count(&pool, "outbox_messages").await, 2);
}

/// An explicit refresh of a DELIVERED or RETURNED shipment is allowed. It creates one job,
/// never a duplicate, and leaves the shipment status alone until a result is processed.
#[sqlx::test(migrator = "MIGRATOR")]
async fn an_explicit_refresh_of_a_terminal_shipment_creates_one_job_and_keeps_the_status(
    pool: PgPool,
) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);
    for (number, terminal) in [("DUMMY-OK-1", "DELIVERED"), ("DUMMY-OK-2", "RETURNED")] {
        let request_id = track(&app, &key, number).await.id();
        complete_all_jobs(&pool).await;
        sqlx::query("UPDATE shipments SET current_status = $1::shipment_status, terminal_at = now() WHERE tracking_number = $2")
            .bind(terminal)
            .bind(number)
            .execute(&pool)
            .await
            .unwrap();
        let jobs_before = count(&pool, "tracking_jobs").await;

        let first = refresh(&app, &key, &request_id).await;
        assert_eq!(first.status, StatusCode::ACCEPTED);
        assert_eq!(first.body["result"], "job_created");
        assert_eq!(first.body["shipment_terminal"], true);
        assert_eq!(first.body["shipment_status"], terminal);
        assert_eq!(first.body["job"]["status"], "QUEUED");
        assert_eq!(first.body["job"]["trigger"], "manual");

        // A second refresh while that job is pending reuses it. The response is clearly
        // different from the first: same shipment state, different result.
        let second = refresh(&app, &key, &request_id).await;
        assert_eq!(second.status, StatusCode::OK);
        assert_eq!(second.body["result"], "job_already_active");
        assert_eq!(second.body["shipment_terminal"], true);
        assert_eq!(second.body["job"]["id"], first.body["job"]["id"]);
        assert_eq!(count(&pool, "tracking_jobs").await, jobs_before + 1);

        // The pending refresh has not touched the shipment.
        assert_eq!(shipment_status(&pool, number).await, terminal);
        let view = get(&app, &format!("/v1/tracking-requests/{request_id}"), &key).await;
        assert_eq!(view.body["shipment_status"], terminal);
        assert_eq!(view.body["latest_job"]["status"], "QUEUED");
        complete_all_jobs(&pool).await;
    }
    assert_eq!(count(&pool, "tracking_jobs").await, 4);
    assert_eq!(count(&pool, "job_attempts").await, 4);
    assert_eq!(count(&pool, "outbox_messages").await, 4);
}

/// Regression test for the Phase 2 review finding M1: a refresh on an archived shipment
/// used to return 202 and queue a job on the closed generation.
#[sqlx::test(migrator = "MIGRATOR")]
async fn refresh_on_an_archived_shipment_is_rejected_and_creates_nothing(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);
    // Not terminal on purpose: this is the exact state in which the bug created a job.
    let old_id = track(&app, &key, "DUMMY-TRANSIT-1").await.id();
    complete_all_jobs(&pool).await;
    sqlx::query("UPDATE shipments SET archived_at = now()")
        .execute(&pool)
        .await
        .unwrap();

    let rejected = refresh(&app, &key, &old_id).await;
    assert_eq!(rejected.status, StatusCode::CONFLICT);
    assert_eq!(rejected.body["error"]["code"], "shipment_archived");
    assert_eq!(
        count(&pool, "tracking_jobs").await,
        1,
        "no job on the archived generation"
    );
    assert_eq!(count(&pool, "job_attempts").await, 1);
    assert_eq!(count(&pool, "outbox_messages").await, 1);

    // The normal dispatch flow still works for the same number: it creates the new active
    // generation, with its own job.
    let new = track(&app, &key, "DUMMY-TRANSIT-1").await;
    assert_eq!(new.status, StatusCode::CREATED);
    assert_ne!(new.id(), old_id);
    assert_eq!(new.body["latest_job"]["status"], "QUEUED");
    assert_eq!(count(&pool, "shipments").await, 2);
    assert_eq!(count(&pool, "active_shipments").await, 1);
    assert_eq!(count(&pool, "tracking_jobs").await, 2);

    // Asking again retrieves that same generation rather than creating a third.
    let again = track(&app, &key, "DUMMY-TRANSIT-1").await;
    assert_eq!(again.status, StatusCode::OK);
    assert_eq!(again.id(), new.id());
    assert_eq!(count(&pool, "shipments").await, 2);

    // The new generation refreshes normally; the old one stays closed.
    assert_eq!(
        refresh(&app, &key, &new.id()).await.body["result"],
        "job_already_active"
    );
    assert_eq!(
        refresh(&app, &key, &old_id).await.status,
        StatusCode::CONFLICT
    );
    let jobs_on_archived: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tracking_jobs j JOIN shipments s ON s.id = j.shipment_id
         WHERE s.archived_at IS NOT NULL AND j.status <> 'COMPLETED'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(jobs_on_archived, 0);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn refresh_reports_a_disabled_provider_instead_of_pretending_a_job_is_running(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);
    let request_id = track(&app, &key, "DUMMY-TRANSIT-1").await.id();
    complete_all_jobs(&pool).await;
    sqlx::query("UPDATE providers SET enabled = false")
        .execute(&pool)
        .await
        .unwrap();

    let reply = refresh(&app, &key, &request_id).await;
    assert_eq!(reply.status, StatusCode::CONFLICT);
    assert_eq!(reply.body["error"]["code"], "provider_unavailable");
    assert_eq!(count(&pool, "tracking_jobs").await, 1);
}

// ----------------------------------------------------------------------------- list

#[sqlx::test(migrator = "MIGRATOR")]
async fn listing_is_paginated_newest_first_without_gaps_or_repeats(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);
    let mut created = Vec::new();
    for n in 1..=5 {
        created.push(track(&app, &key, &format!("DUMMY-OK-{n}")).await.id());
    }
    created.reverse(); // newest first

    let mut seen = Vec::new();
    let mut cursor: Option<String> = None;
    let mut pages = 0;
    loop {
        let path = match &cursor {
            Some(c) => format!("/v1/tracking-requests?limit=2&cursor={c}"),
            None => "/v1/tracking-requests?limit=2".to_string(),
        };
        let page = get(&app, &path, &key).await;
        assert_eq!(page.status, StatusCode::OK);
        let items = page.body["items"].as_array().unwrap();
        assert!(items.len() <= 2);
        seen.extend(items.iter().map(|i| i["id"].as_str().unwrap().to_string()));
        pages += 1;
        match page.body["next_cursor"].as_str() {
            Some(next) => cursor = Some(next.to_string()),
            None => break,
        }
    }
    assert_eq!(pages, 3);
    assert_eq!(seen, created);

    for bad in ["limit=0", "limit=201", "limit=abc", "cursor=not-a-uuid"] {
        let reply = get(&app, &format!("/v1/tracking-requests?{bad}"), &key).await;
        assert_eq!(reply.status, StatusCode::UNPROCESSABLE_ENTITY, "{bad}");
    }
}
