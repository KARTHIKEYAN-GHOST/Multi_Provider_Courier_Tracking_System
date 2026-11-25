//! API-key authentication (acceptance criteria 5 and 6).

mod common;

use axum::{
    body::Body,
    http::{Method, Request, StatusCode, header},
};
use common::{app, call, get, send, user};
use courier_engine::{db, services::api_keys};
use sha2::{Digest, Sha256};
use sqlx::PgPool;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;

#[sqlx::test(migrator = "MIGRATOR")]
async fn only_the_prefix_and_hash_of_a_key_are_stored(pool: PgPool) {
    let (user_id, key) = user(&pool, "a@example.com").await;

    let (prefix, key_hash, name): (String, Vec<u8>, Option<String>) =
        sqlx::query_as("SELECT prefix, key_hash, name FROM api_keys WHERE user_id = $1")
            .bind(user_id)
            .fetch_one(&pool)
            .await
            .unwrap();

    assert_eq!(key_hash, Sha256::digest(key.as_bytes()).to_vec());
    assert_eq!(api_keys::parse_prefix(&key), Some(prefix.as_str()));

    // Neither the key nor its secret part appears anywhere in the row.
    let secret = key.rsplit('_').next().unwrap();
    let row_text: String =
        sqlx::query_scalar("SELECT row_to_json(k)::text FROM api_keys k WHERE user_id = $1")
            .bind(user_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert!(!row_text.contains(secret));
    assert!(!row_text.contains(&key));
    assert_eq!(name.as_deref(), Some("test"));
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_valid_key_is_accepted(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let reply = get(&app(&pool), "/v1/providers", &key).await;
    assert_eq!(reply.status, StatusCode::OK);
    let codes: Vec<&str> = reply.body["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p["code"].as_str().unwrap())
        .collect();
    assert_eq!(
        codes,
        [
            "abt",
            "balaji",
            "delhivery",
            "demosite",
            "dhl",
            "dummy",
            "safexpress",
            "skyking"
        ]
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn missing_malformed_and_unknown_keys_are_rejected_with_401(pool: PgPool) {
    let (_, key) = user(&pool, "a@example.com").await;
    let app = app(&pool);

    // No Authorization header at all.
    let reply = call(&app, Method::GET, "/v1/providers", None, None).await;
    assert_eq!(reply.status, StatusCode::UNAUTHORIZED);
    assert_eq!(reply.body["error"]["code"], "unauthorized");

    // Right prefix, wrong secret: the stored hash must not match.
    let mut wrong_secret = key.clone();
    let last = wrong_secret.pop().unwrap();
    wrong_secret.push(if last == '0' { '1' } else { '0' });

    let well_formed_but_unknown = api_keys::generate().full;

    let bad_keys = [
        String::new(),
        "not-a-key".to_string(),
        key.to_uppercase(),
        format!("{key}x"),
        key[..key.len() - 1].to_string(),
        wrong_secret,
        well_formed_but_unknown,
    ];
    for bad in &bad_keys {
        let reply = get(&app, "/v1/providers", bad).await;
        assert_eq!(
            reply.status,
            StatusCode::UNAUTHORIZED,
            "key {bad:?} must be rejected"
        );
    }

    // Wrong scheme, even with a valid key.
    let request = Request::builder()
        .uri("/v1/providers")
        .header(header::AUTHORIZATION, format!("Basic {key}"))
        .body(Body::empty())
        .unwrap();
    assert_eq!(send(&app, request).await.status, StatusCode::UNAUTHORIZED);

    // And the genuine key still works after all those failures.
    assert_eq!(
        get(&app, "/v1/providers", &key).await.status,
        StatusCode::OK
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_revoked_key_is_rejected_and_other_keys_keep_working(pool: PgPool) {
    let (user_id, first) = user(&pool, "a@example.com").await;
    let second = api_keys::issue(&pool, user_id, Some("second"))
        .await
        .unwrap();
    let app = app(&pool);

    let prefix = api_keys::parse_prefix(&first).unwrap();
    assert!(db::api_keys::revoke_by_prefix(&pool, prefix).await.unwrap());
    // Revoking twice reports that nothing was active.
    assert!(!db::api_keys::revoke_by_prefix(&pool, prefix).await.unwrap());

    assert_eq!(
        get(&app, "/v1/providers", &first).await.status,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        get(&app, "/v1/providers", &second).await.status,
        StatusCode::OK
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_disabled_users_key_is_rejected(pool: PgPool) {
    let (user_id, key) = user(&pool, "a@example.com").await;
    sqlx::query("UPDATE users SET status = 'disabled' WHERE id = $1")
        .bind(user_id)
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(
        get(&app(&pool), "/v1/providers", &key).await.status,
        StatusCode::UNAUTHORIZED
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn every_v1_route_requires_a_key(pool: PgPool) {
    let app = app(&pool);
    let id = "019355a0-1c2d-7e3f-8a4b-5c6d7e8f9a01";
    let routes = [
        (Method::GET, "/v1/providers".to_string()),
        (Method::GET, "/v1/tracking-requests".to_string()),
        (Method::POST, "/v1/tracking-requests".to_string()),
        (Method::GET, format!("/v1/tracking-requests/{id}")),
        (Method::DELETE, format!("/v1/tracking-requests/{id}")),
        (Method::GET, format!("/v1/tracking-requests/{id}/events")),
        (Method::GET, format!("/v1/tracking-requests/{id}/jobs")),
        (Method::POST, format!("/v1/tracking-requests/{id}/refresh")),
    ];
    for (method, path) in routes {
        let reply = call(&app, method.clone(), &path, None, None).await;
        assert_eq!(reply.status, StatusCode::UNAUTHORIZED, "{method} {path}");
    }
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn using_a_key_records_when_it_was_last_used(pool: PgPool) {
    let (user_id, key) = user(&pool, "a@example.com").await;
    let last_used = || async {
        sqlx::query_scalar::<_, Option<chrono::DateTime<chrono::Utc>>>(
            "SELECT last_used_at FROM api_keys WHERE user_id = $1",
        )
        .bind(user_id)
        .fetch_one(&pool)
        .await
        .unwrap()
    };
    assert!(last_used().await.is_none());
    get(&app(&pool), "/v1/providers", &key).await;
    assert!(last_used().await.is_some());
}

// ----------------------------------------------------------------------------- key issue

/// A generator that hands out `first` (whose prefix is already taken) `collisions` times,
/// then real keys.
fn colliding_generator(
    taken: api_keys::GeneratedKey,
    collisions: usize,
    calls: std::sync::Arc<std::sync::atomic::AtomicUsize>,
) -> impl FnMut() -> api_keys::GeneratedKey {
    use std::sync::atomic::Ordering;
    move || {
        if calls.fetch_add(1, Ordering::SeqCst) < collisions {
            taken.clone()
        } else {
            api_keys::generate()
        }
    }
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_prefix_collision_is_retried_with_a_new_key(pool: PgPool) {
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    let user_id = db::api_keys::create_user(&pool, "a@example.com")
        .await
        .unwrap();
    let existing = api_keys::generate();
    let existing_key = api_keys::issue_with(&pool, user_id, None, {
        let k = existing.clone();
        move || k.clone()
    })
    .await
    .unwrap();

    let calls = Arc::new(AtomicUsize::new(0));
    let issued = api_keys::issue_with(
        &pool,
        user_id,
        None,
        colliding_generator(existing.clone(), 2, calls.clone()),
    )
    .await
    .expect("the collision is retried");

    assert_eq!(
        calls.load(Ordering::SeqCst),
        3,
        "two collisions, then success"
    );
    assert_ne!(issued, existing_key);
    assert!(
        api_keys::authenticate(&pool, &issued)
            .await
            .unwrap()
            .is_some()
    );
    assert!(
        api_keys::authenticate(&pool, &existing_key)
            .await
            .unwrap()
            .is_some(),
        "the key that owned the prefix is untouched"
    );
    let keys: i64 = sqlx::query_scalar("SELECT count(*) FROM api_keys")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(keys, 2);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn key_issue_gives_up_after_a_bounded_number_of_collisions(pool: PgPool) {
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    let user_id = db::api_keys::create_user(&pool, "a@example.com")
        .await
        .unwrap();
    let existing = api_keys::generate();
    api_keys::issue_with(&pool, user_id, None, {
        let k = existing.clone();
        move || k.clone()
    })
    .await
    .unwrap();

    let calls = Arc::new(AtomicUsize::new(0));
    let result = api_keys::issue_with(
        &pool,
        user_id,
        None,
        colliding_generator(existing, usize::MAX, calls.clone()),
    )
    .await;
    assert!(result.is_err(), "it must not loop forever");
    assert_eq!(calls.load(Ordering::SeqCst), 5);
}

#[test]
fn generated_keys_are_distinct_and_well_formed() {
    let keys: Vec<_> = (0..200).map(|_| api_keys::generate()).collect();
    let unique: std::collections::HashSet<_> = keys.iter().map(|k| k.full.clone()).collect();
    assert_eq!(unique.len(), keys.len());
    for key in &keys {
        let parts: Vec<_> = key.full.split('_').collect();
        assert_eq!(parts.len(), 3);
        assert_eq!(parts[0], "ctk");
        assert_eq!(parts[1], key.prefix);
        assert_eq!(parts[1].len(), 12);
        assert_eq!(parts[2].len(), 64, "32 random bytes = 256 bits");
        assert!(
            parts[2]
                .chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        );
        assert_ne!(parts[2], "0".repeat(64));
    }
}
