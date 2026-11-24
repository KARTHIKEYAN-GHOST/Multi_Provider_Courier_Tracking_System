-- The provider response exactly as received. Stored only for the accepted execution.
CREATE TABLE raw_tracking_responses (
    id            uuid PRIMARY KEY,
    shipment_id   uuid NOT NULL REFERENCES shipments (id),
    job_id        uuid NOT NULL REFERENCES tracking_jobs (id),
    attempt       integer NOT NULL,
    execution_id  uuid NOT NULL,
    content_type  text NOT NULL,
    body          jsonb NOT NULL,
    sha256        bytea NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_raw_responses_shipment ON raw_tracking_responses (shipment_id, created_at DESC);
CREATE INDEX ix_raw_responses_job ON raw_tracking_responses (job_id);

-- Append-only tracking history. The raw provider status is always kept next to the normalized one.
CREATE TABLE tracking_events (
    id                 uuid PRIMARY KEY,
    shipment_id        uuid NOT NULL REFERENCES shipments (id),
    raw_response_id    uuid REFERENCES raw_tracking_responses (id),
    raw_status         text NOT NULL,
    raw_description    text,
    normalized_status  shipment_status NOT NULL,
    occurred_at        timestamptz NOT NULL,
    location           jsonb,
    dedupe_key         text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (shipment_id, dedupe_key)
);
CREATE INDEX ix_tracking_events_shipment ON tracking_events (shipment_id, occurred_at, id);
