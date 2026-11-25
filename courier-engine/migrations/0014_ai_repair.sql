-- Phase 9: what the AI repair service works from.
--
-- The Engine only records and hands out; it never runs AI. The repair service is a separate
-- program that asks the Engine for a task over the internal API and reports the outcome.

-- Provider answers an adapter could not read (PARSE_ERROR, SCHEMA_DRIFT), exactly as the
-- adapter received them. A few recent ones are kept per adapter; older ones are deleted.
-- They can contain a consignee's details, like raw_tracking_responses.
CREATE TABLE adapter_failure_samples (
    id               uuid PRIMARY KEY,
    adapter_key      text NOT NULL,
    adapter_version  text NOT NULL,
    provider_id      uuid NOT NULL REFERENCES providers (id),
    job_id           uuid NOT NULL REFERENCES tracking_jobs (id) ON DELETE CASCADE,
    execution_id     uuid NOT NULL UNIQUE,
    error_class      text NOT NULL,
    error_message    text NOT NULL,
    tracking_number  text NOT NULL,
    content_type     text NOT NULL,
    body             jsonb NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_failure_samples_adapter ON adapter_failure_samples (adapter_key, created_at DESC);

-- One piece of work for the repair service: repair an adapter, or write one for a new provider.
CREATE TABLE repair_tasks (
    id               uuid PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('repair', 'new_provider')),
    adapter_key      text NOT NULL CHECK (adapter_key ~ '^[a-z][a-z0-9_]{0,49}$'),
    -- The adapter version the failures came from (repair only).
    adapter_version  text,
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'running', 'published', 'failed')),
    -- For a new provider: what the operator said about it. Empty for a repair.
    request          jsonb NOT NULL DEFAULT '{}',
    attempts         integer NOT NULL DEFAULT 0,
    claimed_at       timestamptz,
    finished_at      timestamptz,
    commit_sha       text,
    summary          text,
    failure_reason   text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
-- At most one task in progress per adapter.
CREATE UNIQUE INDEX ux_repair_tasks_open ON repair_tasks (adapter_key) WHERE status IN ('pending', 'running');
CREATE INDEX ix_repair_tasks_created ON repair_tasks (created_at DESC);
