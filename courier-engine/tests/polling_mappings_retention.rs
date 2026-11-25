//! Phase 7: automatic re-polling, status-mapping management, archiving and retention.
//!
//! Worker results are fed to the handler directly, and time is moved with SQL instead of
//! sleeping, so every scenario is deterministic.

mod common;

use axum::http::Method;
use common::{app, call, track, user};
use courier_engine::{
    db,
    domain::ShipmentStatus,
    services::{
        close_attempt::RetryPolicy,
        events::{Handled, handle_event},
        mappings::{self, Applied},
        polling::{PollStats, poll_due},
        retention::{self, RetentionSettings},
    },
};
use serde_json::{Value, json};
use sqlx::PgPool;
use uuid::Uuid;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;
const POLICY: RetryPolicy = RetryPolicy {
    max_dead_letter_retries: 1,
};
const SETTINGS: RetentionSettings = RetentionSettings {
    retention_days: 180,
    outbox_retention_days: 7,
    inbox_retention_days: 30,
    batch: 2, // small, so the tests also cover "more than one batch"
};

// ----------------------------------------------------------------------------- helpers

async fn exec(pool: &PgPool, sql: &str) -> u64 {
    sqlx::query(sqlx::AssertSqlSafe(sql.to_string()))
        .execute(pool)
        .await
        .unwrap_or_else(|e| panic!("{sql}\n{e}"))
        .rows_affected()
}

async fn scalar<T>(pool: &PgPool, sql: &str) -> T
where
    T: for<'r> sqlx::Decode<'r, sqlx::Postgres> + sqlx::Type<sqlx::Postgres> + Send + Unpin,
{
    sqlx::query_scalar(sqlx::AssertSqlSafe(sql.to_string()))
        .fetch_one(pool)
        .await
        .unwrap_or_else(|e| panic!("{sql}\n{e}"))
}

/// Dummy provider polled every 10 minutes, backing off to at most 80 minutes.
async fn enable_polling(pool: &PgPool) {
    exec(
        pool,
        "UPDATE providers SET poll_interval_ms = 600000, poll_max_interval_ms = 4800000 WHERE code = 'dummy'",
    )
    .await;
}

fn event(raw_status: &str, occurred_at: &str) -> Value {
    json!({ "raw_status": raw_status, "raw_description": format!("{raw_status} happened"),
            "occurred_at": occurred_at })
}

/// Delivers a SUCCESS result with these events for the shipment's open job.
async fn answer(pool: &PgPool, events: Vec<Value>) {
    let (job_id, attempt): (Uuid, i32) = sqlx::query_as(
        "SELECT id, attempt FROM tracking_jobs WHERE status IN ('QUEUED', 'PROCESSING') ORDER BY created_at DESC LIMIT 1",
    )
    .fetch_one(pool)
    .await
    .expect("an open job");
    let execution_id = Uuid::now_v7();
    let body = json!({
        "message_id": Uuid::now_v7(), "type": "job.result", "schema_version": "1.0",
        "occurred_at": "2026-11-24T10:00:00Z", "correlation_id": Uuid::now_v7(),
        "producer": "adapter-dummy@0.1.0",
        "payload": {
            "job_id": job_id, "attempt": attempt, "execution_id": execution_id, "delivery_count": 0,
            "worker_id": "t", "adapter_key": "dummy", "adapter_version": "0.1.0",
            "started_at": "2026-11-24T10:00:00.100Z", "finished_at": "2026-11-24T10:00:00.300Z",
            "outcome": "SUCCESS",
            "raw_response": { "content_type": "application/json", "body": { "n": events.len() } },
            "events": events, "metrics": { "duration_ms": 200 },
        },
    })
    .to_string();
    let handled = handle_event(pool, body.as_bytes(), POLICY).await.unwrap();
    assert!(matches!(handled, Handled::Processed), "{handled:?}");
}

/// Minutes from now until the shipment's next lookup, rounded; None when not scheduled.
async fn next_poll_in_minutes(pool: &PgPool) -> Option<i64> {
    scalar::<Option<f64>>(
        pool,
        "SELECT (extract(epoch FROM next_poll_at - clock_timestamp()) / 60)::float8 FROM shipments WHERE archived_at IS NULL",
    )
    .await
    .map(|m| m.round() as i64)
}

async fn make_poll_due(pool: &PgPool) {
    exec(pool, "UPDATE shipments SET next_poll_at = clock_timestamp() - interval '1 second' WHERE next_poll_at IS NOT NULL").await;
}

