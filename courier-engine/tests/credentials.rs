//! Phase 6: provider credentials, the internal resolve API, dispatch rules and rate limiting.

mod common;

use axum::{Router, http::Method};
use common::call;
use courier_engine::{
    api,
    config::Config,
    db,
    services::{
        credentials::{self, PgSecretStore, ResolveRequest, Resolved, SecretStore},
        crypto::MasterKey,
        worker_tokens,
    },
    state::AppState,
};
use serde_json::{Value, json};
use sqlx::PgPool;
use uuid::Uuid;

const DHL_KEY: &str = "dhl-api-key-THAT-MUST-NEVER-LEAK";

fn store() -> PgSecretStore {
    PgSecretStore::new(MasterKey::from_hex(&"5a".repeat(32), 1).unwrap())
}

fn secret(value: &str) -> credentials::SecretFields {
    json!({ "api_key": value }).as_object().unwrap().clone()
}

fn internal_app(pool: &PgPool, with_store: bool) -> Router {
    let mut state = AppState::new(pool.clone(), Config::for_tests());
    if with_store {
        state = state.with_secrets(store());
    }
    api::internal::router(state)
}

/// Stores the DHL credential, creates a user and one DHL tracking request.
/// Returns (credential id, job id, user key).
async fn dhl_job(pool: &PgPool, number: &str) -> (Uuid, Uuid, String) {
    let credential_id =
        credentials::set_credential(pool, &store(), "dhl", "test", &secret(DHL_KEY))
            .await
            .unwrap();
    let (_, key) = common::user(pool, &format!("{number}@example.com")).await;
    let reply = call(
        &common::app(pool),
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(json!({ "provider_code": "dhl", "tracking_number": number })),
    )
    .await;
    assert_eq!(reply.status, 201, "{:?}", reply.body);
    let job_id: Uuid = sqlx::query_scalar(
        "SELECT j.id FROM tracking_jobs j JOIN shipments s ON s.id = j.shipment_id WHERE s.tracking_number = $1",
    )
    .bind(number)
    .fetch_one(pool)
    .await
    .unwrap();
    (credential_id, job_id, key)
}

async fn token(pool: &PgPool, adapter: &str) -> (String, worker_tokens::WorkerIdentity) {
    let token = worker_tokens::issue(pool, adapter, Some("test"))
        .await
        .unwrap()
        .unwrap();
    let identity = worker_tokens::authenticate(pool, &token)
        .await
        .unwrap()
        .unwrap();
    (token, identity)
}

async fn resolve_as(
    pool: &PgPool,
    worker: &worker_tokens::WorkerIdentity,
    credential_ref: Uuid,
    job_id: Uuid,
    attempt: i32,
) -> Resolved {
    credentials::resolve(
        pool,
        &store(),
        worker,
        ResolveRequest {
            credential_ref,
            job_id,
            attempt,
        },
    )
    .await
    .unwrap()
}

fn denied_reason(resolved: Resolved) -> &'static str {
    match resolved {
        Resolved::Denied(reason) => reason,
        Resolved::Secret(_) => panic!("expected a denial"),
    }
}

async fn audit_rows(pool: &PgPool, job_id: Uuid) -> Vec<(String, Option<String>)> {
    sqlx::query_as("SELECT outcome, reason FROM audit_log WHERE job_id = $1 ORDER BY id")
        .bind(job_id)
        .fetch_all(pool)
        .await
        .unwrap()
}

