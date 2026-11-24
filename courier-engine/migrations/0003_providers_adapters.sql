CREATE TABLE providers (
    id                  uuid PRIMARY KEY,
    code                text NOT NULL UNIQUE CHECK (code ~ '^[a-z][a-z0-9_]{0,49}$'),
    name                text NOT NULL,
    tracking_method     tracking_method NOT NULL,
    -- Three independent timers (docs/milestone-1.md section 1.7):
    timeout_ms          integer NOT NULL CHECK (timeout_ms BETWEEN 1 AND 600000),  -- adapter execution budget
    queue_wait_max_ms   integer NOT NULL CHECK (queue_wait_max_ms > 0),            -- how long an attempt may wait for a worker
    result_grace_ms     integer NOT NULL CHECK (result_grace_ms >= 0),             -- extra wait for a result after timeout_ms
    max_attempts        integer NOT NULL CHECK (max_attempts >= 1),
    retry_base_ms       integer NOT NULL CHECK (retry_base_ms > 0),
    retry_max_ms        integer NOT NULL CHECK (retry_max_ms >= retry_base_ms),
    reuse_window_days   integer NOT NULL DEFAULT 90 CHECK (reuse_window_days > 0),
    enabled             boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE adapters (
    id           uuid PRIMARY KEY,
    provider_id  uuid NOT NULL REFERENCES providers (id),
    key          text NOT NULL UNIQUE CHECK (key ~ '^[a-z][a-z0-9_]{0,49}$'),
    method       tracking_method NOT NULL,
    queue_name   text NOT NULL,
    enabled      boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_adapters_provider ON adapters (provider_id);

-- Versions observed from workers (filled from job.started / job.result in Phase 3).
CREATE TABLE adapter_versions (
    id             uuid PRIMARY KEY,
    adapter_id     uuid NOT NULL REFERENCES adapters (id),
    version        text NOT NULL,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (adapter_id, version)
);

-- Deterministic raw status -> normalized status mapping. Unmapped statuses become UNKNOWN.
CREATE TABLE provider_status_mappings (
    id                 uuid PRIMARY KEY,
    provider_id        uuid NOT NULL REFERENCES providers (id),
    raw_status         text NOT NULL,
    normalized_status  shipment_status NOT NULL,
    source             text NOT NULL DEFAULT 'manual',
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider_id, raw_status)
);
