-- Phase 2.1 hardening: invariants that were enforced only by application code are now
-- enforced by PostgreSQL itself. Every constraint is named, so a violation identifies the rule.

-- ----------------------------------------------------------------------------- tracking_jobs

-- Each status requires the timestamp that the scheduler or reaper will act on.
ALTER TABLE tracking_jobs
    ADD CONSTRAINT ck_jobs_queued_has_queue_deadline
        CHECK (status <> 'QUEUED' OR queue_deadline_at IS NOT NULL),
    ADD CONSTRAINT ck_jobs_processing_has_execution_deadline
        CHECK (status <> 'PROCESSING' OR execution_deadline_at IS NOT NULL),
    ADD CONSTRAINT ck_jobs_retrying_has_next_attempt
        CHECK (status <> 'RETRYING' OR next_attempt_at IS NOT NULL),
    -- completed_at is "when the job reached a terminal status": set exactly for terminal jobs.
    ADD CONSTRAINT ck_jobs_completed_at_matches_terminal
        CHECK ((status IN ('COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED')) = (completed_at IS NOT NULL)),
    -- A job only completes with an answer from the provider.
    ADD CONSTRAINT ck_jobs_completed_has_outcome
        CHECK (status <> 'COMPLETED' OR (outcome IS NOT NULL AND outcome IN ('SUCCESS', 'NOT_FOUND'))),
    ADD CONSTRAINT ck_jobs_attempt_within_max
        CHECK (attempt <= max_attempts),
    ADD CONSTRAINT ck_jobs_dead_letter_count_non_negative
        CHECK (dead_letter_count >= 0);

-- Every job must have a job_attempts row for its current attempt.
-- A CHECK constraint cannot look at another table, and the job row must be inserted before
-- its attempt row (the attempt references the job). A DEFERRED constraint trigger solves both:
-- it runs at COMMIT, after the whole transaction has written its rows.
CREATE FUNCTION enforce_job_has_current_attempt() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM job_attempts a WHERE a.job_id = NEW.id AND a.attempt = NEW.attempt) THEN
        RAISE EXCEPTION 'tracking job % has no job_attempts row for attempt %', NEW.id, NEW.attempt
            USING ERRCODE = 'check_violation', CONSTRAINT = 'job_has_current_attempt', TABLE = 'tracking_jobs';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER job_has_current_attempt
    AFTER INSERT OR UPDATE OF attempt ON tracking_jobs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_job_has_current_attempt();

-- The attempt row a job currently points at cannot be removed on its own.
-- (Deleting the job itself still cascades: by then the job row is gone.)
CREATE FUNCTION forbid_deleting_current_attempt() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM tracking_jobs j WHERE j.id = OLD.job_id AND j.attempt = OLD.attempt) THEN
        RAISE EXCEPTION 'attempt % is the current attempt of tracking job %', OLD.attempt, OLD.job_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'job_has_current_attempt', TABLE = 'job_attempts';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER current_attempt_not_deleted
    AFTER DELETE ON job_attempts
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION forbid_deleting_current_attempt();

-- ----------------------------------------------------------------------------- archived shipments

-- An archived shipment is a closed generation of a tracking number: no new job may ever be
-- created for it.
--
-- Both triggers below first lock the shipment row and only then look. Without the lock, a job
-- insert and an archive running at the same moment could each miss the other's uncommitted
-- change. With it, one waits for the other to commit and then sees the result.
CREATE FUNCTION forbid_job_on_archived_shipment() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM shipments s WHERE s.id = NEW.shipment_id FOR SHARE;
    IF EXISTS (SELECT 1 FROM shipments s WHERE s.id = NEW.shipment_id AND s.archived_at IS NOT NULL) THEN
        RAISE EXCEPTION 'shipment % is archived; no job can be created for it', NEW.shipment_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'no_job_on_archived_shipment', TABLE = 'tracking_jobs';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER no_job_on_archived_shipment
    BEFORE INSERT ON tracking_jobs
    FOR EACH ROW EXECUTE FUNCTION forbid_job_on_archived_shipment();

-- And the other direction: a shipment cannot be archived while a job for it is still running.
CREATE FUNCTION forbid_archiving_shipment_with_active_job() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM shipments s WHERE s.id = NEW.id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM tracking_jobs j
        WHERE j.shipment_id = NEW.id
          AND j.status IN ('PENDING', 'QUEUED', 'PROCESSING', 'WAITING_FOR_OTP', 'WAITING_FOR_CAPTCHA', 'RETRYING')
    ) THEN
        RAISE EXCEPTION 'shipment % has an active job and cannot be archived', NEW.id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'no_archive_with_active_job', TABLE = 'shipments';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER no_archive_with_active_job
    BEFORE UPDATE OF archived_at ON shipments
    FOR EACH ROW
    WHEN (OLD.archived_at IS NULL AND NEW.archived_at IS NOT NULL)
    EXECUTE FUNCTION forbid_archiving_shipment_with_active_job();

-- access_scope decides who may share a shipment row; only the three documented forms exist.
ALTER TABLE shipments
    ADD CONSTRAINT ck_shipments_access_scope
        CHECK (access_scope = 'public' OR access_scope ~ '^(cred|verifier):[A-Za-z0-9_-]{1,100}$'),
    ADD CONSTRAINT ck_shipments_poll_count_non_negative
        CHECK (poll_count >= 0);

-- ----------------------------------------------------------------------------- tracking_requests

ALTER TABLE tracking_requests
    ADD CONSTRAINT ck_requests_cancelled_at_matches_status
        CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL));

-- ----------------------------------------------------------------------------- outbox_messages

ALTER TABLE outbox_messages
    ADD CONSTRAINT ck_outbox_sent_at_matches_status
        CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
    -- The row and the envelope it carries must describe the same message: consumers
    -- deduplicate on the envelope's message_id.
    ADD CONSTRAINT ck_outbox_message_id_matches_payload
        CHECK (payload ->> 'message_id' IS NOT NULL AND payload ->> 'message_id' = message_id::text),
    ADD CONSTRAINT ck_outbox_type_matches_payload
        CHECK (payload ->> 'type' IS NOT NULL AND payload ->> 'type' = message_type),
    ADD CONSTRAINT ck_outbox_route_not_empty
        CHECK (exchange <> '' AND routing_key <> ''),
    ADD CONSTRAINT ck_outbox_attempts_non_negative
        CHECK (attempts >= 0);

-- ----------------------------------------------------------------------------- adapters

-- Dispatch picks "the" adapter of a provider; there must be at most one enabled.
CREATE UNIQUE INDEX ux_adapters_one_enabled_per_provider ON adapters (provider_id) WHERE enabled;

-- ----------------------------------------------------------------------------- api_keys

ALTER TABLE api_keys
    ADD CONSTRAINT ck_api_keys_hash_is_sha256
        CHECK (octet_length(key_hash) = 32),
    ADD CONSTRAINT ck_api_keys_prefix_format
        CHECK (prefix ~ '^[0-9a-f]{12}$');
