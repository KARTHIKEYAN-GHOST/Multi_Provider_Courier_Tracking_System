-- Phase 12: a courier can be added before it has an adapter, and the AI builds the adapter.
--
--   building  added by a user or an admin (or inserted straight into this table); the Engine
--             asks the AI repair service for an adapter. Tracking requests are accepted and
--             wait.
--   live      it has an adapter (the adapter's migration sets this). Waiting lookups start.
--   failed    the AI could not build the adapter after three tries; build_failure says why.
--             admin retry-provider puts it back to building.

ALTER TABLE providers
    ADD COLUMN status                 text NOT NULL DEFAULT 'live'
                                      CHECK (status IN ('building', 'live', 'failed')),
    -- Where it is tracked: the tracking page or the API address the AI starts from.
    ADD COLUMN tracking_url           text CHECK (length(tracking_url) <= 500),
    -- The only hosts the AI and the adapter may contact.
    ADD COLUMN hosts                  text[] NOT NULL DEFAULT '{}',
    -- How it is tracked, in the words of whoever added it.
    ADD COLUMN notes                  text CHECK (length(notes) <= 4000),
    ADD COLUMN added_by               uuid REFERENCES users (id) ON DELETE SET NULL,
    ADD COLUMN added_by_organization  uuid REFERENCES organizations (id),
    -- Who recorded the website owner's permission to look it up automatically, and when.
    ADD COLUMN permission_recorded_by uuid REFERENCES users (id) ON DELETE SET NULL,
    ADD COLUMN permission_recorded_at timestamptz,
    -- The current round of building (reset by retry-provider) and why the last round failed.
    ADD COLUMN build_started_at       timestamptz,
    ADD COLUMN build_failure          text,
    -- An example answer of the courier, if whoever added it had one (handed to the AI).
    ADD COLUMN build_sample           text CHECK (length(build_sample) <= 100000);

CREATE INDEX ix_providers_building ON providers (id) WHERE status = 'building';
CREATE INDEX ix_providers_added_by_organization ON providers (added_by_organization, created_at);

-- Each organization can keep its own login for a courier (its own account with it). A login
-- without an organization is the platform's own, used for everyone who has none.
ALTER TABLE provider_credentials ADD COLUMN organization_id uuid REFERENCES organizations (id);
DROP INDEX ux_provider_credentials_one_active;
CREATE UNIQUE INDEX ux_provider_credentials_one_active
    ON provider_credentials (provider_id, COALESCE(organization_id, '00000000-0000-0000-0000-000000000000'))
    WHERE revoked_at IS NULL;

-- A lookup made with an organization's own login is shared only inside that organization:
-- its shipment has access_scope 'org:<organization id>'.
ALTER TABLE shipments DROP CONSTRAINT ck_shipments_access_scope;
ALTER TABLE shipments ADD CONSTRAINT ck_shipments_access_scope
    CHECK (access_scope = 'public' OR access_scope ~ '^(cred|verifier|org):[A-Za-z0-9_-]{1,100}$');

-- A user (not only an admin) now adds couriers and stores logins; the audit log says so.
ALTER TABLE audit_log DROP CONSTRAINT audit_log_actor_type_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_actor_type_check
    CHECK (actor_type IN ('worker', 'admin', 'engine', 'user', 'ai_repair'));
