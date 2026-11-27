//! Phase 11: infrastructure/test-data.sql, loaded straight into the database as a tester
//! does, works with the Engine: organizations, roles, shared lookups, separate views.

mod common;

use axum::http::Method;
use common::{call, get};
use courier_engine::{db, services::api_keys};
use sqlx::PgPool;
use uuid::Uuid;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;
const SEED: &str = include_str!("../../infrastructure/test-data.sql");

async fn key(pool: &PgPool, email: &str) -> String {
    let id = db::api_keys::find_user_id_by_email(pool, email)
        .await
        .unwrap()
        .expect("seeded user");
    api_keys::issue(pool, id, Some("test-data")).await.unwrap()
}

async fn ids(pool: &PgPool, key: &str) -> Vec<String> {
    let reply = get(&common::app(pool), "/v1/tracking-requests", key).await;
    assert_eq!(reply.status, 200);
    let mut found: Vec<String> = reply.body["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["reference"].as_str().unwrap_or_default().to_string())
        .collect();
    found.sort();
    found
}

async fn refresh(pool: &PgPool, key: &str, request: &str) -> common::Reply {
    call(
        &common::app(pool),
        Method::POST,
        &format!("/v1/tracking-requests/{request}/refresh"),
        Some(key),
        None,
    )
    .await
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_test_data_loads_twice_and_each_user_sees_only_their_own(pool: PgPool) {
    sqlx::raw_sql(SEED).execute(&pool).await.unwrap();
    sqlx::raw_sql(SEED)
        .execute(&pool)
        .await
        .expect("loading again changes nothing");
    let users: i64 = sqlx::query_scalar("SELECT count(*) FROM users")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(users, 5);

    let asha = key(&pool, "asha@acme.example.com").await;
    let ravi = key(&pool, "ravi@acme.example.com").await;
    let meena = key(&pool, "meena@globex.example.com").await;
    let arun = key(&pool, "arun@globex.example.com").await;
    let ops = key(&pool, "ops@platform.example.com").await;
    assert_eq!(ids(&pool, &asha).await, ["acme-order-1"]);
    assert_eq!(ids(&pool, &ravi).await, ["acme-order-2"]);
    assert_eq!(ids(&pool, &meena).await, ["globex-po-7"]);
    assert_eq!(ids(&pool, &arun).await, ["globex-po-8"]);
    assert!(
        ids(&pool, &ops).await.is_empty(),
        "the admin tracks nothing of their own"
    );

    // Another organization's request does not exist for you.
    let ravis = "01930000-0000-7000-8000-00000000d002";
    assert_eq!(refresh(&pool, &meena, ravis).await.status, 404);

    // The shared shipment gets one lookup, whoever starts it.
    assert_eq!(refresh(&pool, &ravi, ravis).await.status, 202);
    let meenas = "01930000-0000-7000-8000-00000000d003";
    let again = refresh(&pool, &meena, meenas).await;
    assert_eq!(again.status, 200, "{:?}", again.body);
    assert_eq!(again.body["result"], "job_already_active");
    let jobs: i64 = sqlx::query_scalar("SELECT count(*) FROM tracking_jobs WHERE shipment_id = $1")
        .bind(Uuid::parse_str("01930000-0000-7000-8000-00000000c002").unwrap())
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(jobs, 1);

    // Challenges are for the admin only.
    assert_eq!(
        get(&common::app(&pool), "/v1/interventions", &ops)
            .await
            .status,
        200
    );
    assert_eq!(
        get(&common::app(&pool), "/v1/interventions", &arun)
            .await
            .status,
        403
    );
}
