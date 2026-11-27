//! Phase 12: couriers added without an adapter, built by the AI; per-organization logins.

mod common;

use axum::{Router, http::Method};
use common::{app, call, count, get};
use courier_engine::{
    api,
    config::Config,
    db,
    services::{
        credentials::{self, PgSecretStore, ResolveRequest, Resolved},
        crypto::MasterKey,
        onboarding::{self, code_from_name, host_of},
        polling, worker_tokens,
    },
    state::AppState,
};
use serde_json::{Value, json};
use sqlx::PgPool;
use uuid::Uuid;

const PASSWORD: &str = "acme-password-THAT-MUST-NEVER-LEAK";

fn store() -> PgSecretStore {
    PgSecretStore::new(MasterKey::from_hex(&"5a".repeat(32), 1).unwrap())
}

fn app_with_store(pool: &PgPool) -> Router {
    api::router(AppState::new(pool.clone(), Config::for_tests()).with_secrets(store()))
}

fn courier(name: &str) -> Value {
    json!({
        "name": name,
        "method": "scrape",
        "tracking_url": "https://track.newcourier.example/status?awb=",
        "notes": "Enter the number, press Track; a table of scans appears.",
        "permitted": true,
    })
}

async fn add(app: &Router, key: &str, body: Value) -> common::Reply {
    call(app, Method::POST, "/v1/providers", Some(key), Some(body)).await
}

async fn track(app: &Router, key: &str, code: &str, number: &str) -> common::Reply {
    call(
        app,
        Method::POST,
        "/v1/tracking-requests",
        Some(key),
        Some(json!({ "provider_code": code, "tracking_number": number })),
    )
    .await
}

