//! Database integrity (Phase 2.1, migration 0009).
//!
//! Every test here bypasses the application and sends SQL straight to PostgreSQL, proving the
//! database refuses invalid data on its own. Each rejection is checked by constraint name, so
//! a test cannot pass because of some unrelated error.

mod common;

use common::{app, complete_all_jobs, count, track, user};
use courier_engine::db;
use sqlx::PgPool;

const MIGRATOR: &sqlx::migrate::Migrator = &db::MIGRATOR;

/// One user, one shipment, one QUEUED job with its attempt and outbox message, created
/// through the real API so the starting state is exactly what production writes.
async fn seed(pool: &PgPool) {
    let (_, key) = user(pool, "a@example.com").await;
    let reply = track(&app(pool), &key, "DUMMY-TRANSIT-1").await;
    assert!(reply.status.is_success());
}

fn constraint_of(error: sqlx::Error) -> String {
    match error {
        sqlx::Error::Database(e) => e
            .constraint()
            .unwrap_or_else(|| panic!("rejected without a constraint name: {e}"))
            .to_string(),
        other => panic!("expected a database rejection, got {other}"),
    }
}

/// Runs the statement and returns the name of the constraint that rejected it.
async fn rejected_by(pool: &PgPool, sql: &'static str) -> String {
    match sqlx::query(sql).execute(pool).await {
        Ok(done) => panic!("PostgreSQL accepted ({} rows): {sql}", done.rows_affected()),
        Err(e) => constraint_of(e),
    }
}

/// Runs the statement, which must succeed and touch at least one row.
async fn accepted(pool: &PgPool, sql: &'static str) {
    let done = sqlx::query(sql)
        .execute(pool)
        .await
        .unwrap_or_else(|e| panic!("PostgreSQL rejected: {sql}\n{e}"));
    assert!(done.rows_affected() > 0, "statement matched nothing: {sql}");
}

