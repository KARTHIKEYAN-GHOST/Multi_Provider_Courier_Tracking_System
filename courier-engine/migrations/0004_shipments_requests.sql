-- A shipment row is one GENERATION of a tracking number. Couriers recycle numbers, so
-- uniqueness applies only to rows that are not archived. access_scope keeps data fetched
-- with a particular credential or verifier from being shared with other users
-- (always 'public' in Milestone 1).
CREATE TABLE shipments (
    id               uuid PRIMARY KEY,
    provider_id      uuid NOT NULL REFERENCES providers (id),
    tracking_number  text NOT NULL CHECK (tracking_number ~ '^[A-Z0-9][A-Z0-9._/-]{0,99}$'),
    access_scope     text NOT NULL DEFAULT 'public',
    current_status   shipment_status NOT NULL DEFAULT 'UNKNOWN',
    last_event_at    timestamptz,
    next_poll_at     timestamptz,
    poll_count       integer NOT NULL DEFAULT 0,
    terminal_at      timestamptz,
    archived_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_shipments_active
    ON shipments (provider_id, tracking_number, access_scope)
    WHERE archived_at IS NULL;

-- A user's subscription to a shipment. This is the only handle a user has on a shipment.
CREATE TABLE tracking_requests (
    id            uuid PRIMARY KEY,
    user_id       uuid NOT NULL REFERENCES users (id),
    shipment_id   uuid NOT NULL REFERENCES shipments (id),
    reference     text,
    params        jsonb NOT NULL DEFAULT '{}'::jsonb,
    status        request_status NOT NULL DEFAULT 'active',
    cancelled_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_tracking_requests_active
    ON tracking_requests (user_id, shipment_id)
    WHERE status = 'active';
CREATE INDEX ix_tracking_requests_user ON tracking_requests (user_id, id DESC);
CREATE INDEX ix_tracking_requests_shipment ON tracking_requests (shipment_id);
