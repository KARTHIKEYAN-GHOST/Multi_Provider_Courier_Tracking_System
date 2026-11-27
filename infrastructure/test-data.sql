-- Test data put straight into the database (docs/phase-11.md, "Testing with data put
-- straight into the database"). Load it with: make seed-test-data
--
-- Two organizations with two users each, one admin, and three shipments:
--   DUMMY-OK-SEED1   Acme's only            (dummy courier: completes by itself)
--   DUMMY-OK-SEED2   Acme AND Globex        (one shared lookup, two requests)
--   DEMO-OTP-SEED3   Globex's only          (demo website: asks for a one-time code,
--                                            which only the admin sees and answers)
-- No lookup has run yet: each request is started with POST /v1/tracking-requests/{id}/refresh.
--
-- API keys are NOT inserted here: the database holds only a hash of each key, so keys are
-- made by the Engine (make seed-test-data does that and prints them once).
--
-- Every row has a fixed id; loading the file again changes nothing. Only example.com
-- addresses are used. Nothing here is real customer data.

BEGIN;

INSERT INTO organizations (id, name) VALUES
    ('01930000-0000-7000-8000-00000000a001', 'Acme Traders'),
    ('01930000-0000-7000-8000-00000000a002', 'Globex Logistics')
ON CONFLICT DO NOTHING;

-- role org_user needs an organization; role admin must have none (the database checks it).
INSERT INTO users (id, email, role, organization_id) VALUES
    ('01930000-0000-7000-8000-00000000b001', 'asha@acme.example.com',   'org_user', '01930000-0000-7000-8000-00000000a001'),
    ('01930000-0000-7000-8000-00000000b002', 'ravi@acme.example.com',   'org_user', '01930000-0000-7000-8000-00000000a001'),
    ('01930000-0000-7000-8000-00000000b003', 'meena@globex.example.com', 'org_user', '01930000-0000-7000-8000-00000000a002'),
    ('01930000-0000-7000-8000-00000000b004', 'arun@globex.example.com',  'org_user', '01930000-0000-7000-8000-00000000a002'),
    ('01930000-0000-7000-8000-00000000b009', 'ops@platform.example.com', 'admin',    NULL)
ON CONFLICT DO NOTHING;

-- Providers come from the migrations: dummy ...0001, demosite ...0201.
INSERT INTO shipments (id, provider_id, tracking_number) VALUES
    ('01930000-0000-7000-8000-00000000c001', '01900000-0000-7000-8000-000000000001', 'DUMMY-OK-SEED1'),
    ('01930000-0000-7000-8000-00000000c002', '01900000-0000-7000-8000-000000000001', 'DUMMY-OK-SEED2'),
    ('01930000-0000-7000-8000-00000000c003', '01900000-0000-7000-8000-000000000201', 'DEMO-OTP-SEED3')
ON CONFLICT DO NOTHING;

INSERT INTO tracking_requests (id, user_id, shipment_id, reference) VALUES
    ('01930000-0000-7000-8000-00000000d001', '01930000-0000-7000-8000-00000000b001', '01930000-0000-7000-8000-00000000c001', 'acme-order-1'),
    ('01930000-0000-7000-8000-00000000d002', '01930000-0000-7000-8000-00000000b002', '01930000-0000-7000-8000-00000000c002', 'acme-order-2'),
    ('01930000-0000-7000-8000-00000000d003', '01930000-0000-7000-8000-00000000b003', '01930000-0000-7000-8000-00000000c002', 'globex-po-7'),
    ('01930000-0000-7000-8000-00000000d004', '01930000-0000-7000-8000-00000000b004', '01930000-0000-7000-8000-00000000c003', 'globex-po-8')
ON CONFLICT DO NOTHING;

COMMIT;
