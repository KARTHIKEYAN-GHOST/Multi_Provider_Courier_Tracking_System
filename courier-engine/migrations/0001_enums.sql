-- Enumerated types. Execution status and shipment status are deliberately separate.

CREATE TYPE execution_status AS ENUM (
    'PENDING', 'QUEUED', 'PROCESSING', 'WAITING_FOR_OTP', 'WAITING_FOR_CAPTCHA',
    'RETRYING', 'COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED'
);

CREATE TYPE shipment_status AS ENUM (
    'CREATED', 'PICKED_UP', 'IN_TRANSIT', 'OUT_FOR_DELIVERY',
    'DELIVERED', 'RETURNED', 'EXCEPTION', 'UNKNOWN'
);

CREATE TYPE job_outcome AS ENUM ('SUCCESS', 'NOT_FOUND', 'FAILURE');

-- The last three are assigned only by the Engine; workers can never report them.
CREATE TYPE error_class AS ENUM (
    'NETWORK_TIMEOUT', 'NETWORK_CONNECT', 'HTTP_5XX', 'RATE_LIMITED', 'BLOCKED',
    'AUTH_INVALID', 'AUTH_EXPIRED', 'PARSE_ERROR', 'SCHEMA_DRIFT', 'ADAPTER_TIMEOUT',
    'INTERNAL', 'UNKNOWN',
    'DELIVERY_LIMIT_EXCEEDED', 'QUEUE_WAIT_EXCEEDED', 'WORKER_LOST'
);

CREATE TYPE job_trigger AS ENUM ('initial', 'poll', 'manual');
CREATE TYPE tracking_method AS ENUM ('api', 'scrape', 'browser');
CREATE TYPE attempt_closed_by AS ENUM ('result', 'dead_letter', 'reaper');
CREATE TYPE execution_disposition AS ENUM ('incomplete', 'accepted', 'duplicate', 'stale');
CREATE TYPE user_status AS ENUM ('active', 'disabled');
CREATE TYPE request_status AS ENUM ('active', 'cancelled');
CREATE TYPE outbox_status AS ENUM ('pending', 'sent');