async fn status(pool: &PgPool, code: &str) -> String {
    sqlx::query_scalar("SELECT status FROM providers WHERE code = $1")
        .bind(code)
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn tasks(pool: &PgPool, code: &str) -> Vec<(String, String, Value)> {
    sqlx::query_as(
        "SELECT kind, status, request FROM repair_tasks WHERE adapter_key = $1 ORDER BY created_at",
    )
    .bind(code)
    .fetch_all(pool)
    .await
    .unwrap()
}

/// What the AI's adapter migration does: the adapter row, and the courier live.
async fn adapter_arrives(pool: &PgPool, code: &str) {
    sqlx::query(
        "INSERT INTO adapters (id, provider_id, key, method, queue_name)
         SELECT $1, id, code, tracking_method, 'adapter.' || code || '.jobs' FROM providers WHERE code = $2",
    )
    .bind(Uuid::now_v7())
    .bind(code)
    .execute(pool)
    .await
    .unwrap();
    sqlx::query("UPDATE providers SET status = 'live' WHERE code = $1")
        .bind(code)
        .execute(pool)
        .await
        .unwrap();
}

async fn fail_open_task(pool: &PgPool, code: &str, reason: &str) {
    sqlx::query(
        "UPDATE repair_tasks SET status = 'failed', failure_reason = $2,
                finished_at = now() - interval '1 hour'
          WHERE adapter_key = $1 AND status IN ('pending', 'running')",
    )
    .bind(code)
    .bind(reason)
    .execute(pool)
    .await
    .unwrap();
}

// ----------------------------------------------------------------------------- helpers

#[test]
fn codes_and_hosts_come_from_names_and_addresses() {
    assert_eq!(
        code_from_name("Shree Maruti Courier").as_deref(),
        Some("shreemaruticourier")
    );
    assert_eq!(
        code_from_name("2 Day Express").as_deref(),
        Some("dayexpress")
    );
    assert_eq!(code_from_name("!!!"), None);
    assert_eq!(
        host_of("https://Track.Example.com:8443/a?b=c").as_deref(),
        Some("track.example.com")
    );
    assert_eq!(
        host_of("http://user@x.example/").as_deref(),
        Some("x.example")
    );
    assert_eq!(host_of("ftp://x.example/"), None);
    assert_eq!(host_of("https://"), None);
}

// ----------------------------------------------------------------------------- adding

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn an_organization_user_adds_a_courier_and_a_build_task_opens_by_itself(pool: PgPool) {
    let (user_id, key) = common::user(&pool, "asha@example.com").await;
    let app = app(&pool);

    let reply = add(&app, &key, courier("New Courier")).await;
    assert_eq!(reply.status, 202, "{:?}", reply.body);
    assert_eq!(
        reply.body,
        json!({ "code": "newcourier", "status": "building" })
    );
    assert_eq!(status(&pool, "newcourier").await, "building");

    // Who added it and who recorded the owner's permission are kept.
    let (added_by, permitted_by, permitted): (Option<Uuid>, Option<Uuid>, bool) = sqlx::query_as(
        "SELECT added_by, permission_recorded_by, scraping_authorized FROM providers WHERE code = 'newcourier'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        (added_by, permitted_by, permitted),
        (Some(user_id), Some(user_id), true)
    );

    // The AI repair service is asked for an adapter, with what the user told.
    let tasks = tasks(&pool, "newcourier").await;
    assert_eq!(tasks.len(), 1);
    let (kind, task_status, request) = &tasks[0];
    assert_eq!(
        (kind.as_str(), task_status.as_str()),
        ("new_provider", "pending")
    );
    assert_eq!(request["hosts"], json!(["track.newcourier.example"]));
    assert_eq!(request["method"], "scrape");
    assert_eq!(
        request["tracking_url"],
        "https://track.newcourier.example/status?awb="
    );
    assert_eq!(request["login"], false);

    // Running the check again opens nothing more.
    assert_eq!(onboarding::open_build_tasks(&pool).await.unwrap(), 0);

    // It is listed, with its status.
    let list = get(&app, "/v1/providers", &key).await;
    let item = list.body["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p["code"] == "newcourier")
        .cloned()
        .unwrap();
    assert_eq!(item["status"], "building");
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_website_needs_its_owners_permission_and_bad_input_is_refused(pool: PgPool) {
    let (_, key) = common::user(&pool, "a@example.com").await;
    let app = app(&pool);

    let mut body = courier("Nope Courier");
    body["permitted"] = json!(false);
    let reply = add(&app, &key, body).await;
    assert_eq!(reply.status, 422, "{:?}", reply.body);

    for (field, value) in [
        ("method", json!("email")),
        ("tracking_url", json!("track.example.com")),
        ("tracking_url", json!("javascript:alert(1)")),
        ("tracking_url", json!("http://10.0.0.5/track")),
        (
            "tracking_url",
            json!("http://127.0.0.1:8081/internal/v1/repairs/claim"),
        ),
        ("tracking_url", json!("http://engine.internal/x")),
        ("hosts", json!(["192.168.1.1"])),
        ("hosts", json!(["db.corp"])),
        ("name", json!("")),
        ("code", json!("Bad Code")),
    ] {
        let mut body = courier("Bad Courier");
        body[field] = value;
        assert_eq!(add(&app, &key, body).await.status, 422, "{field}");
    }
    let mut body = courier("Bad Courier");
    body["hosts"] = json!(["https://x.example/path"]);
    assert_eq!(add(&app, &key, body).await.status, 422);

    // An existing courier's code cannot be taken.
    let mut body = courier("Dummy");
    body["code"] = json!("dummy");
    let reply = add(&app, &key, body).await;
    assert_eq!(reply.status, 409);
    assert_eq!(reply.body["error"]["code"], "provider_exists");
    assert_eq!(count(&pool, "repair_tasks").await, 0);

    // Without a key nobody adds anything.
    let reply = call(
        &app,
        Method::POST,
        "/v1/providers",
        None,
        Some(courier("X Courier")),
    )
    .await;
    assert_eq!(reply.status, 401);
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn an_organization_adds_at_most_five_couriers_a_day_admins_any_number(pool: PgPool) {
    let (_, key) = common::user(&pool, "a@example.com").await;
    let (_, admin) = common::admin(&pool, "ops@example.com").await;
    let app = app(&pool);
    for i in 0..onboarding::MAX_PER_ORGANIZATION_PER_DAY {
        let reply = add(&app, &key, courier(&format!("Courier {i}"))).await;
        assert_eq!(reply.status, 202, "{:?}", reply.body);
    }
    let reply = add(&app, &key, courier("Courier Six")).await;
    assert_eq!(reply.status, 409);
    assert_eq!(reply.body["error"]["code"], "too_many_new_providers");
    for i in 0..6 {
        assert_eq!(
            add(&app, &admin, courier(&format!("Admin {i}")))
                .await
                .status,
            202
        );
    }
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_courier_inserted_straight_into_the_database_is_built_too(pool: PgPool) {
    sqlx::query(
        "INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms,
                                result_grace_ms, max_attempts, retry_base_ms, retry_max_ms,
                                status, tracking_url, notes)
         VALUES ($1, 'handmade', 'Handmade Courier', 'api', 20000, 900000, 30000, 3, 5000, 300000,
                 'building', 'https://api.handmade.example/v1/track', NULL)",
    )
    .bind(Uuid::now_v7())
    .execute(&pool)
    .await
    .unwrap();
    assert_eq!(onboarding::open_build_tasks(&pool).await.unwrap(), 1);
    let tasks = tasks(&pool, "handmade").await;
    assert_eq!(tasks[0].2["hosts"], json!(["api.handmade.example"]));
    assert_eq!(
        tasks[0].2["notes"],
        "Tracked at https://api.handmade.example/v1/track"
    );
}

// ----------------------------------------------------------------------------- waiting lookups

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn requests_wait_for_the_adapter_and_start_by_themselves_when_it_is_live(pool: PgPool) {
    let (_, key) = common::user(&pool, "a@example.com").await;
    let app = app(&pool);
    assert_eq!(add(&app, &key, courier("New Courier")).await.status, 202);

    // Accepted, but no lookup yet.
    let reply = track(&app, &key, "newcourier", "NC123").await;
    assert_eq!(reply.status, 201, "{:?}", reply.body);
    assert_eq!(reply.body["provider_status"], "building");
    assert!(reply.body["latest_job"].is_null());
    assert_eq!(count(&pool, "tracking_jobs").await, 0);
    let request_id = reply.id();

    // A refresh says why nothing happens.
    let refresh = call(
        &app,
        Method::POST,
        &format!("/v1/tracking-requests/{request_id}/refresh"),
        Some(&key),
        None,
    )
    .await;
    assert_eq!(refresh.status, 409);
    assert_eq!(refresh.body["error"]["code"], "adapter_being_built");

    // The scheduler looks again and keeps it waiting while there is no adapter.
    sqlx::query("UPDATE shipments SET next_poll_at = now() - interval '1 second'")
        .execute(&pool)
        .await
        .unwrap();
    polling::poll_due(&pool, 10).await.unwrap();
    assert_eq!(count(&pool, "tracking_jobs").await, 0);
    let next: bool = sqlx::query_scalar("SELECT next_poll_at > now() FROM shipments")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(next);

    // The adapter arrives: the next pass starts the lookup.
    adapter_arrives(&pool, "newcourier").await;
    sqlx::query("UPDATE shipments SET next_poll_at = now() - interval '1 second'")
        .execute(&pool)
        .await
        .unwrap();
    let stats = polling::poll_due(&pool, 10).await.unwrap();
    assert_eq!(stats.started, 1);
    assert_eq!(count(&pool, "tracking_jobs").await, 1);
    let view = get(&app, &format!("/v1/tracking-requests/{request_id}"), &key).await;
    assert_eq!(view.body["provider_status"], "live");
    assert!(!view.body["latest_job"].is_null());
}

// ----------------------------------------------------------------------------- failing builds

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn after_three_failed_tries_the_courier_is_failed_until_an_admin_retries(pool: PgPool) {
    let (_, key) = common::user(&pool, "a@example.com").await;
    let app = app(&pool);
    assert_eq!(add(&app, &key, courier("Hard Courier")).await.status, 202);
    let request_id = track(&app, &key, "hardcourier", "H1").await.id();

    for attempt in 1..=onboarding::MAX_BUILD_TRIES {
        fail_open_task(
            &pool,
            "hardcourier",
            &format!("try {attempt}: the site timed out"),
        )
        .await;
        onboarding::open_build_tasks(&pool).await.unwrap();
    }
    assert_eq!(status(&pool, "hardcourier").await, "failed");
    assert_eq!(tasks(&pool, "hardcourier").await.len(), 3);
    let failure: String =
        sqlx::query_scalar("SELECT build_failure FROM providers WHERE code = 'hardcourier'")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert!(failure.contains("timed out"), "{failure}");

    // New requests and refreshes say so.
    let reply = track(&app, &key, "hardcourier", "H2").await;
    assert_eq!(reply.body["error"]["code"], "adapter_build_failed");
    let refresh = call(
        &app,
        Method::POST,
        &format!("/v1/tracking-requests/{request_id}/refresh"),
        Some(&key),
        None,
    )
    .await;
    assert_eq!(refresh.body["error"]["code"], "adapter_build_failed");

    // A new round of three tries.
    assert!(onboarding::retry(&pool, "hardcourier").await.unwrap());
    assert_eq!(status(&pool, "hardcourier").await, "building");
    let tasks = tasks(&pool, "hardcourier").await;
    assert_eq!(tasks.len(), 4);
    assert_eq!(tasks[3].1, "pending");
    assert!(!onboarding::retry(&pool, "hardcourier").await.unwrap());
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_failed_try_is_followed_by_another_after_a_pause(pool: PgPool) {
    let (_, key) = common::user(&pool, "a@example.com").await;
    assert_eq!(
        add(&app(&pool), &key, courier("Slow Courier")).await.status,
        202
    );
    sqlx::query(
        "UPDATE repair_tasks SET status = 'failed', failure_reason = 'x', finished_at = now()
          WHERE adapter_key = 'slowcourier'",
    )
    .execute(&pool)
    .await
    .unwrap();
    // Just failed: wait.
    assert_eq!(onboarding::open_build_tasks(&pool).await.unwrap(), 0);
    sqlx::query("UPDATE repair_tasks SET finished_at = now() - interval '11 minutes'")
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(onboarding::open_build_tasks(&pool).await.unwrap(), 1);
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn an_adapter_that_arrives_without_setting_the_status_makes_the_courier_live(pool: PgPool) {
    let (_, key) = common::user(&pool, "a@example.com").await;
    assert_eq!(
        add(&app(&pool), &key, courier("Quiet Courier"))
            .await
            .status,
        202
    );
    sqlx::query(
        "INSERT INTO adapters (id, provider_id, key, method, queue_name)
         SELECT $1, id, code, tracking_method, 'adapter.quietcourier.jobs' FROM providers WHERE code = 'quietcourier'",
    )
    .bind(Uuid::now_v7())
    .execute(&pool)
    .await
    .unwrap();
    onboarding::open_build_tasks(&pool).await.unwrap();
    assert_eq!(status(&pool, "quietcourier").await, "live");
}

// ----------------------------------------------------------------------------- logins

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn a_login_given_with_the_courier_is_stored_encrypted_for_the_organization(pool: PgPool) {
    let (_, key) = common::user_of(&pool, "a@acme.example.com", "Acme").await;
    let app = app_with_store(&pool);
    let mut body = courier("Portal Courier");
    body["login"] = json!({ "email": "ops@acme.example.com", "password": PASSWORD });
    let reply = add(&app, &key, body).await;
    assert_eq!(reply.status, 202, "{:?}", reply.body);
    assert!(!reply.body.to_string().contains(PASSWORD));

    let acme = common::organization(&pool, "Acme").await;
    let (org, needs): (Option<Uuid>, bool) = sqlx::query_as(
        "SELECT c.organization_id, p.requires_credential FROM provider_credentials c
           JOIN providers p ON p.id = c.provider_id WHERE p.code = 'portalcourier'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!((org, needs), (Some(acme), true));
    // The AI is told a login exists, never what it is.
    let tasks = tasks(&pool, "portalcourier").await;
    assert_eq!(tasks[0].2["login"], true);
    assert!(!tasks[0].2.to_string().contains(PASSWORD));
    let audit: String = sqlx::query_scalar("SELECT string_agg(details::text, ' ') FROM audit_log")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(!audit.contains(PASSWORD));

    // Without a master key a login is refused before anything is added.
    let mut body = courier("Other Courier");
    body["login"] = json!({ "password": PASSWORD });
    let reply = add(&common::app(&pool), &key, body).await;
    assert_eq!(reply.status, 503);
    assert_eq!(status_or_none(&pool, "othercourier").await, None);
}

async fn status_or_none(pool: &PgPool, code: &str) -> Option<String> {
    sqlx::query_scalar("SELECT status FROM providers WHERE code = $1")
        .bind(code)
        .fetch_optional(pool)
        .await
        .unwrap()
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn an_organizations_own_login_is_used_for_its_lookups_only(pool: PgPool) {
    let platform = credentials::set_credential(
        &pool,
        &store(),
        "dhl",
        "platform",
        json!({ "api_key": "platform-key" }).as_object().unwrap(),
    )
    .await
    .unwrap();
    let (_, acme_key) = common::user_of(&pool, "a@acme.example.com", "Acme").await;
    let (_, globex_key) = common::user_of(&pool, "g@globex.example.com", "Globex").await;
    let app = app_with_store(&pool);

    // Acme stores its own login.
    let reply = call(
        &app,
        Method::PUT,
        "/v1/providers/dhl/login",
        Some(&acme_key),
        Some(json!({ "login": { "api_key": PASSWORD } })),
    )
    .await;
    assert_eq!(reply.status, 204, "{:?}", reply.body);

    // The same number for both: two lookups, one with each login.
    assert_eq!(
        track(&app, &acme_key, "dhl", "1234567890").await.status,
        201
    );
    assert_eq!(
        track(&app, &globex_key, "dhl", "1234567890").await.status,
        201
    );
    assert_eq!(count(&pool, "shipments").await, 2);
    let acme = common::organization(&pool, "Acme").await;
    let rows: Vec<(String, Option<String>)> = sqlx::query_as(
        "SELECT s.access_scope, o.payload -> 'payload' ->> 'credential_ref'
           FROM outbox_messages o
           JOIN tracking_jobs j ON j.id = (o.payload -> 'payload' ->> 'job_id')::uuid
           JOIN shipments s ON s.id = j.shipment_id ORDER BY s.access_scope",
    )
    .fetch_all(&pool)
    .await
    .unwrap();
    let acme_ref: Uuid = sqlx::query_scalar(
        "SELECT id FROM provider_credentials WHERE organization_id = $1 AND revoked_at IS NULL",
    )
    .bind(acme)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        rows,
        vec![
            (format!("org:{acme}"), Some(acme_ref.to_string())),
            ("public".into(), Some(platform.to_string())),
        ]
    );

    // The worker gets Acme's login for Acme's job only.
    let token = worker_tokens::issue(&pool, "dhl", Some("t"))
        .await
        .unwrap()
        .unwrap();
    let worker = worker_tokens::authenticate(&pool, &token)
        .await
        .unwrap()
        .unwrap();
    let jobs: Vec<(Uuid, String)> = sqlx::query_as(
        "SELECT j.id, s.access_scope FROM tracking_jobs j JOIN shipments s ON s.id = j.shipment_id
          ORDER BY s.access_scope",
    )
    .fetch_all(&pool)
    .await
    .unwrap();
    let (acme_job, public_job) = (jobs[0].0, jobs[1].0);
    let resolve = |credential_ref, job_id| {
        let pool = pool.clone();
        let worker = worker.clone();
        async move {
            match credentials::resolve(
                &pool,
                &store(),
                &worker,
                ResolveRequest {
                    credential_ref,
                    job_id,
                    attempt: 1,
                },
            )
            .await
            .unwrap()
            {
                Resolved::Secret(fields) => fields["api_key"].as_str().unwrap().to_string(),
                Resolved::Denied(reason) => panic!("denied: {reason}"),
            }
        }
    };
    // Whatever reference is sent, a job gets the login of its own shipment's scope.
    assert_eq!(resolve(acme_ref, acme_job).await, PASSWORD);
    assert_eq!(resolve(platform, acme_job).await, PASSWORD);
    assert_eq!(resolve(platform, public_job).await, "platform-key");
    assert_eq!(resolve(acme_ref, public_job).await, "platform-key");

    // Nobody reads a login back.
    let list = get(&app, "/v1/providers", &acme_key).await;
    assert!(!list.body.to_string().contains(PASSWORD));
}

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn without_a_login_of_its_own_an_organization_uses_the_platforms(pool: PgPool) {
    let (_, key) = common::user_of(&pool, "a@acme.example.com", "Acme").await;
    let app = app_with_store(&pool);
    // DHL needs a login and nobody has stored one: refused as before.
    assert_eq!(track(&app, &key, "dhl", "1234567890").await.status, 409);
    credentials::set_credential(
        &pool,
        &store(),
        "dhl",
        "platform",
        json!({ "api_key": "platform-key" }).as_object().unwrap(),
    )
    .await
    .unwrap();
    assert_eq!(track(&app, &key, "dhl", "1234567890").await.status, 201);
    let scope: String = sqlx::query_scalar("SELECT access_scope FROM shipments")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(scope, "public");
}

// ----------------------------------------------------------------------------- the agent's login

const REPAIR_TOKEN: &str = "repair-token-0123456789-0123456789-abcdef";

#[sqlx::test(migrator = "db::MIGRATOR")]
async fn the_repair_service_gets_the_login_only_while_its_task_runs(pool: PgPool) {
    let (_, key) = common::user_of(&pool, "a@acme.example.com", "Acme").await;
    let mut body = courier("Portal Courier");
    body["login"] = json!({ "email": "ops@acme.example.com", "password": PASSWORD });
    assert_eq!(add(&app_with_store(&pool), &key, body).await.status, 202);
    let task: Uuid = sqlx::query_scalar("SELECT id FROM repair_tasks")
        .fetch_one(&pool)
        .await
        .unwrap();
    let mut config = Config::for_tests();
    config.engine_repair_token = Some(REPAIR_TOKEN.into());
    let internal = api::internal::router(AppState::new(pool.clone(), config).with_secrets(store()));
    let path = format!("/internal/v1/repairs/{task}/login");

    // Pending: not yet.
    let reply = call(&internal, Method::POST, &path, Some(REPAIR_TOKEN), None).await;
    assert_eq!(reply.status, 204);

    // Running: yes, and the hand-out is audited (without the value).
    let claimed = call(
        &internal,
        Method::POST,
        "/internal/v1/repairs/claim",
        Some(REPAIR_TOKEN),
        None,
    )
    .await;
    assert_eq!(claimed.status, 200);
    let reply = call(&internal, Method::POST, &path, Some(REPAIR_TOKEN), None).await;
    assert_eq!(reply.status, 200);
    assert_eq!(reply.body["login"]["password"], PASSWORD);
    assert_eq!(reply.headers["cache-control"], "no-store");
    let audited: (String, bool, String) = sqlx::query_as(
        "SELECT actor_type, outcome = 'allowed', details::text FROM audit_log WHERE action = 'credential.agent_use'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!((audited.0.as_str(), audited.1), ("ai_repair", true));
    assert!(!audited.2.contains(PASSWORD));

    // A worker token or a user key gets nothing; neither does anyone after the task ended.
    let worker = worker_tokens::issue(&pool, "dhl", Some("t"))
        .await
        .unwrap()
        .unwrap();
    for wrong in [worker.as_str(), key.as_str()] {
        let reply = call(&internal, Method::POST, &path, Some(wrong), None).await;
        assert_eq!(reply.status, 401);
    }
    sqlx::query("UPDATE repair_tasks SET status = 'failed', finished_at = now()")
        .execute(&pool)
        .await
        .unwrap();
    let reply = call(&internal, Method::POST, &path, Some(REPAIR_TOKEN), None).await;
    assert_eq!(reply.status, 204);
}