// ----------------------------------------------------------------------------- storage

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_credential_is_stored_encrypted_and_reads_back(pool: PgPool) {
    let id = credentials::set_credential(&pool, &store(), "dhl", "main", &secret(DHL_KEY))
        .await
        .unwrap();

    // Nothing in the row, in any encoding PostgreSQL can print, contains the secret.
    let row: String =
        sqlx::query_scalar("SELECT c::text FROM provider_credentials c WHERE id = $1")
            .bind(id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert!(!row.contains(DHL_KEY));
    assert!(!row.contains(&hex::encode(DHL_KEY)));
    let audit: String = sqlx::query_scalar("SELECT string_agg(a::text, ' ') FROM audit_log a")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(audit.contains("credential.set") && audit.contains("api_key"));
    assert!(
        !audit.contains(DHL_KEY),
        "the audit log names the fields, never the values"
    );

    let mut conn = pool.acquire().await.unwrap();
    let read = store().get(&mut conn, id).await.unwrap().unwrap();
    assert_eq!(read["api_key"], DHL_KEY);

    // Another master key cannot read it.
    let other = PgSecretStore::new(MasterKey::from_hex(&"11".repeat(32), 1).unwrap());
    assert!(other.get(&mut conn, id).await.is_err());
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_ciphertext_copied_to_another_credential_row_cannot_be_read(pool: PgPool) {
    let dhl = credentials::set_credential(&pool, &store(), "dhl", "a", &secret("dhl-secret"))
        .await
        .unwrap();
    let dummy = credentials::set_credential(&pool, &store(), "dummy", "b", &secret("dummy-secret"))
        .await
        .unwrap();
    sqlx::query(
        "UPDATE provider_credentials t SET wrapped_key = s.wrapped_key, key_nonce = s.key_nonce,
                ciphertext = s.ciphertext, nonce = s.nonce
         FROM provider_credentials s WHERE t.id = $1 AND s.id = $2",
    )
    .bind(dummy)
    .bind(dhl)
    .execute(&pool)
    .await
    .unwrap();
    let mut conn = pool.acquire().await.unwrap();
    assert!(store().get(&mut conn, dummy).await.is_err());
    assert_eq!(
        store().get(&mut conn, dhl).await.unwrap().unwrap()["api_key"],
        "dhl-secret"
    );
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn storing_a_new_credential_revokes_the_previous_one(pool: PgPool) {
    let first = credentials::set_credential(&pool, &store(), "dhl", "old", &secret("one"))
        .await
        .unwrap();
    let second = credentials::set_credential(&pool, &store(), "dhl", "new", &secret("two"))
        .await
        .unwrap();
    let listed = credentials::list_credentials(&pool).await.unwrap();
    let state: Vec<_> = listed
        .iter()
        .map(|c| (c.id, c.revoked_at.is_some()))
        .collect();
    assert_eq!(state.len(), 2);
    assert!(state.contains(&(first, true)) && state.contains(&(second, false)));

    assert!(credentials::revoke_credential(&pool, "dhl").await.unwrap());
    assert!(
        !credentials::revoke_credential(&pool, "dhl").await.unwrap(),
        "nothing left to revoke"
    );
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn malformed_secrets_and_unknown_providers_are_refused(pool: PgPool) {
    let bad = [
        json!({}),
        json!({ "api_key": "" }),
        json!({ "api_key": 5 }),
        json!({ "API-Key": "x" }),
        json!({ "api_key": "x".repeat(9000) }),
    ];
    for value in bad {
        let fields = value.as_object().unwrap().clone();
        let result = credentials::set_credential(&pool, &store(), "dhl", "l", &fields).await;
        assert!(
            matches!(result, Err(credentials::CredentialError::InvalidSecret(_))),
            "{value}"
        );
    }
    let unknown = credentials::set_credential(&pool, &store(), "nope", "l", &secret("x")).await;
    assert!(matches!(
        unknown,
        Err(credentials::CredentialError::UnknownProvider)
    ));
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM provider_credentials")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(count, 0);
}

// ----------------------------------------------------------------------------- dispatch rules

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_provider_that_needs_a_credential_is_refused_until_one_is_stored(pool: PgPool) {
    let app = common::app(&pool);
    let (_, key) = common::user(&pool, "a@example.com").await;
    let body = json!({ "provider_code": "dhl", "tracking_number": "JD0001" });
    let refused = call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(body.clone()),
    )
    .await;
    assert_eq!(refused.status, 409);
    assert_eq!(refused.body["error"]["code"], "provider_unavailable");
    let rows: i64 = sqlx::query_scalar("SELECT count(*) FROM shipments")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(rows, 0, "a refused request leaves nothing behind");

    let credential = credentials::set_credential(&pool, &store(), "dhl", "k", &secret(DHL_KEY))
        .await
        .unwrap();
    let accepted = call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(body),
    )
    .await;
    assert_eq!(accepted.status, 201);

    // The message carries the reference, and nothing of the secret.
    let payload: Value = sqlx::query_scalar("SELECT payload FROM outbox_messages")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(payload["payload"]["credential_ref"], credential.to_string());
    assert!(!payload.to_string().contains(DHL_KEY));
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_job_for_a_provider_without_credentials_carries_no_reference(pool: PgPool) {
    let (_, key) = common::user(&pool, "a@example.com").await;
    let body = json!({ "provider_code": "dummy", "tracking_number": "DUMMY-OK-1" });
    call(
        &common::app(&pool),
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(body),
    )
    .await;
    let payload: Value = sqlx::query_scalar("SELECT payload FROM outbox_messages")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(payload["payload"].get("credential_ref").is_none());
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_scraping_provider_is_refused_until_scraping_is_authorized(pool: PgPool) {
    let app = common::app(&pool);
    let (_, key) = common::user(&pool, "a@example.com").await;
    let body = json!({ "provider_code": "demosite", "tracking_number": "DEMO-1" });
    sqlx::query("UPDATE providers SET scraping_authorized = false WHERE code = 'demosite'")
        .execute(&pool)
        .await
        .unwrap();
    let refused = call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(body.clone()),
    )
    .await;
    assert_eq!(
        (refused.status.as_u16(), &refused.body["error"]["code"]),
        (409, &json!("provider_unavailable"))
    );

    sqlx::query("UPDATE providers SET scraping_authorized = true WHERE code = 'demosite'")
        .execute(&pool)
        .await
        .unwrap();
    let accepted = call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(body),
    )
    .await;
    assert_eq!(accepted.status, 201);
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn the_dummy_provider_switch_turns_only_test_providers_on_and_off(pool: PgPool) {
    let app = common::app(&pool);
    let (_, key) = common::user(&pool, "a@example.com").await;
    let codes = |reply: &common::Reply| -> Vec<String> {
        let items = reply
            .body
            .as_array()
            .or(reply.body["items"].as_array())
            .unwrap();
        items
            .iter()
            .map(|p| p["code"].as_str().unwrap().to_string())
            .collect()
    };
    let before = call(&app, Method::GET, "/v1/providers", Some(&key), None).await;
    // Real couriers stay listed whatever the switch says. (The list grows with new adapters.)
    let real: Vec<String> =
        sqlx::query_scalar("SELECT code FROM providers WHERE NOT is_test ORDER BY code")
            .fetch_all(&pool)
            .await
            .unwrap();
    assert!(real.contains(&"dhl".to_string()));
    let mut all = [
        real.as_slice(),
        &["demosite".to_string(), "dummy".to_string()],
    ]
    .concat();
    all.sort_unstable();
    assert_eq!(codes(&before), all);

    assert_eq!(
        db::providers::set_test_providers_enabled(&pool, false)
            .await
            .unwrap(),
        2
    );
    let off = call(&app, Method::GET, "/v1/providers", Some(&key), None).await;
    assert_eq!(codes(&off), real);
    let body = json!({ "provider_code": "dummy", "tracking_number": "DUMMY-OK-1" });
    let refused = call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(body.clone()),
    )
    .await;
    assert_eq!(
        refused.status, 404,
        "a disabled provider does not exist for users"
    );
    assert_eq!(
        db::providers::set_test_providers_enabled(&pool, false)
            .await
            .unwrap(),
        0,
        "idempotent"
    );

    assert_eq!(
        db::providers::set_test_providers_enabled(&pool, true)
            .await
            .unwrap(),
        2
    );
    let accepted = call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(body),
    )
    .await;
    assert_eq!(accepted.status, 201);
}

// ----------------------------------------------------------------------------- resolve rules

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn the_worker_of_the_jobs_adapter_gets_the_secret_while_the_attempt_is_in_flight(
    pool: PgPool,
) {
    let (credential, job, _) = dhl_job(&pool, "JD0002").await;
    let (_, worker) = token(&pool, "dhl").await;

    for status in ["QUEUED", "PROCESSING"] {
        if status == "PROCESSING" {
            sqlx::query("UPDATE tracking_jobs SET status = 'PROCESSING', execution_deadline_at = now() + interval '1 minute' WHERE id = $1")
                .bind(job)
                .execute(&pool)
                .await
                .unwrap();
        }
        match resolve_as(&pool, &worker, credential, job, 1).await {
            Resolved::Secret(fields) => assert_eq!(fields["api_key"], DHL_KEY),
            Resolved::Denied(reason) => panic!("denied while {status}: {reason}"),
        }
    }
    assert_eq!(
        audit_rows(&pool, job).await,
        vec![("allowed".to_string(), None); 2]
    );
    let audit: String = sqlx::query_scalar("SELECT string_agg(a::text, ' ') FROM audit_log a")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(!audit.contains(DHL_KEY));
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn every_other_request_is_denied_and_audited(pool: PgPool) {
    let (credential, job, _) = dhl_job(&pool, "JD0003").await;
    let (_, dhl_worker) = token(&pool, "dhl").await;
    let (_, dummy_worker) = token(&pool, "dummy").await;
    let foreign = credentials::set_credential(&pool, &store(), "dummy", "x", &secret("other"))
        .await
        .unwrap();

    let cases = [
        (
            resolve_as(&pool, &dummy_worker, credential, job, 1).await,
            "job_belongs_to_another_adapter",
        ),
        (
            resolve_as(&pool, &dhl_worker, credential, job, 2).await,
            "attempt_not_in_flight",
        ),
        (
            resolve_as(&pool, &dhl_worker, credential, Uuid::now_v7(), 1).await,
            "unknown_job",
        ),
        (
            resolve_as(&pool, &dhl_worker, foreign, job, 1).await,
            "reference_not_for_this_provider",
        ),
        (
            resolve_as(&pool, &dhl_worker, Uuid::now_v7(), job, 1).await,
            "reference_not_for_this_provider",
        ),
    ];
    for (resolved, expected) in cases {
        assert_eq!(denied_reason(resolved), expected);
    }

    // A finished job no longer unlocks the secret.
    sqlx::query("UPDATE tracking_jobs SET status = 'FAILED', completed_at = now() WHERE id = $1")
        .bind(job)
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(
        denied_reason(resolve_as(&pool, &dhl_worker, credential, job, 1).await),
        "attempt_not_in_flight"
    );

    let denied = audit_rows(&pool, job).await;
    assert_eq!(denied.len(), 5, "one row per request that named this job");
    assert!(
        denied
            .iter()
            .all(|(outcome, reason)| outcome == "denied" && reason.is_some())
    );
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_replaced_credential_resolves_to_the_new_secret_and_a_revoked_one_to_nothing(
    pool: PgPool,
) {
    let (old_reference, job, _) = dhl_job(&pool, "JD0004").await;
    let (_, worker) = token(&pool, "dhl").await;

    credentials::set_credential(&pool, &store(), "dhl", "rotated", &secret("the-new-key"))
        .await
        .unwrap();
    match resolve_as(&pool, &worker, old_reference, job, 1).await {
        Resolved::Secret(fields) => assert_eq!(fields["api_key"], "the-new-key"),
        Resolved::Denied(reason) => panic!("denied: {reason}"),
    }

    credentials::revoke_credential(&pool, "dhl").await.unwrap();
    assert_eq!(
        denied_reason(resolve_as(&pool, &worker, old_reference, job, 1).await),
        "no_credential_in_use"
    );
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn worker_tokens_are_stored_hashed_and_can_be_revoked(pool: PgPool) {
    let (full, identity) = token(&pool, "dhl").await;
    assert_eq!(identity.adapter_key, "dhl");
    assert!(full.starts_with("cwt_"));
    let row: String = sqlx::query_scalar("SELECT t::text FROM worker_tokens t")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(
        !row.contains(full.rsplit('_').next().unwrap()),
        "only the hash of the secret part is stored"
    );

    let tampered = format!("{}0", &full[..full.len() - 1]);
    for bad in [
        tampered.as_str(),
        "cwt_x_y",
        "",
        &full.replace("cwt_", "ctk_"),
    ] {
        if bad != full {
            assert!(
                worker_tokens::authenticate(&pool, bad)
                    .await
                    .unwrap()
                    .is_none(),
                "{bad}"
            );
        }
    }
    assert!(
        worker_tokens::issue(&pool, "no_such_adapter", None)
            .await
            .unwrap()
            .is_none()
    );

    let prefix = full.split('_').nth(1).unwrap();
    assert!(
        worker_tokens::revoke_by_prefix(&pool, prefix)
            .await
            .unwrap()
    );
    assert!(
        worker_tokens::authenticate(&pool, &full)
            .await
            .unwrap()
            .is_none()
    );
}

// ----------------------------------------------------------------------------- internal HTTP API

async fn post_resolve(app: &Router, bearer: Option<&str>, body: Value) -> common::Reply {
    call(
        app,
        Method::POST,
        "/internal/v1/credentials/resolve",
        bearer,
        Some(body),
    )
    .await
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn the_internal_api_returns_the_secret_only_to_an_authorized_worker(pool: PgPool) {
    let (credential, job, user_key) = dhl_job(&pool, "JD0005").await;
    let (dhl_token, _) = token(&pool, "dhl").await;
    let (dummy_token, _) = token(&pool, "dummy").await;
    let app = internal_app(&pool, true);
    let body = json!({ "credential_ref": credential, "job_id": job, "attempt": 1 });

    let ok = post_resolve(&app, Some(&dhl_token), body.clone()).await;
    assert_eq!(ok.status, 200);
    assert_eq!(ok.body["secret"]["api_key"], DHL_KEY);
    assert_eq!(ok.headers["cache-control"], "no-store");

    assert_eq!(post_resolve(&app, None, body.clone()).await.status, 401);
    assert_eq!(
        post_resolve(&app, Some("cwt_bad"), body.clone())
            .await
            .status,
        401
    );
    assert_eq!(
        post_resolve(&app, Some(&user_key), body.clone())
            .await
            .status,
        401,
        "a user API key is not a worker token"
    );

    // Every refusal looks the same from outside.
    let wrong_adapter = post_resolve(&app, Some(&dummy_token), body.clone()).await;
    let wrong_attempt = post_resolve(
        &app,
        Some(&dhl_token),
        json!({ "credential_ref": credential, "job_id": job, "attempt": 9 }),
    )
    .await;
    for reply in [&wrong_adapter, &wrong_attempt] {
        assert_eq!(reply.status, 403);
        assert_eq!(reply.body["error"]["code"], "resolve_denied");
        assert!(!reply.body.to_string().contains(DHL_KEY));
    }
    assert_eq!(wrong_adapter.body, wrong_attempt.body);

    assert_eq!(
        post_resolve(&app, Some(&dhl_token), json!({ "job_id": "x" }))
            .await
            .status,
        422
    );
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn the_resolve_route_does_not_exist_on_the_public_api_and_needs_a_master_key(pool: PgPool) {
    let (credential, job, user_key) = dhl_job(&pool, "JD0006").await;
    let (dhl_token, _) = token(&pool, "dhl").await;
    let body = json!({ "credential_ref": credential, "job_id": job, "attempt": 1 });

    let public = common::app(&pool);
    for bearer in [&user_key, &dhl_token] {
        assert_eq!(
            post_resolve(&public, Some(bearer), body.clone())
                .await
                .status,
            404
        );
    }
    // The public API's own routes are not served on the internal listener either.
    let internal = internal_app(&pool, true);
    let reply = call(
        &internal,
        Method::GET,
        "/v1/providers",
        Some(&user_key),
        None,
    )
    .await;
    assert_eq!(reply.status, 404);

    let without_key = internal_app(&pool, false);
    assert_eq!(
        post_resolve(&without_key, Some(&dhl_token), body)
            .await
            .status,
        503
    );
}

// ----------------------------------------------------------------------------- rate limiting

async fn schedule(pool: &PgPool) -> Vec<(f64, f64)> {
    // (seconds from the first message's availability, queue budget in seconds) per message
    sqlx::query_as(
        "SELECT extract(epoch FROM o.available_at - min(o.available_at) OVER ())::float8,
                extract(epoch FROM j.queue_deadline_at - o.available_at)::float8
         FROM outbox_messages o
         JOIN tracking_jobs j ON j.id = (o.payload -> 'payload' ->> 'job_id')::uuid
         ORDER BY o.available_at",
    )
    .fetch_all(pool)
    .await
    .unwrap()
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn jobs_for_a_rate_limited_provider_are_spaced_by_its_interval(pool: PgPool) {
    sqlx::query("UPDATE providers SET min_dispatch_interval_ms = 2000, queue_wait_max_ms = 60000 WHERE code = 'dummy'")
        .execute(&pool)
        .await
        .unwrap();
    let app = common::app(&pool);
    let (_, key) = common::user(&pool, "a@example.com").await;
    for n in 0..4 {
        let body = json!({ "provider_code": "dummy", "tracking_number": format!("DUMMY-OK-{n}") });
        assert_eq!(
            call(
                &app,
                Method::POST,
                "/v1/tracking-requests",
                Some(&key),
                Some(body)
            )
            .await
            .status,
            201
        );
    }
    let slots = schedule(&pool).await;
    let offsets: Vec<f64> = slots.iter().map(|(offset, _)| *offset).collect();
    assert_eq!(offsets, [0.0, 2.0, 4.0, 6.0]);
    // The queue-wait budget starts at the slot: waiting for a slot is not waiting for a worker.
    assert!(
        slots
            .iter()
            .all(|(_, budget)| (*budget - 60.0).abs() < 0.001),
        "{slots:?}"
    );
    // The message tells the worker the same deadline.
    let agree: bool = sqlx::query_scalar(
        "SELECT bool_and((o.payload -> 'payload' ->> 'expires_at')::timestamptz = j.queue_deadline_at)
         FROM outbox_messages o JOIN tracking_jobs j ON j.id = (o.payload -> 'payload' ->> 'job_id')::uuid",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(agree);

    // Only the first message is publishable now.
    let due: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM outbox_messages WHERE available_at <= clock_timestamp()",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(due, 1);
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn simultaneous_requests_each_get_their_own_slot(pool: PgPool) {
    sqlx::query("UPDATE providers SET min_dispatch_interval_ms = 1000 WHERE code = 'dummy'")
        .execute(&pool)
        .await
        .unwrap();
    let app = common::app(&pool);
    let (_, key) = common::user(&pool, "a@example.com").await;
    let mut tasks = tokio::task::JoinSet::new();
    for n in 0..12 {
        let (app, key) = (app.clone(), key.clone());
        tasks.spawn(async move {
            let body =
                json!({ "provider_code": "dummy", "tracking_number": format!("DUMMY-OK-{n}") });
            call(
                &app,
                Method::POST,
                "/v1/tracking-requests",
                Some(&key),
                Some(body),
            )
            .await
            .status
        });
    }
    while let Some(status) = tasks.join_next().await {
        assert_eq!(status.unwrap(), 201);
    }
    let mut offsets: Vec<i64> = schedule(&pool)
        .await
        .iter()
        .map(|(o, _)| o.round() as i64)
        .collect();
    offsets.sort_unstable();
    assert_eq!(
        offsets,
        (0..12).collect::<Vec<i64>>(),
        "no two jobs share a slot"
    );
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn an_idle_provider_dispatches_immediately_and_an_unlimited_one_always(pool: PgPool) {
    let app = common::app(&pool);
    let (_, key) = common::user(&pool, "a@example.com").await;
    // Unlimited (the default for the dummy provider): everything is available at once.
    for n in 0..3 {
        let body = json!({ "provider_code": "dummy", "tracking_number": format!("DUMMY-OK-{n}") });
        call(
            &app,
            Method::POST,
            "/v1/tracking-requests",
            Some(&key),
            Some(body),
        )
        .await;
    }
    let due: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM outbox_messages WHERE available_at <= clock_timestamp()",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(due, 3);

    // Limited, but the last dispatch was long ago: no waiting, slots do not accumulate.
    sqlx::query("UPDATE providers SET min_dispatch_interval_ms = 5000, next_dispatch_at = now() - interval '1 hour' WHERE code = 'dummy'")
        .execute(&pool)
        .await
        .unwrap();
    let body = json!({ "provider_code": "dummy", "tracking_number": "DUMMY-OK-LATER" });
    call(
        &app,
        Method::POST,
        "/v1/tracking-requests",
        Some(&key),
        Some(body),
    )
    .await;
    let due: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM outbox_messages WHERE available_at <= clock_timestamp()",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(due, 4);
}

// ----------------------------------------------------------------------------- audit log

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn the_audit_log_cannot_be_changed_or_deleted(pool: PgPool) {
    credentials::set_credential(&pool, &store(), "dhl", "k", &secret("x"))
        .await
        .unwrap();
    for statement in [
        "UPDATE audit_log SET outcome = 'denied'",
        "DELETE FROM audit_log",
    ] {
        let error = sqlx::query(statement).execute(&pool).await.unwrap_err();
        assert!(error.to_string().contains("append-only"), "{error}");
    }
    let rows: i64 = sqlx::query_scalar("SELECT count(*) FROM audit_log")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(rows, 1);
}
