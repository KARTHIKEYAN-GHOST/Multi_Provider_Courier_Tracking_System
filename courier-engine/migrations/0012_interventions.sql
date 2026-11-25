-- Phase 8: human interventions (one-time codes and CAPTCHAs).
--
-- A lookup can stop because the provider asks for something only a person can supply. The
-- job then waits (WAITING_FOR_OTP / WAITING_FOR_CAPTCHA) for the assigned user's answer,
-- and continues with it on the same attempt.

-- New enum values are only added here; nothing in this migration uses them.
ALTER TYPE error_class ADD VALUE 'SESSION_LOST' BEFORE 'DELIVERY_LIMIT_EXCEEDED';  -- reported by workers
ALTER TYPE error_class ADD VALUE 'INTERVENTION_EXPIRED';       -- Engine: nobody answered in time
ALTER TYPE error_class ADD VALUE 'CHALLENGE_LIMIT_EXCEEDED';   -- Engine: too many challenges for one job
ALTER TYPE execution_disposition ADD VALUE 'suspended';        -- the execution ended by asking for an answer

ALTER TABLE providers
    -- How many challenges one job may put to a user (a wrong code leads to a new challenge).
    ADD COLUMN max_challenges      integer NOT NULL DEFAULT 3 CHECK (max_challenges >= 1),
    -- The longest the Engine waits for an answer, whatever the adapter says.
    ADD COLUMN intervention_ttl_s  integer NOT NULL DEFAULT 600 CHECK (intervention_ttl_s BETWEEN 30 AND 3600);

ALTER TABLE tracking_jobs
    -- Set while WAITING_FOR_*: when the wait for the user's answer ends.
    ADD COLUMN intervention_deadline_at  timestamptz,
    ADD COLUMN challenge_count           integer NOT NULL DEFAULT 0 CHECK (challenge_count >= 0),
    -- The user whose request or refresh created the job (NULL for automatic polls). A
    -- challenge is put to this user.
    ADD COLUMN initiated_by              uuid REFERENCES users (id),
    ADD CONSTRAINT ck_jobs_waiting_has_intervention_deadline
        CHECK (status NOT IN ('WAITING_FOR_OTP', 'WAITING_FOR_CAPTCHA') OR intervention_deadline_at IS NOT NULL);

CREATE INDEX ix_tracking_jobs_intervention_deadline ON tracking_jobs (intervention_deadline_at)
    WHERE status IN ('WAITING_FOR_OTP', 'WAITING_FOR_CAPTCHA');

-- One challenge put to one user.
--   pending   waiting for the user's answer
--   answered  the answer is stored (encrypted) and job.resume has been sent
--   completed the lookup went on with the answer (accepted or not, the attempt is closed)
--   rejected  the provider did not accept the answer; a new challenge follows
--   expired   nobody answered in time
--   closed    overtaken: the attempt ended some other way
CREATE TABLE human_interventions (
    id                  uuid PRIMARY KEY,
    job_id              uuid NOT NULL REFERENCES tracking_jobs (id) ON DELETE CASCADE,
    attempt             integer NOT NULL,
    sequence            integer NOT NULL CHECK (sequence >= 1),   -- 1st, 2nd ... challenge of the job
    kind                text NOT NULL CHECK (kind IN ('otp', 'captcha')),
    prompt              text NOT NULL CHECK (char_length(prompt) BETWEEN 1 AND 500),
    image_content_type  text CHECK (image_content_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')),
    image               bytea,
    response_format     jsonb,
    session_ref         uuid NOT NULL,          -- the worker's saved session; opaque to the Engine
    execution_id        uuid NOT NULL,          -- the execution that asked
    assigned_user_id    uuid REFERENCES users (id),   -- only this user sees and answers it
    status              text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'answered', 'completed', 'rejected', 'expired', 'closed')),
    expires_at          timestamptz NOT NULL,
    submit_failures     integer NOT NULL DEFAULT 0,
    -- The user's answer, envelope-encrypted like a credential. Present only while 'answered'.
    wrapped_key         bytea,
    key_nonce           bytea,
    ciphertext          bytea,
    nonce               bytea,
    key_version         integer,
    answered_at         timestamptz,
    closed_at           timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, sequence),
    CONSTRAINT ck_interventions_image_complete CHECK ((image IS NULL) = (image_content_type IS NULL)),
    CONSTRAINT ck_interventions_answer_only_while_answered CHECK (ciphertext IS NULL OR status = 'answered')
);
-- At most one open challenge per job.
CREATE UNIQUE INDEX ux_interventions_one_open ON human_interventions (job_id) WHERE status IN ('pending', 'answered');
CREATE INDEX ix_interventions_user_pending ON human_interventions (assigned_user_id, created_at DESC) WHERE status = 'pending';
