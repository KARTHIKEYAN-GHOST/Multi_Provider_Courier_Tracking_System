-- Phase 7: automatic re-polling, status-mapping management, archiving and retention.

-- ----------------------------------------------------------------------------- polling policy

ALTER TABLE providers
    -- Time until a shipment is looked up again. 0 = this provider is never polled automatically.
    ADD COLUMN poll_interval_ms      integer NOT NULL DEFAULT 0 CHECK (poll_interval_ms >= 0),
    -- The interval doubles after every lookup that brought nothing new, up to this value.
    ADD COLUMN poll_max_interval_ms  integer NOT NULL DEFAULT 0,
    -- Polling stops when a shipment has shown nothing new for this many days.
    ADD COLUMN poll_give_up_days     integer NOT NULL DEFAULT 30 CHECK (poll_give_up_days > 0),
    ADD CONSTRAINT ck_providers_poll_max CHECK (poll_max_interval_ms >= poll_interval_ms);

ALTER TABLE shipments
    -- When the Engine last stored a new event for the shipment (Engine time, unlike
    -- last_event_at, which is the provider's time of the newest event).
    ADD COLUMN last_change_at        timestamptz,
    -- Lookups in a row that brought no new event; drives the polling back-off.
    ADD COLUMN polls_without_change  integer NOT NULL DEFAULT 0 CHECK (polls_without_change >= 0),
    -- Set when the events and raw responses of an archived shipment have been deleted.
    ADD COLUMN purged_at             timestamptz,
    ADD CONSTRAINT ck_shipments_purged_only_when_archived CHECK (purged_at IS NULL OR archived_at IS NOT NULL);

CREATE INDEX ix_shipments_due_poll ON shipments (next_poll_at)
    WHERE next_poll_at IS NOT NULL AND archived_at IS NULL;
CREATE INDEX ix_shipments_to_purge ON shipments (archived_at)
    WHERE archived_at IS NOT NULL AND purged_at IS NULL;

-- DHL: look again after 30 minutes, backing off to every 6 hours while nothing changes.
-- The test providers (dummy, demosite) are not polled unless an operator sets a policy.
UPDATE providers SET poll_interval_ms = 1800000, poll_max_interval_ms = 21600000 WHERE code = 'dhl';

-- ----------------------------------------------------------------------------- status mappings

ALTER TABLE provider_status_mappings
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

-- Finding statuses that still need a mapping.
CREATE INDEX ix_tracking_events_unmapped ON tracking_events (raw_status)
    WHERE normalized_status = 'UNKNOWN';

-- ----------------------------------------------------------------------------- message retention

CREATE INDEX ix_outbox_sent ON outbox_messages (sent_at) WHERE status = 'sent';
CREATE INDEX ix_processed_messages_time ON processed_messages (processed_at);
