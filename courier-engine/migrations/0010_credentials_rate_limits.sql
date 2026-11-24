-- Phase 6: provider credentials, worker tokens, audit log, dispatch rate limiting, and the
-- first two providers after the dummy one.

-- ----------------------------------------------------------------------------- providers

ALTER TABLE providers
    -- A provider that exists only to exercise the system (dummy, demosite). The Engine
    -- enables or disables all of them at start-up according to ENABLE_DUMMY_PROVIDER.
    ADD COLUMN is_test                  boolean NOT NULL DEFAULT false,
    -- Scraping a website needs its owner's permission. A 'scrape' or 'browser' provider is
    -- never dispatched to until a person has recorded that permission here.
    ADD COLUMN scraping_authorized      boolean NOT NULL DEFAULT false,
    -- Jobs for a provider that needs a credential are refused while none is stored.
    ADD COLUMN requires_credential      boolean NOT NULL DEFAULT false,
    -- Rate limit: the minimum time between two job dispatches to this provider (0 = none).
    ADD COLUMN min_dispatch_interval_ms integer NOT NULL DEFAULT 0 CHECK (min_dispatch_interval_ms >= 0),
    -- The earliest moment the next job.track for this provider may be published.
    ADD COLUMN next_dispatch_at         timestamptz;

UPDATE providers SET is_test = true WHERE code = 'dummy';

-- ----------------------------------------------------------------------------- credentials

-- Secrets are stored with envelope encryption (AES-256-GCM): each record is encrypted with
-- its own random data key, and the data key is encrypted ("wrapped") with the master key
-- from the Engine's environment. The database alone never reveals a secret.
CREATE TABLE provider_credentials (
    id            uuid PRIMARY KEY,
    provider_id   uuid NOT NULL REFERENCES providers (id),
    label         text NOT NULL CHECK (label <> '' AND length(label) <= 100),
    key_version   integer NOT NULL CHECK (key_version >= 1),   -- which master key wrapped the data key
    wrapped_key   bytea NOT NULL,                              -- data key, encrypted with the master key
    key_nonce     bytea NOT NULL CHECK (octet_length(key_nonce) = 12),
    ciphertext    bytea NOT NULL,                              -- the secret, encrypted with the data key
    nonce         bytea NOT NULL CHECK (octet_length(nonce) = 12),
    created_at    timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz
);
-- A provider has at most one credential in use; replacing it revokes the previous one.
CREATE UNIQUE INDEX ux_provider_credentials_one_active
    ON provider_credentials (provider_id) WHERE revoked_at IS NULL;

-- ----------------------------------------------------------------------------- worker tokens

-- A worker proves which adapter it runs when it asks the Engine for a credential.
-- Stored like user API keys: prefix plus SHA-256 hash, never the token itself.
CREATE TABLE worker_tokens (
    id            uuid PRIMARY KEY,
    adapter_id    uuid NOT NULL REFERENCES adapters (id),
    prefix        text NOT NULL UNIQUE CHECK (prefix ~ '^[0-9a-f]{12}$'),
    token_hash    bytea NOT NULL CHECK (octet_length(token_hash) = 32),
    name          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);
CREATE INDEX ix_worker_tokens_adapter ON worker_tokens (adapter_id);

-- ----------------------------------------------------------------------------- audit log

-- Append-only record of security-relevant actions. It never contains a secret.
CREATE TABLE audit_log (
    id             uuid PRIMARY KEY,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    actor_type     text NOT NULL CHECK (actor_type IN ('worker', 'admin', 'engine')),
    actor_id       text,                 -- worker token ID, or NULL for the command line
    action         text NOT NULL,        -- e.g. credential.resolve, credential.set
    outcome        text NOT NULL CHECK (outcome IN ('allowed', 'denied')),
    reason         text,                 -- why it was denied
    provider_id    uuid,
    credential_id  uuid,
    job_id         uuid,
    attempt        integer,
    details        jsonb
);
CREATE INDEX ix_audit_log_time ON audit_log (occurred_at DESC);
CREATE INDEX ix_audit_log_job ON audit_log (job_id) WHERE job_id IS NOT NULL;

CREATE FUNCTION forbid_audit_log_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'check_violation', CONSTRAINT = 'audit_log_append_only';
END;
$$;
CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_log_change();

-- ----------------------------------------------------------------------------- DHL (official API)

-- DHL Shipment Tracking - Unified API. Needs an API key (stored as a credential).
-- The free tier allows roughly one call every five seconds, hence the dispatch interval.
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential, min_dispatch_interval_ms)
VALUES ('01900000-0000-7000-8000-000000000101', 'dhl', 'DHL', 'api',
        20000, 900000, 30000, 4, 5000, 300000, true, 5000);

INSERT INTO adapters (id, provider_id, key, method, queue_name)
VALUES ('01900000-0000-7000-8000-000000000102', '01900000-0000-7000-8000-000000000101',
        'dhl', 'api', 'adapter.dhl.jobs');

-- The adapter reports DHL's statusCode as the raw status.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
VALUES
    ('01900000-0000-7000-8000-000000000111', '01900000-0000-7000-8000-000000000101', 'pre-transit', 'CREATED'),
    ('01900000-0000-7000-8000-000000000112', '01900000-0000-7000-8000-000000000101', 'transit',     'IN_TRANSIT'),
    ('01900000-0000-7000-8000-000000000113', '01900000-0000-7000-8000-000000000101', 'delivered',   'DELIVERED'),
    ('01900000-0000-7000-8000-000000000114', '01900000-0000-7000-8000-000000000101', 'failure',     'EXCEPTION'),
    ('01900000-0000-7000-8000-000000000115', '01900000-0000-7000-8000-000000000101', 'unknown',     'UNKNOWN');

-- ----------------------------------------------------------------------------- demo site (scraping practice)

-- A small courier website that ships with this project (courier-adapters/demo_site). It is
-- ours, so scraping it is authorized; it is a test provider like the dummy one.
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, is_test, scraping_authorized)
VALUES ('01900000-0000-7000-8000-000000000201', 'demosite', 'Demo Courier Website', 'scrape',
        15000, 900000, 30000, 3, 1000, 60000, true, true);

INSERT INTO adapters (id, provider_id, key, method, queue_name)
VALUES ('01900000-0000-7000-8000-000000000202', '01900000-0000-7000-8000-000000000201',
        'demosite', 'scrape', 'adapter.demosite.jobs');

-- The adapter reports the text of the page's status column as the raw status.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
VALUES
    ('01900000-0000-7000-8000-000000000211', '01900000-0000-7000-8000-000000000201', 'Booked',           'CREATED'),
    ('01900000-0000-7000-8000-000000000212', '01900000-0000-7000-8000-000000000201', 'Picked up',        'PICKED_UP'),
    ('01900000-0000-7000-8000-000000000213', '01900000-0000-7000-8000-000000000201', 'In transit',       'IN_TRANSIT'),
    ('01900000-0000-7000-8000-000000000214', '01900000-0000-7000-8000-000000000201', 'Out for delivery', 'OUT_FOR_DELIVERY'),
    ('01900000-0000-7000-8000-000000000215', '01900000-0000-7000-8000-000000000201', 'Delivered',        'DELIVERED'),
    ('01900000-0000-7000-8000-000000000216', '01900000-0000-7000-8000-000000000201', 'Returned',         'RETURNED');
