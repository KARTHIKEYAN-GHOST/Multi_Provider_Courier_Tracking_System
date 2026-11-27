-- Phase 11: many organizations use one system. Two roles:
--   org_user  belongs to exactly one organization; tracks shipments and sees only its own
--             tracking requests.
--   admin     belongs to the platform operator, not to an organization; sees and answers
--             every open challenge (one-time codes, CAPTCHAs) and runs the platform.
-- Replaces the staff flag of Phase 10.

CREATE TYPE user_role AS ENUM ('org_user', 'admin');

CREATE TABLE organizations (
    id          uuid PRIMARY KEY,
    name        text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    -- A disabled organization's users can no longer use their API keys.
    status      user_status NOT NULL DEFAULT 'active',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_organizations_name ON organizations (lower(name));

ALTER TABLE users
    ADD COLUMN organization_id uuid REFERENCES organizations (id),
    ADD COLUMN role user_role NOT NULL DEFAULT 'org_user';

-- Users that exist already become users of one organization. Nobody is an admin until one
-- is named: admin set-role --email ... --role admin
INSERT INTO organizations (id, name)
SELECT gen_random_uuid(), 'Default organization'
WHERE EXISTS (SELECT 1 FROM users);
UPDATE users SET organization_id = (SELECT id FROM organizations WHERE name = 'Default organization');

-- An organization user always has an organization; an admin never has one.
ALTER TABLE users ADD CONSTRAINT users_role_matches_organization
    CHECK ((role = 'admin') = (organization_id IS NULL));
CREATE INDEX ix_users_organization ON users (organization_id);

ALTER TABLE users DROP COLUMN is_staff;

-- A challenge is no longer assigned to one user: admins answer every one of them.
DROP INDEX ix_interventions_user_pending;
ALTER TABLE human_interventions DROP COLUMN assigned_user_id;
