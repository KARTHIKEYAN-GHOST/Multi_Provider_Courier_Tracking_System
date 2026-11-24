-- Transactional outbox: a message is written in the same transaction as the state change
-- that causes it, and published afterwards by the relay (at least once).
CREATE TABLE outbox_messages (
    message_id    uuid PRIMARY KEY,
    message_type  text NOT NULL,
    exchange      text NOT NULL,
    routing_key   text NOT NULL,
    payload       jsonb NOT NULL,           -- the complete message envelope
    status        outbox_status NOT NULL DEFAULT 'pending',
    attempts      integer NOT NULL DEFAULT 0,
    last_error    text,
    available_at  timestamptz NOT NULL DEFAULT now(),
    sent_at       timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbox_pending ON outbox_messages (available_at) WHERE status = 'pending';

-- Inbox: remembers which messages each consumer has already processed.
CREATE TABLE processed_messages (
    consumer      text NOT NULL,
    message_id    uuid NOT NULL,
    processed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, message_id)
);

-- Record of every dead-lettered job message. applied = whether it closed the job's
-- current open attempt (false for a stale dead-letter).
CREATE TABLE dead_letters (
    id            uuid PRIMARY KEY,
    message_id    uuid,
    source_queue  text NOT NULL,
    reason        text,
    job_id        uuid,
    attempt       integer,
    applied       boolean NOT NULL,
    payload       jsonb,
    headers       jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_dead_letters_job ON dead_letters (job_id);
