CREATE TABLE users (
    id          uuid PRIMARY KEY,
    email       text NOT NULL,
    status      user_status NOT NULL DEFAULT 'active',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_users_email ON users (lower(email));

-- Only the SHA-256 hash of a key is stored. The key itself is shown once, at creation.
CREATE TABLE api_keys (
    id            uuid PRIMARY KEY,
    user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    prefix        text NOT NULL UNIQUE,
    key_hash      bytea NOT NULL,
    name          text,
    last_used_at  timestamptz,
    revoked_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_api_keys_user ON api_keys (user_id);
