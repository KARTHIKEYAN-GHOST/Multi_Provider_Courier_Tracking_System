//! Simultaneous requests for the same shipment (acceptance criteria 11, 12, 13).
//!
//! All tasks are released together by a barrier and each runs its own database transaction,
//! so the shipment row lock and the partial unique indexes are exercised for real.

mod common;

use std::sync::Arc;

use axum::http::{Method, StatusCode};
use common::{app, call, complete_all_jobs, count, track, user};
use courier_engine::db;
use sqlx::PgPool;
use tokio::sync::Barrier;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;

/// Runs one POST per key in `keys`, all starting at the same instant.
async fn post_simultaneously(
    pool: &PgPool,
    keys: Vec<String>,
    tracking_number: &'static str,
) -> Vec<common::Reply> {
    let app = app(pool);
    let barrier = Arc::new(Barrier::new(keys.len()));
    let mut tasks = Vec::new();
    for key in keys {
        let (app, barrier) = (app.clone(), barrier.clone());
        tasks.push(tokio::spawn(async move {
            barrier.wait().await;
            track(&app, &key, tracking_number).await
        }));
    }
    let mut replies = Vec::new();
    for task in tasks {
        replies.push(task.await.unwrap());
    }
    replies
}

fn statuses(replies: &[common::Reply], status: StatusCode) -> usize {
    replies.iter().filter(|r| r.status == status).count()
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn twenty_identical_requests_from_one_user_create_everything_exactly_once(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;

    let replies = post_simultaneously(&pool, vec![key; 20], "DUMMY-OK-1").await;

    assert!(
        replies.iter().all(|r| r.status.is_success()),
        "no request may fail"
    );
    assert_eq!(
        statuses(&replies, StatusCode::CREATED),
        1,
        "exactly one request creates the subscription"
    );
    assert_eq!(statuses(&replies, StatusCode::OK), 19);

    let first_id = replies[0].id();
    assert!(
        replies.iter().all(|r| r.id() == first_id),
        "every reply names the same request"
    );
    let first_job = replies[0].body["latest_job"]["id"].clone();
    assert!(
        replies
            .iter()
            .all(|r| r.body["latest_job"]["id"] == first_job)
    );

    assert_eq!(count(&pool, "active_shipments").await, 1);
    assert_eq!(count(&pool, "shipments").await, 1);
    assert_eq!(count(&pool, "active_requests").await, 1);
    assert_eq!(count(&pool, "tracking_requests").await, 1);
    assert_eq!(count(&pool, "tracking_jobs").await, 1);
    assert_eq!(count(&pool, "job_attempts").await, 1);
    assert_eq!(count(&pool, "outbox_messages").await, 1);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn ten_users_requesting_the_same_shipment_at_once_share_one_shipment_and_one_job(
    pool: PgPool,
) {
    let mut keys = Vec::new();
    for n in 0..10 {
        let (_, key) = user(&pool, &format!("user{n}@example.com")).await;
        // Each user sends the request twice.
        keys.push(key.clone());
        keys.push(key);
    }

    let replies = post_simultaneously(&pool, keys, "DUMMY-OK-1").await;

    assert!(
        replies.iter().all(|r| r.status.is_success()),
        "no request may fail"
    );
    assert_eq!(
        statuses(&replies, StatusCode::CREATED),
        10,
        "one new subscription per user"
    );
    assert_eq!(statuses(&replies, StatusCode::OK), 10);

    let mut request_ids: Vec<String> = replies.iter().map(|r| r.id()).collect();
    request_ids.sort();
    request_ids.dedup();
    assert_eq!(request_ids.len(), 10, "ten distinct subscriptions");

    let first_job = replies[0].body["latest_job"]["id"].clone();
    assert!(first_job.is_string());
    assert!(
        replies
            .iter()
            .all(|r| r.body["latest_job"]["id"] == first_job),
        "one shared job"
    );

    assert_eq!(count(&pool, "shipments").await, 1);
    assert_eq!(count(&pool, "active_requests").await, 10);
    assert_eq!(count(&pool, "tracking_jobs").await, 1);
    assert_eq!(count(&pool, "job_attempts").await, 1);
    assert_eq!(count(&pool, "outbox_messages").await, 1);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn twenty_simultaneous_refreshes_create_exactly_one_new_job(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);
    let request_id = track(&app, &key, "DUMMY-TRANSIT-1").await.id();
    complete_all_jobs(&pool).await; // no active job remains
    let path = format!("/v1/tracking-requests/{request_id}/refresh");

    let barrier = Arc::new(Barrier::new(20));
    let mut tasks = Vec::new();
    for _ in 0..20 {
        let (app, barrier, key, path) = (app.clone(), barrier.clone(), key.clone(), path.clone());
        tasks.push(tokio::spawn(async move {
            barrier.wait().await;
            call(&app, Method::POST, &path, Some(&key), None).await
        }));
    }
    let mut replies = Vec::new();
    for task in tasks {
        replies.push(task.await.unwrap());
    }

    assert!(
        replies.iter().all(|r| r.status.is_success()),
        "no request may fail"
    );
    assert_eq!(
        statuses(&replies, StatusCode::ACCEPTED),
        1,
        "exactly one refresh creates a job"
    );
    assert_eq!(statuses(&replies, StatusCode::OK), 19);
    let new_job = replies[0].body["job"]["id"].clone();
    assert!(
        replies.iter().all(|r| r.body["job"]["id"] == new_job),
        "all report the same new job"
    );

    assert_eq!(
        count(&pool, "tracking_jobs").await,
        2,
        "the completed job plus one new job"
    );
    assert_eq!(count(&pool, "job_attempts").await, 2);
    assert_eq!(count(&pool, "outbox_messages").await, 2);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_database_itself_refuses_duplicates_even_without_the_application_logic(pool: PgPool) {
    let (user_id, key) = user(&pool, "a@example.com").await;
    track(&app(&pool), &key, "DUMMY-OK-1").await;
    let (shipment_id, provider_id, adapter_id): (uuid::Uuid, uuid::Uuid, uuid::Uuid) = sqlx::query_as(
        "SELECT s.id, s.provider_id, j.adapter_id FROM shipments s JOIN tracking_jobs j ON j.shipment_id = s.id",
    )
    .fetch_one(&pool)
    .await
    .unwrap();

    // A second active shipment for the same provider + number.
    let duplicate_shipment = sqlx::query(
        "INSERT INTO shipments (id, provider_id, tracking_number) VALUES ($1, $2, 'DUMMY-OK-1')",
    )
    .bind(uuid::Uuid::now_v7())
    .bind(provider_id)
    .execute(&pool)
    .await;
    assert!(db::is_unique_violation(&duplicate_shipment.unwrap_err()));

    // A second active subscription for the same user + shipment.
    let duplicate_request =
        sqlx::query("INSERT INTO tracking_requests (id, user_id, shipment_id) VALUES ($1, $2, $3)")
            .bind(uuid::Uuid::now_v7())
            .bind(user_id)
            .bind(shipment_id)
            .execute(&pool)
            .await;
    assert!(db::is_unique_violation(&duplicate_request.unwrap_err()));

    // A second non-terminal job for the same shipment.
    let duplicate_job = sqlx::query(
        "INSERT INTO tracking_jobs (id, shipment_id, adapter_id, trigger, status, max_attempts, execution_deadline_at, correlation_id)
         VALUES ($1, $2, $3, 'manual', 'PROCESSING', 3, now(), $1)",
    )
    .bind(uuid::Uuid::now_v7())
    .bind(shipment_id)
    .bind(adapter_id)
    .execute(&pool)
    .await;
    assert!(db::is_unique_violation(&duplicate_job.unwrap_err()));
}