// ----------------------------------------------------------------------------- job state

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_job_status_requires_its_deadline_column(pool: PgPool) {
    seed(&pool).await;
    assert_eq!(
        rejected_by(&pool, "UPDATE tracking_jobs SET queue_deadline_at = NULL").await,
        "ck_jobs_queued_has_queue_deadline"
    );
    assert_eq!(
        rejected_by(&pool, "UPDATE tracking_jobs SET status = 'PROCESSING'").await,
        "ck_jobs_processing_has_execution_deadline"
    );
    assert_eq!(
        rejected_by(&pool, "UPDATE tracking_jobs SET status = 'RETRYING'").await,
        "ck_jobs_retrying_has_next_attempt"
    );
    // The same transitions with the required column are fine.
    accepted(
        &pool,
        "UPDATE tracking_jobs SET status = 'PROCESSING', execution_deadline_at = now()",
    )
    .await;
    accepted(
        &pool,
        "UPDATE tracking_jobs SET status = 'RETRYING', next_attempt_at = now()",
    )
    .await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_queued_job_cannot_be_inserted_without_a_queue_deadline(pool: PgPool) {
    seed(&pool).await;
    complete_all_jobs(&pool).await;
    assert_eq!(
        rejected_by(
            &pool,
            "INSERT INTO tracking_jobs (id, shipment_id, adapter_id, trigger, status, max_attempts, correlation_id)
             SELECT gen_random_uuid(), s.id, a.id, 'manual', 'QUEUED', 3, gen_random_uuid()
             FROM shipments s, adapters a WHERE a.key = 'dummy'",
        )
        .await,
        "ck_jobs_queued_has_queue_deadline"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn completed_at_is_set_exactly_for_terminal_jobs(pool: PgPool) {
    seed(&pool).await;
    for sql in [
        "UPDATE tracking_jobs SET status = 'COMPLETED', outcome = 'SUCCESS'",
        "UPDATE tracking_jobs SET status = 'FAILED'",
        "UPDATE tracking_jobs SET status = 'CANCELLED'",
        "UPDATE tracking_jobs SET status = 'EXPIRED'",
        // ...and the reverse: a job that is still running cannot carry a completion time.
        "UPDATE tracking_jobs SET completed_at = now()",
    ] {
        assert_eq!(
            rejected_by(&pool, sql).await,
            "ck_jobs_completed_at_matches_terminal",
            "{sql}"
        );
    }
    accepted(
        &pool,
        "UPDATE tracking_jobs SET status = 'FAILED', completed_at = now()",
    )
    .await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_completed_job_needs_a_provider_answer(pool: PgPool) {
    seed(&pool).await;
    for sql in [
        "UPDATE tracking_jobs SET status = 'COMPLETED', completed_at = now()",
        "UPDATE tracking_jobs SET status = 'COMPLETED', completed_at = now(), outcome = 'FAILURE'",
    ] {
        assert_eq!(
            rejected_by(&pool, sql).await,
            "ck_jobs_completed_has_outcome",
            "{sql}"
        );
    }
    accepted(
        &pool,
        "UPDATE tracking_jobs SET status = 'COMPLETED', completed_at = now(), outcome = 'NOT_FOUND'",
    )
    .await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn attempt_and_dead_letter_counters_stay_in_range(pool: PgPool) {
    seed(&pool).await;
    assert_eq!(
        rejected_by(&pool, "UPDATE tracking_jobs SET attempt = max_attempts + 1").await,
        "ck_jobs_attempt_within_max"
    );
    assert_eq!(
        rejected_by(
            &pool,
            "UPDATE tracking_jobs SET max_attempts = 0, attempt = 0"
        )
        .await,
        "tracking_jobs_attempt_check"
    );
    assert_eq!(
        rejected_by(&pool, "UPDATE tracking_jobs SET dead_letter_count = -1").await,
        "ck_jobs_dead_letter_count_non_negative"
    );
}

// ----------------------------------------------------------------------------- job ↔ attempt

const INSERT_JOB_ONLY: &str =
    "INSERT INTO tracking_jobs (id, shipment_id, adapter_id, trigger, status, max_attempts, queue_deadline_at, correlation_id)
     SELECT gen_random_uuid(), s.id, a.id, 'manual', 'QUEUED', 3, now(), gen_random_uuid()
     FROM shipments s, adapters a WHERE a.key = 'dummy' AND s.archived_at IS NULL";

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_job_cannot_be_committed_without_its_attempt_row(pool: PgPool) {
    seed(&pool).await;
    complete_all_jobs(&pool).await;

    // The check is deferred to COMMIT, so the INSERT itself succeeds...
    let mut tx = pool.begin().await.unwrap();
    sqlx::query(INSERT_JOB_ONLY)
        .execute(&mut *tx)
        .await
        .unwrap();
    // ...and the transaction as a whole is refused.
    let error = tx.commit().await.expect_err("commit must fail");
    assert_eq!(constraint_of(error), "job_has_current_attempt");
    assert_eq!(count(&pool, "tracking_jobs").await, 1, "nothing was kept");

    // With the attempt row written in the same transaction, it commits.
    let mut tx = pool.begin().await.unwrap();
    sqlx::query(INSERT_JOB_ONLY)
        .execute(&mut *tx)
        .await
        .unwrap();
    sqlx::query(
        "INSERT INTO job_attempts (id, job_id, attempt, dispatched_at)
         SELECT gen_random_uuid(), j.id, 1, now() FROM tracking_jobs j WHERE j.status = 'QUEUED'",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
    assert_eq!(count(&pool, "tracking_jobs").await, 2);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn advancing_the_attempt_number_requires_the_new_attempt_row(pool: PgPool) {
    seed(&pool).await;
    assert_eq!(
        rejected_by(&pool, "UPDATE tracking_jobs SET attempt = 2").await,
        "job_has_current_attempt"
    );

    // What the Phase 3 retry scheduler will do: bump the number and insert the row together.
    let mut tx = pool.begin().await.unwrap();
    sqlx::query("UPDATE tracking_jobs SET attempt = 2")
        .execute(&mut *tx)
        .await
        .unwrap();
    sqlx::query(
        "INSERT INTO job_attempts (id, job_id, attempt, dispatched_at)
         SELECT gen_random_uuid(), id, 2, now() FROM tracking_jobs",
    )
    .execute(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn the_current_attempt_row_cannot_be_deleted_but_a_whole_job_can(pool: PgPool) {
    seed(&pool).await;
    assert_eq!(
        rejected_by(&pool, "DELETE FROM job_attempts").await,
        "job_has_current_attempt"
    );
    assert_eq!(count(&pool, "job_attempts").await, 1);

    // Deleting the job itself still cascades to its attempts (used by future retention).
    accepted(&pool, "DELETE FROM tracking_jobs").await;
    assert_eq!(count(&pool, "job_attempts").await, 0);
}

// ----------------------------------------------------------------------------- archived shipments

#[sqlx::test(migrator = "MIGRATOR")]
async fn no_job_can_be_inserted_for_an_archived_shipment(pool: PgPool) {
    seed(&pool).await;
    complete_all_jobs(&pool).await;
    accepted(&pool, "UPDATE shipments SET archived_at = now()").await;
    assert_eq!(
        rejected_by(
            &pool,
            "INSERT INTO tracking_jobs (id, shipment_id, adapter_id, trigger, status, max_attempts, queue_deadline_at, correlation_id)
             SELECT gen_random_uuid(), s.id, a.id, 'manual', 'QUEUED', 3, now(), gen_random_uuid()
             FROM shipments s, adapters a WHERE a.key = 'dummy'",
        )
        .await,
        "no_job_on_archived_shipment"
    );
    assert_eq!(count(&pool, "tracking_jobs").await, 1);
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_shipment_with_a_running_job_cannot_be_archived(pool: PgPool) {
    seed(&pool).await;
    assert_eq!(
        rejected_by(&pool, "UPDATE shipments SET archived_at = now()").await,
        "no_archive_with_active_job"
    );
    complete_all_jobs(&pool).await;
    accepted(&pool, "UPDATE shipments SET archived_at = now()").await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn access_scope_accepts_only_the_documented_forms(pool: PgPool) {
    seed(&pool).await;
    for sql in [
        "UPDATE shipments SET access_scope = 'anything'",
        "UPDATE shipments SET access_scope = ''",
        "UPDATE shipments SET access_scope = 'cred:'",
        "UPDATE shipments SET access_scope = 'public '",
    ] {
        assert_eq!(
            rejected_by(&pool, sql).await,
            "ck_shipments_access_scope",
            "{sql}"
        );
    }
    accepted(
        &pool,
        "UPDATE shipments SET access_scope = 'cred:0190abcd-1234'",
    )
    .await;
    accepted(
        &pool,
        "UPDATE shipments SET access_scope = 'verifier:9f2c_AB'",
    )
    .await;
    assert_eq!(
        rejected_by(&pool, "UPDATE shipments SET poll_count = -1").await,
        "ck_shipments_poll_count_non_negative"
    );
}

// ----------------------------------------------------------------------------- requests, outbox

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_request_is_cancelled_exactly_when_it_has_a_cancellation_time(pool: PgPool) {
    seed(&pool).await;
    for sql in [
        "UPDATE tracking_requests SET status = 'cancelled'",
        "UPDATE tracking_requests SET cancelled_at = now()",
    ] {
        assert_eq!(
            rejected_by(&pool, sql).await,
            "ck_requests_cancelled_at_matches_status",
            "{sql}"
        );
    }
    accepted(
        &pool,
        "UPDATE tracking_requests SET status = 'cancelled', cancelled_at = now()",
    )
    .await;
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_outbox_row_must_agree_with_itself_and_with_its_envelope(pool: PgPool) {
    seed(&pool).await;
    for (sql, constraint) in [
        (
            "UPDATE outbox_messages SET status = 'sent'",
            "ck_outbox_sent_at_matches_status",
        ),
        (
            "UPDATE outbox_messages SET sent_at = now()",
            "ck_outbox_sent_at_matches_status",
        ),
        (
            "UPDATE outbox_messages SET message_id = gen_random_uuid()",
            "ck_outbox_message_id_matches_payload",
        ),
        (
            "UPDATE outbox_messages SET payload = payload - 'message_id'",
            "ck_outbox_message_id_matches_payload",
        ),
        (
            "UPDATE outbox_messages SET message_type = 'job.result'",
            "ck_outbox_type_matches_payload",
        ),
        (
            "UPDATE outbox_messages SET payload = payload - 'type'",
            "ck_outbox_type_matches_payload",
        ),
        (
            "UPDATE outbox_messages SET routing_key = ''",
            "ck_outbox_route_not_empty",
        ),
        (
            "UPDATE outbox_messages SET exchange = ''",
            "ck_outbox_route_not_empty",
        ),
        (
            "UPDATE outbox_messages SET attempts = -1",
            "ck_outbox_attempts_non_negative",
        ),
    ] {
        assert_eq!(rejected_by(&pool, sql).await, constraint, "{sql}");
    }
    // What the Phase 3 relay will do.
    accepted(
        &pool,
        "UPDATE outbox_messages SET status = 'sent', sent_at = now()",
    )
    .await;
}

// ----------------------------------------------------------------------------- adapters, keys

#[sqlx::test(migrator = "MIGRATOR")]
async fn a_provider_has_at_most_one_enabled_adapter(pool: PgPool) {
    const SECOND_ADAPTER: &str =
        "INSERT INTO adapters (id, provider_id, key, method, queue_name, enabled)
         SELECT gen_random_uuid(), id, 'dummy_two', 'api', 'adapter.dummy_two.jobs', true FROM providers WHERE code = 'dummy'";
    assert_eq!(
        rejected_by(&pool, SECOND_ADAPTER).await,
        "ux_adapters_one_enabled_per_provider"
    );
    // A disabled second adapter may exist (for a later switch-over), but cannot simply be enabled.
    accepted(
        &pool,
        "INSERT INTO adapters (id, provider_id, key, method, queue_name, enabled)
         SELECT gen_random_uuid(), id, 'dummy_two', 'api', 'adapter.dummy_two.jobs', false FROM providers WHERE code = 'dummy'",
    )
    .await;
    assert_eq!(
        rejected_by(&pool, "UPDATE adapters SET enabled = true").await,
        "ux_adapters_one_enabled_per_provider"
    );
}

#[sqlx::test(migrator = "MIGRATOR")]
async fn an_api_key_row_must_hold_a_sha256_hash_and_a_well_formed_prefix(pool: PgPool) {
    seed(&pool).await;
    for (sql, constraint) in [
        (
            "UPDATE api_keys SET key_hash = '\\x00'",
            "ck_api_keys_hash_is_sha256",
        ),
        (
            "UPDATE api_keys SET key_hash = ''",
            "ck_api_keys_hash_is_sha256",
        ),
        (
            "UPDATE api_keys SET prefix = 'p1'",
            "ck_api_keys_prefix_format",
        ),
        (
            "UPDATE api_keys SET prefix = 'ABCDEF012345'",
            "ck_api_keys_prefix_format",
        ),
    ] {
        assert_eq!(rejected_by(&pool, sql).await, constraint, "{sql}");
    }
}