async fn jobs(pool: &PgPool) -> Vec<(String, String)> {
    sqlx::query_as("SELECT trigger::text, status::text FROM tracking_jobs ORDER BY created_at, id")
        .fetch_all(pool)
        .await
        .unwrap()
}

async fn status(pool: &PgPool) -> String {
    scalar(
        pool,
        "SELECT current_status::text FROM shipments WHERE archived_at IS NULL",
    )
    .await
}

fn stats(started: usize, postponed: usize, stopped: usize) -> PollStats {
    PollStats {
        started,
        postponed,
        stopped,
    }
}

// ----------------------------------------------------------------------------- polling

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_shipment_in_transit_is_polled_until_it_is_delivered(pool: PgPool) {
    enable_polling(&pool).await;
    let (_, key) = user(&pool, "a@example.com").await;
    let created = track(&app(&pool), &key, "DUMMY-1").await;
    assert_eq!(
        next_poll_in_minutes(&pool).await,
        Some(10),
        "scheduled when the first job is created"
    );
    assert!(created.body["next_check_at"].is_string());

    // Not due yet: nothing happens.
    answer(&pool, vec![event("PKG_CREATED", "2026-11-20T08:00:00Z")]).await;
    assert_eq!(poll_due(&pool, 10).await.unwrap(), stats(0, 0, 0));

    // Due: a poll job is created through the ordinary dispatch path.
    make_poll_due(&pool).await;
    assert_eq!(poll_due(&pool, 10).await.unwrap(), stats(1, 0, 0));
    assert_eq!(
        jobs(&pool).await.last().unwrap(),
        &("poll".to_string(), "QUEUED".to_string())
    );
    let (attempts, outbox): (i64, i64) = (
        scalar(&pool, "SELECT count(*) FROM job_attempts").await,
        scalar(&pool, "SELECT count(*) FROM outbox_messages").await,
    );
    assert_eq!(
        (attempts, outbox),
        (2, 2),
        "a poll has its attempt row and its job.track message"
    );
    assert_eq!(next_poll_in_minutes(&pool).await, Some(10));

    // The poll finds the parcel delivered: polling ends.
    answer(
        &pool,
        vec![
            event("PKG_CREATED", "2026-11-20T08:00:00Z"),
            event("PKG_DELIVERED", "2026-11-21T08:00:00Z"),
        ],
    )
    .await;
    assert_eq!(status(&pool).await, "DELIVERED");
    assert_eq!(next_poll_in_minutes(&pool).await, None);
    make_poll_due(&pool).await;
    assert_eq!(poll_due(&pool, 10).await.unwrap(), stats(0, 0, 0));
    assert_eq!(jobs(&pool).await.len(), 2);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_interval_doubles_while_nothing_changes_and_resets_on_news(pool: PgPool) {
    enable_polling(&pool).await;
    let (_, key) = user(&pool, "a@example.com").await;
    track(&app(&pool), &key, "DUMMY-1").await;
    let first = vec![event("PKG_CREATED", "2026-11-20T08:00:00Z")];
    answer(&pool, first.clone()).await; // new event: back-off 0

    // Each unchanged lookup doubles the interval used for the lookup after it, up to the cap.
    let mut seen = Vec::new();
    for _ in 0..5 {
        make_poll_due(&pool).await;
        assert_eq!(poll_due(&pool, 10).await.unwrap().started, 1);
        seen.push(next_poll_in_minutes(&pool).await.unwrap());
        answer(&pool, first.clone()).await; // same event again: nothing new
    }
    assert_eq!(
        seen,
        [10, 20, 40, 80, 80],
        "10 min base, doubling, capped at 80"
    );
    let unchanged: i32 = scalar(&pool, "SELECT polls_without_change FROM shipments").await;
    assert_eq!(unchanged, 5);

    // News: the next lookup is brought forward to the base interval at once.
    make_poll_due(&pool).await;
    poll_due(&pool, 10).await.unwrap();
    answer(
        &pool,
        vec![
            first[0].clone(),
            event("PKG_IN_TRANSIT", "2026-11-21T08:00:00Z"),
        ],
    )
    .await;
    assert_eq!(next_poll_in_minutes(&pool).await, Some(10));
    let (unchanged, polls): (i32, i32) =
        sqlx::query_as("SELECT polls_without_change, poll_count FROM shipments")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!((unchanged, polls), (0, 7));
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_provider_without_a_poll_policy_is_never_polled(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let created = track(&app(&pool), &key, "DUMMY-1").await;
    assert!(created.body["next_check_at"].is_null());
    answer(&pool, vec![event("PKG_CREATED", "2026-11-20T08:00:00Z")]).await;
    assert_eq!(next_poll_in_minutes(&pool).await, None);
    assert_eq!(poll_due(&pool, 10).await.unwrap(), stats(0, 0, 0));
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn polling_stops_without_subscribers_and_resumes_with_a_new_one(pool: PgPool) {
    enable_polling(&pool).await;
    let app = app(&pool);
    let (_, key) = user(&pool, "a@example.com").await;
    let created = track(&app, &key, "DUMMY-1").await;
    answer(&pool, vec![event("PKG_CREATED", "2026-11-20T08:00:00Z")]).await;

    let path = format!(
        "/v1/tracking-requests/{}",
        created.body["id"].as_str().unwrap()
    );
    assert_eq!(
        call(&app, Method::DELETE, &path, Some(&key), None)
            .await
            .status,
        204
    );
    make_poll_due(&pool).await;
    assert_eq!(poll_due(&pool, 10).await.unwrap(), stats(0, 0, 1));
    assert_eq!(next_poll_in_minutes(&pool).await, None);
    assert_eq!(
        jobs(&pool).await.len(),
        1,
        "nobody is interested: no lookup"
    );

    // Another user subscribes to the same shipment: one lookup now, and polling is on again.
    let (_, other) = user(&pool, "b@example.com").await;
    track(&app, &other, "DUMMY-1").await;
    assert_eq!(jobs(&pool).await.len(), 2);
    assert_eq!(next_poll_in_minutes(&pool).await, Some(10));
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn polling_gives_up_on_a_shipment_that_shows_nothing_new_for_too_long(pool: PgPool) {
    enable_polling(&pool).await;
    let (_, key) = user(&pool, "a@example.com").await;
    track(&app(&pool), &key, "DUMMY-1").await;
    answer(&pool, vec![event("PKG_CREATED", "2026-11-20T08:00:00Z")]).await;

    exec(
        &pool,
        "UPDATE shipments SET last_change_at = now() - interval '29 days'",
    )
    .await;
    make_poll_due(&pool).await;
    assert_eq!(
        poll_due(&pool, 10).await.unwrap(),
        stats(1, 0, 0),
        "29 days: still trying"
    );
    answer(&pool, vec![event("PKG_CREATED", "2026-11-20T08:00:00Z")]).await;

    exec(
        &pool,
        "UPDATE shipments SET last_change_at = now() - interval '31 days'",
    )
    .await;
    make_poll_due(&pool).await;
    assert_eq!(
        poll_due(&pool, 10).await.unwrap(),
        stats(0, 0, 1),
        "31 days: given up"
    );
    assert_eq!(next_poll_in_minutes(&pool).await, None);

    // The user can still ask explicitly, which also puts the shipment back on the schedule.
    let id: Uuid = scalar(&pool, "SELECT id FROM tracking_requests").await;
    let refreshed = call(
        &app(&pool),
        Method::POST,
        &format!("/v1/tracking-requests/{id}/refresh"),
        Some(&key),
        None,
    )
    .await;
    assert_eq!(refreshed.status, 202);
    assert_eq!(
        next_poll_in_minutes(&pool).await,
        Some(20),
        "one unchanged lookup so far: twice the base interval"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_due_poll_waits_while_a_lookup_is_running_or_the_provider_is_unavailable(pool: PgPool) {
    enable_polling(&pool).await;
    let (_, key) = user(&pool, "a@example.com").await;
    track(&app(&pool), &key, "DUMMY-1").await;

    // The first job is still open.
    make_poll_due(&pool).await;
    assert_eq!(poll_due(&pool, 10).await.unwrap(), stats(0, 1, 0));
    assert_eq!(
        jobs(&pool).await.len(),
        1,
        "never two lookups of one shipment at once"
    );
    assert_eq!(next_poll_in_minutes(&pool).await, Some(10));

    answer(&pool, vec![event("PKG_CREATED", "2026-11-20T08:00:00Z")]).await;
    exec(
        &pool,
        "UPDATE providers SET enabled = false WHERE code = 'dummy'",
    )
    .await;
    make_poll_due(&pool).await;
    assert_eq!(poll_due(&pool, 10).await.unwrap(), stats(0, 1, 0));
    assert_eq!(jobs(&pool).await.len(), 1);

    // Available again: polling continues without anybody doing anything.
    exec(
        &pool,
        "UPDATE providers SET enabled = true WHERE code = 'dummy'",
    )
    .await;
    make_poll_due(&pool).await;
    assert_eq!(poll_due(&pool, 10).await.unwrap(), stats(1, 0, 0));
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_manual_refresh_counts_as_a_lookup_and_moves_the_schedule(pool: PgPool) {
    enable_polling(&pool).await;
    let (_, key) = user(&pool, "a@example.com").await;
    let created = track(&app(&pool), &key, "DUMMY-1").await;
    answer(&pool, vec![event("PKG_CREATED", "2026-11-20T08:00:00Z")]).await;
    exec(
        &pool,
        "UPDATE shipments SET next_poll_at = clock_timestamp() + interval '1 minute'",
    )
    .await;

    let path = format!(
        "/v1/tracking-requests/{}/refresh",
        created.body["id"].as_str().unwrap()
    );
    assert_eq!(
        call(&app(&pool), Method::POST, &path, Some(&key), None)
            .await
            .status,
        202
    );
    assert_eq!(
        next_poll_in_minutes(&pool).await,
        Some(10),
        "no poll one minute after a refresh"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn concurrent_schedulers_start_exactly_one_poll_per_shipment(pool: PgPool) {
    enable_polling(&pool).await;
    let (_, key) = user(&pool, "a@example.com").await;
    for n in 0..6 {
        track(&app(&pool), &key, &format!("DUMMY-{n}")).await;
    }
    exec(
        &pool,
        "UPDATE tracking_jobs SET status = 'COMPLETED', outcome = 'SUCCESS', completed_at = now()",
    )
    .await;
    exec(
        &pool,
        "UPDATE job_attempts SET closed_at = now(), outcome = 'SUCCESS', closed_by = 'result'",
    )
    .await;
    make_poll_due(&pool).await;

    let mut tasks = tokio::task::JoinSet::new();
    for _ in 0..8 {
        let pool = pool.clone();
        tasks.spawn(async move { poll_due(&pool, 4).await.unwrap().started });
    }
    let mut started = 0;
    while let Some(result) = tasks.join_next().await {
        started += result.unwrap();
    }
    // Rows another instance holds are skipped, so one round may leave some for the next tick.
    started += poll_due(&pool, 10).await.unwrap().started;
    assert_eq!(started, 6);
    let polls: i64 = scalar(
        &pool,
        "SELECT count(*) FROM tracking_jobs WHERE trigger = 'poll'",
    )
    .await;
    let per_shipment: i64 = scalar(
        &pool,
        "SELECT max(n) FROM (SELECT count(*) AS n FROM tracking_jobs WHERE trigger = 'poll' GROUP BY shipment_id) c",
    )
    .await;
    assert_eq!((polls, per_shipment), (6, 1));
}

// ----------------------------------------------------------------------------- status mappings

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_unmapped_status_is_listed_and_mapping_it_updates_stored_events_and_shipments(
    pool: PgPool,
) {
    enable_polling(&pool).await;
    let (_, key) = user(&pool, "a@example.com").await;
    for number in ["DUMMY-1", "DUMMY-2"] {
        track(&app(&pool), &key, number).await;
        answer(
            &pool,
            vec![
                event("PKG_CREATED", "2026-11-20T08:00:00Z"),
                event("PKG_HANDED_TO_NEIGHBOUR", "2026-11-21T08:00:00Z"),
            ],
        )
        .await;
    }
    let unmapped = mappings::list_unmapped(&pool, None).await.unwrap();
    assert_eq!(unmapped.len(), 1);
    let entry = &unmapped[0];
    assert_eq!(
        (entry.provider_code.as_str(), entry.raw_status.as_str()),
        ("dummy", "PKG_HANDED_TO_NEIGHBOUR")
    );
    assert_eq!((entry.events, entry.shipments), (2, 2));
    assert_eq!(
        entry.sample_description.as_deref(),
        Some("PKG_HANDED_TO_NEIGHBOUR happened")
    );
    assert!(
        mappings::list_unmapped(&pool, Some("dhl"))
            .await
            .unwrap()
            .is_empty()
    );
    let before: Vec<String> = sqlx::query_scalar("SELECT current_status::text FROM shipments")
        .fetch_all(&pool)
        .await
        .unwrap();
    assert_eq!(
        before,
        ["CREATED", "CREATED"],
        "the unknown wording did not move the status"
    );

    let applied = mappings::set_mapping(
        &pool,
        "dummy",
        "PKG_HANDED_TO_NEIGHBOUR",
        ShipmentStatus::Delivered,
        "manual",
    )
    .await
    .unwrap();
    assert_eq!(
        applied,
        Applied {
            events_updated: 2,
            shipments_changed: 2
        }
    );
    let after: Vec<(String, bool, bool)> = sqlx::query_as(
        "SELECT current_status::text, terminal_at IS NOT NULL, next_poll_at IS NULL FROM shipments",
    )
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(
        after,
        vec![("DELIVERED".to_string(), true, true); 2],
        "finished: no more polling"
    );
    assert!(
        mappings::list_unmapped(&pool, None)
            .await
            .unwrap()
            .is_empty()
    );

    // The user sees the corrected history.
    let id: Uuid = scalar(
        &pool,
        "SELECT id FROM tracking_requests ORDER BY id LIMIT 1",
    )
    .await;
    let events = call(
        &app(&pool),
        Method::GET,
        &format!("/v1/tracking-requests/{id}/events"),
        Some(&key),
        None,
    )
    .await;
    assert_eq!(events.body["items"][1]["normalized_status"], "DELIVERED");
    assert_eq!(
        events.body["items"][1]["raw_status"], "PKG_HANDED_TO_NEIGHBOUR",
        "the provider's wording is kept"
    );

    // Applying the same mapping again changes nothing.
    let again = mappings::set_mapping(
        &pool,
        "dummy",
        "PKG_HANDED_TO_NEIGHBOUR",
        ShipmentStatus::Delivered,
        "manual",
    )
    .await
    .unwrap();
    assert_eq!(again, Applied::default());
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn correcting_or_removing_a_mapping_reopens_the_shipment(pool: PgPool) {
    enable_polling(&pool).await;
    let (_, key) = user(&pool, "a@example.com").await;
    track(&app(&pool), &key, "DUMMY-1").await;
    answer(
        &pool,
        vec![
            event("PKG_CREATED", "2026-11-20T08:00:00Z"),
            event("HELD", "2026-11-21T08:00:00Z"),
        ],
    )
    .await;

    // Mapped wrongly to DELIVERED...
    mappings::set_mapping(&pool, "dummy", "HELD", ShipmentStatus::Delivered, "manual")
        .await
        .unwrap();
    assert_eq!(
        (
            status(&pool).await.as_str(),
            next_poll_in_minutes(&pool).await
        ),
        ("DELIVERED", None)
    );

    // ...then corrected: the shipment is open again and back on the polling schedule.
    let applied =
        mappings::set_mapping(&pool, "dummy", "HELD", ShipmentStatus::Exception, "manual")
            .await
            .unwrap();
    assert_eq!(
        applied,
        Applied {
            events_updated: 1,
            shipments_changed: 1
        }
    );
    assert_eq!(status(&pool).await, "EXCEPTION");
    let terminal: bool = scalar(&pool, "SELECT terminal_at IS NOT NULL FROM shipments").await;
    assert!(!terminal);
    assert_eq!(next_poll_in_minutes(&pool).await, Some(10));

    // Removed: the event is UNKNOWN again and the status falls back to the last known one.
    let removed = mappings::remove_mapping(&pool, "dummy", "HELD")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        removed,
        Applied {
            events_updated: 1,
            shipments_changed: 1
        }
    );
    assert_eq!(status(&pool).await, "CREATED");
    assert!(
        mappings::remove_mapping(&pool, "dummy", "HELD")
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(mappings::list_unmapped(&pool, None).await.unwrap().len(), 1);

    let actions: Vec<String> =
        sqlx::query_scalar("SELECT action || ':' || (details ->> 'raw_status') || ':' || COALESCE(details ->> 'previous', '-') FROM audit_log ORDER BY id")
            .fetch_all(&pool)
            .await
            .unwrap();
    assert_eq!(
        actions,
        [
            "status_mapping.set:HELD:-",
            "status_mapping.set:HELD:DELIVERED",
            "status_mapping.remove:HELD:EXCEPTION"
        ],
        "every change is audited with what it replaced"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_mapping_belongs_to_one_provider_and_leaves_archived_shipments_alone(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    track(&app(&pool), &key, "DUMMY-1").await;
    answer(&pool, vec![event("transit", "2026-11-20T08:00:00Z")]).await; // DHL's wording, dummy shipment
    exec(&pool, "UPDATE shipments SET archived_at = now()").await;
    track(&app(&pool), &key, "DUMMY-2").await;
    answer(&pool, vec![event("transit", "2026-11-20T08:00:00Z")]).await;

    // DHL already maps "transit"; that says nothing about the dummy provider.
    let listed: Vec<String> =
        sqlx::query_scalar("SELECT current_status::text FROM shipments ORDER BY id")
            .fetch_all(&pool)
            .await
            .unwrap();
    assert_eq!(listed, ["UNKNOWN", "UNKNOWN"]);

    let applied = mappings::set_mapping(
        &pool,
        "dummy",
        "transit",
        ShipmentStatus::InTransit,
        "manual",
    )
    .await
    .unwrap();
    assert_eq!(
        applied,
        Applied {
            events_updated: 1,
            shipments_changed: 1
        }
    );
    let listed: Vec<String> =
        sqlx::query_scalar("SELECT current_status::text FROM shipments ORDER BY id")
            .fetch_all(&pool)
            .await
            .unwrap();
    assert_eq!(
        listed,
        ["UNKNOWN", "IN_TRANSIT"],
        "the archived generation is closed"
    );

    let dhl = mappings::list_mappings(&pool, Some("dhl")).await.unwrap();
    assert_eq!(dhl.len(), 5);
    assert!(dhl.iter().all(|m| m.provider_code == "dhl"));
    assert!(mappings::list_mappings(&pool, None).await.unwrap().len() > 10);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn invalid_mapping_requests_are_refused(pool: PgPool) {
    let unknown =
        mappings::set_mapping(&pool, "nope", "X", ShipmentStatus::Created, "manual").await;
    assert!(matches!(
        unknown,
        Err(mappings::MappingError::UnknownProvider)
    ));
    for raw in [String::new(), "x".repeat(201)] {
        let invalid =
            mappings::set_mapping(&pool, "dummy", &raw, ShipmentStatus::Created, "manual").await;
        assert!(matches!(
            invalid,
            Err(mappings::MappingError::InvalidRawStatus)
        ));
    }
    let audited: i64 = scalar(&pool, "SELECT count(*) FROM audit_log").await;
    assert_eq!(audited, 0);
}

// ----------------------------------------------------------------------------- archiving

/// One delivered shipment with a subscriber, events and a raw response.
async fn delivered_shipment(pool: &PgPool, key: &str, number: &str) {
    track(&app(pool), key, number).await;
    answer(
        pool,
        vec![
            event("PKG_CREATED", "2026-11-20T08:00:00Z"),
            event("PKG_DELIVERED", "2026-11-21T08:00:00Z"),
        ],
    )
    .await;
}

async fn archived(pool: &PgPool) -> i64 {
    scalar(
        pool,
        "SELECT count(*) FROM shipments WHERE archived_at IS NOT NULL",
    )
    .await
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_finished_shipment_is_archived_after_the_reuse_window_and_the_number_starts_anew(
    pool: PgPool,
) {
    let (_, key) = user(&pool, "a@example.com").await;
    delivered_shipment(&pool, &key, "DUMMY-1").await;

    exec(
        &pool,
        "UPDATE shipments SET terminal_at = now() - interval '89 days'",
    )
    .await;
    assert_eq!(
        retention::archive_due(&pool, 10).await.unwrap(),
        0,
        "the reuse window is 90 days"
    );
    exec(
        &pool,
        "UPDATE shipments SET terminal_at = now() - interval '91 days'",
    )
    .await;
    assert_eq!(retention::archive_due(&pool, 10).await.unwrap(), 1);
    assert_eq!(retention::archive_due(&pool, 10).await.unwrap(), 0, "once");

    // The subscriber still sees the old shipment, marked as archived, with its history.
    let listed = call(
        &app(&pool),
        Method::GET,
        "/v1/tracking-requests",
        Some(&key),
        None,
    )
    .await;
    let old = &listed.body["items"][0];
    assert_eq!(old["shipment_status"], "DELIVERED");
    assert!(old["archived_at"].is_string());
    let events = call(
        &app(&pool),
        Method::GET,
        &format!(
            "/v1/tracking-requests/{}/events",
            old["id"].as_str().unwrap()
        ),
        Some(&key),
        None,
    )
    .await;
    assert_eq!(events.body["items"].as_array().unwrap().len(), 2);

    // The courier reuses the number: a new request is a new shipment with its own lookup.
    let again = track(&app(&pool), &key, "DUMMY-1").await;
    assert_eq!(again.status, 201);
    assert_ne!(again.body["id"], old["id"]);
    assert_eq!(again.body["shipment_status"], "UNKNOWN");
    assert!(again.body["archived_at"].is_null());
    let shipments: i64 = scalar(&pool, "SELECT count(*) FROM shipments").await;
    assert_eq!(shipments, 2);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_abandoned_shipment_is_archived_but_a_watched_or_busy_one_is_not(pool: PgPool) {
    let app = app(&pool);
    let (_, key) = user(&pool, "a@example.com").await;
    let created = track(&app, &key, "DUMMY-1").await;
    answer(&pool, vec![event("PKG_CREATED", "2026-11-20T08:00:00Z")]).await;
    let age = "UPDATE shipments SET created_at = now() - interval '200 days', last_change_at = now() - interval '200 days'";
    exec(&pool, age).await;

    // In transit for 200 days, but somebody is still subscribed: kept.
    assert_eq!(retention::archive_due(&pool, 10).await.unwrap(), 0);

    // The subscriber leaves today: the window counts from the moment they left.
    let path = format!(
        "/v1/tracking-requests/{}",
        created.body["id"].as_str().unwrap()
    );
    call(&app, Method::DELETE, &path, Some(&key), None).await;
    assert_eq!(retention::archive_due(&pool, 10).await.unwrap(), 0);
    exec(
        &pool,
        "UPDATE tracking_requests SET cancelled_at = now() - interval '91 days'",
    )
    .await;

    // A lookup is running: never archived underneath it.
    exec(&pool, "UPDATE tracking_jobs SET status = 'PROCESSING', execution_deadline_at = now() + interval '1 minute', completed_at = NULL, outcome = NULL").await;
    assert_eq!(retention::archive_due(&pool, 10).await.unwrap(), 0);
    exec(&pool, "UPDATE tracking_jobs SET status = 'COMPLETED', outcome = 'SUCCESS', completed_at = now(), execution_deadline_at = NULL").await;

    assert_eq!(retention::archive_due(&pool, 10).await.unwrap(), 1);
    let unscheduled: bool = scalar(&pool, "SELECT next_poll_at IS NULL FROM shipments").await;
    assert!(unscheduled);
}

// ----------------------------------------------------------------------------- retention

#[sqlx::test(migrator = "MIGRATOR")]
async fn events_and_raw_responses_of_long_archived_shipments_are_deleted_and_nothing_else(
    pool: PgPool,
) {
    let (_, key) = user(&pool, "a@example.com").await;
    for n in 0..5 {
        delivered_shipment(&pool, &key, &format!("DUMMY-{n}")).await;
    }
    // 0-2: archived long ago. 3: archived recently. 4: not archived.
    exec(&pool, "UPDATE shipments SET archived_at = now() - interval '181 days' WHERE tracking_number IN ('DUMMY-0', 'DUMMY-1', 'DUMMY-2')").await;
    exec(&pool, "UPDATE shipments SET archived_at = now() - interval '179 days' WHERE tracking_number = 'DUMMY-3'").await;

    let report = retention::run_maintenance(&pool, &SETTINGS).await.unwrap();
    assert_eq!(
        (
            report.shipments_purged,
            report.events_deleted,
            report.raw_responses_deleted,
            report.shipments_archived
        ),
        (3, 6, 3, 0),
        "three shipments in two batches"
    );
    let left: Vec<(String, i64, i64, bool)> = sqlx::query_as(
        "SELECT s.tracking_number,
                (SELECT count(*) FROM tracking_events e WHERE e.shipment_id = s.id),
                (SELECT count(*) FROM raw_tracking_responses r WHERE r.shipment_id = s.id),
                s.purged_at IS NOT NULL
         FROM shipments s ORDER BY s.tracking_number",
    )
    .fetch_all(&pool)
    .await
    .unwrap();
    let expect =
        |n: &str, events: i64, raw: i64, purged: bool| (n.to_string(), events, raw, purged);
    assert_eq!(
        left,
        [
            expect("DUMMY-0", 0, 0, true),
            expect("DUMMY-1", 0, 0, true),
            expect("DUMMY-2", 0, 0, true),
            expect("DUMMY-3", 2, 1, false),
            expect("DUMMY-4", 2, 1, false),
        ]
    );
    // Shipments, jobs and subscriptions stay as the record of what was tracked.
    let (shipments, jobs, requests): (i64, i64, i64) = (
        scalar(&pool, "SELECT count(*) FROM shipments").await,
        scalar(&pool, "SELECT count(*) FROM tracking_jobs").await,
        scalar(
            &pool,
            "SELECT count(*) FROM tracking_requests WHERE status = 'active'",
        )
        .await,
    );
    assert_eq!((shipments, jobs, requests), (5, 5, 5));
    let listed = call(
        &app(&pool),
        Method::GET,
        "/v1/tracking-requests",
        Some(&key),
        None,
    )
    .await;
    assert_eq!(listed.body["items"].as_array().unwrap().len(), 5);
    assert!(
        retention::run_maintenance(&pool, &SETTINGS)
            .await
            .unwrap()
            .is_empty(),
        "a second pass finds nothing"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn retention_zero_keeps_everything(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    delivered_shipment(&pool, &key, "DUMMY-1").await;
    exec(
        &pool,
        "UPDATE shipments SET archived_at = now() - interval '10 years'",
    )
    .await;
    exec(
        &pool,
        "UPDATE outbox_messages SET status = 'sent', sent_at = now() - interval '10 years'",
    )
    .await;
    let keep = RetentionSettings {
        retention_days: 0,
        outbox_retention_days: 0,
        inbox_retention_days: 0,
        batch: 10,
    };
    assert!(
        retention::run_maintenance(&pool, &keep)
            .await
            .unwrap()
            .is_empty()
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn old_sent_messages_and_inbox_rows_are_deleted_but_pending_messages_never(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    for n in 0..6 {
        delivered_shipment(&pool, &key, &format!("DUMMY-{n}")).await; // one outbox + one inbox row each
    }
    let numbered = |column: &str| {
        format!(
            "FROM (SELECT {column} AS key, row_number() OVER (ORDER BY {column}) AS n FROM {}) x",
            if column == "message_id" {
                "outbox_messages"
            } else {
                "processed_messages"
            }
        )
    };
    // Outbox: 1-3 sent long ago, 4 sent yesterday, 5 pending and old, 6 pending.
    exec(&pool, &format!("UPDATE outbox_messages o SET status = 'sent', sent_at = now() - interval '8 days' {} WHERE o.message_id = x.key AND x.n <= 3", numbered("message_id"))).await;
    exec(&pool, &format!("UPDATE outbox_messages o SET status = 'sent', sent_at = now() - interval '1 day' {} WHERE o.message_id = x.key AND x.n = 4", numbered("message_id"))).await;
    exec(&pool, &format!("UPDATE outbox_messages o SET created_at = now() - interval '1 year', available_at = now() - interval '1 year' {} WHERE o.message_id = x.key AND x.n = 5", numbered("message_id"))).await;
    exec(&pool, "UPDATE processed_messages SET processed_at = now() - interval '31 days' WHERE message_id IN (SELECT message_id FROM processed_messages ORDER BY message_id LIMIT 4)").await;

    let report = retention::run_maintenance(&pool, &SETTINGS).await.unwrap();
    assert_eq!(
        (report.outbox_rows_deleted, report.inbox_rows_deleted),
        (3, 4)
    );
    let (sent, pending, inbox): (i64, i64, i64) = (
        scalar(
            &pool,
            "SELECT count(*) FROM outbox_messages WHERE status = 'sent'",
        )
        .await,
        scalar(
            &pool,
            "SELECT count(*) FROM outbox_messages WHERE status = 'pending'",
        )
        .await,
        scalar(&pool, "SELECT count(*) FROM processed_messages").await,
    );
    assert_eq!((sent, pending, inbox), (1, 2, 2));
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn concurrent_maintenance_passes_do_each_piece_of_work_once(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    for n in 0..8 {
        delivered_shipment(&pool, &key, &format!("DUMMY-{n}")).await;
    }
    exec(
        &pool,
        "UPDATE shipments SET terminal_at = now() - interval '91 days'",
    )
    .await;
    let mut tasks = tokio::task::JoinSet::new();
    for _ in 0..4 {
        let pool = pool.clone();
        tasks.spawn(async move {
            retention::run_maintenance(&pool, &SETTINGS)
                .await
                .unwrap()
                .shipments_archived
        });
    }
    let mut total = 0;
    while let Some(result) = tasks.join_next().await {
        total += result.unwrap();
    }
    total += retention::run_maintenance(&pool, &SETTINGS)
        .await
        .unwrap()
        .shipments_archived;
    assert_eq!((total, archived(&pool).await), (8, 8));
}
