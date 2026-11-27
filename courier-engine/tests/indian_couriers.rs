//! The Indian couriers of migration 0013 (SkyKing removed again by 0019): seeded, mapped,
//! and unusable until an operator has supplied what each needs.

mod common;

use axum::http::Method;
use common::call;
use courier_engine::services::{
    credentials::{self, PgSecretStore},
    crypto::MasterKey,
};
use serde_json::json;
use sqlx::PgPool;

use courier_engine::db::MIGRATOR;

const API: [&str; 2] = ["delhivery", "safexpress"];
const WEBSITES: [&str; 2] = ["abt", "balaji"];

/// code, method, adapter key, queue, requires_credential, scraping_authorized, is_test, poll interval
type Seeded = (String, String, String, String, bool, bool, bool, i32);

async fn request(pool: &PgPool, key: &str, provider: &str, number: &str) -> common::Reply {
    call(
        &common::app(pool),
        Method::POST,
        "/v1/tracking-requests",
        Some(key),
        Some(json!({ "provider_code": provider, "tracking_number": number })),
    )
    .await
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn each_courier_has_one_adapter_its_own_queue_and_no_polling(pool: PgPool) {
    let rows: Vec<Seeded> = sqlx::query_as(
        "SELECT p.code, p.tracking_method::text, a.key, a.queue_name, p.requires_credential,
                p.scraping_authorized, p.is_test, p.poll_interval_ms
           FROM providers p JOIN adapters a ON a.provider_id = p.id
          WHERE p.code = ANY($1) ORDER BY p.code",
    )
    .bind([API.as_slice(), WEBSITES.as_slice()].concat())
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(rows.len(), API.len() + WEBSITES.len());
    for (code, method, key, queue, needs_credential, authorized, is_test, poll) in rows {
        assert_eq!(key, code);
        assert_eq!(queue, format!("adapter.{code}.jobs"));
        assert!(!is_test, "{code} is a real courier");
        assert!(!authorized, "{code}: nobody has recorded a permission yet");
        assert_eq!(poll, 0, "{code} is not polled until an operator says so");
        let api = API.contains(&code.as_str());
        assert_eq!(method, if api { "api" } else { "scrape" }, "{code}");
        assert_eq!(needs_credential, api, "{code}");
    }
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_api_courier_is_refused_until_its_credential_is_stored(pool: PgPool) {
    let (_, key) = common::user(&pool, "api@example.com").await;
    for provider in API {
        let refused = request(&pool, &key, provider, "1234567890").await;
        assert_eq!(refused.status, 409, "{provider}: {:?}", refused.body);
    }
    let store = PgSecretStore::new(MasterKey::from_hex(&"5a".repeat(32), 1).unwrap());
    let secret = json!({ "token": "t" }).as_object().unwrap().clone();
    credentials::set_credential(&pool, &store, "delhivery", "test", &secret)
        .await
        .unwrap();
    let accepted = request(&pool, &key, "delhivery", "1234567890").await;
    assert_eq!(accepted.status, 201, "{:?}", accepted.body);
    // The other courier's credential is its own.
    assert_eq!(
        request(&pool, &key, "safexpress", "1234567890")
            .await
            .status,
        409
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_website_courier_is_refused_until_its_owners_permission_is_recorded(pool: PgPool) {
    let (_, key) = common::user(&pool, "web@example.com").await;
    for provider in WEBSITES {
        let refused = request(&pool, &key, provider, "LR12345").await;
        assert_eq!(refused.status, 409, "{provider}: {:?}", refused.body);
    }
    sqlx::query("UPDATE providers SET scraping_authorized = true WHERE code = 'balaji'")
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(request(&pool, &key, "balaji", "LR12345").await.status, 201);
    assert_eq!(request(&pool, &key, "abt", "LR12345").await.status, 409);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn every_status_code_the_adapters_produce_has_a_mapping(pool: PgPool) {
    let mapped = |code: &'static str| {
        let pool = pool.clone();
        async move {
            sqlx::query_as::<_, (String, String)>(
                "SELECT m.raw_status, m.normalized_status::text FROM provider_status_mappings m
                   JOIN providers p ON p.id = m.provider_id WHERE p.code = $1 ORDER BY m.raw_status",
            )
            .bind(code)
            .fetch_all(&pool)
            .await
            .unwrap()
        }
    };
    // courier-adapters/adapters/india.py: status_code()
    let codes = [
        ("booked", "CREATED"),
        ("delivered", "DELIVERED"),
        ("in-transit", "IN_TRANSIT"),
        ("out-for-delivery", "OUT_FOR_DELIVERY"),
        ("picked-up", "PICKED_UP"),
        ("returned", "RETURNED"),
        ("undelivered", "EXCEPTION"),
    ]
    .map(|(raw, normalized)| (raw.to_string(), normalized.to_string()));
    for provider in ["abt", "balaji", "safexpress"] {
        assert_eq!(mapped(provider).await, codes, "{provider}");
    }
    let delhivery = mapped("delhivery").await;
    assert_eq!(delhivery.len(), 13);
    for (raw, normalized) in [
        ("UD:In Transit", "IN_TRANSIT"),
        ("RT:In Transit", "RETURNED"),
        ("UD:Dispatched", "OUT_FOR_DELIVERY"),
        ("DL:Delivered", "DELIVERED"),
    ] {
        assert!(
            delhivery.contains(&(raw.to_string(), normalized.to_string())),
            "{raw}"
        );
    }
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn skyking_is_gone(pool: PgPool) {
    let (_, key) = common::user(&pool, "web@example.com").await;
    let left: i64 = sqlx::query_scalar(
        "SELECT (SELECT count(*) FROM providers WHERE code = 'skyking')
              + (SELECT count(*) FROM adapters WHERE key = 'skyking')",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(left, 0);
    let refused = request(&pool, &key, "skyking", "LR12345").await;
    assert!(refused.status.is_client_error(), "{:?}", refused.body);
}
