-- Provider 'demosecure' (Demo Secure Logistics), added by the AI repair service on request.
-- Generated from a template; the adapter code itself is in courier-adapters/adapters/demosecure/.
-- On a database where somebody added the courier already (Phase 12: status 'building'), its
-- row is kept as it is (with its login and recorded permission) and only the adapter is
-- added; elsewhere the row is created here. Either way the courier is live afterwards.
-- Elsewhere the provider cannot be used until its credential is stored (admin set-credential).
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential,
                       min_dispatch_interval_ms)
VALUES ('6ee13b79-2334-4c23-a961-09a10f443c27', 'demosecure', 'Demo Secure Logistics', 'scrape',
        20000, 900000, 30000, 3, 5000, 300000, true, 2000)
ON CONFLICT (code) DO NOTHING;

INSERT INTO adapters (id, provider_id, key, method, queue_name)
SELECT 'ce65c73b-8d13-4dbe-ade9-9c2239b5a065', id, 'demosecure', 'scrape', 'adapter.demosecure.jobs'
  FROM providers WHERE code = 'demosecure'
ON CONFLICT (key) DO NOTHING;

-- The status codes of courier-adapters/adapters/india.py. Wording the adapter does not
-- recognize arrives unchanged and appears in the list of unmapped statuses.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
SELECT m.id::uuid, p.id, m.raw_status, m.normalized_status::shipment_status
  FROM providers p, (VALUES
    ('fbcfb7b1-b6f8-4a02-8204-2a4e0a5d5236', 'booked', 'CREATED'),
    ('f2d5cd27-c837-41e5-9b3d-7e18779d1ad0', 'picked-up', 'PICKED_UP'),
    ('61ac66be-b5f6-4b57-80e2-eaf5aa3445c0', 'in-transit', 'IN_TRANSIT'),
    ('56ea8f9f-c90c-469b-9a0b-2806e6910cd9', 'out-for-delivery', 'OUT_FOR_DELIVERY'),
    ('c1724feb-1ce0-421b-82c6-977966ed0e80', 'delivered', 'DELIVERED'),
    ('8bd2c1b6-d8cb-45b4-bd04-7b02433156c0', 'undelivered', 'EXCEPTION'),
    ('de1da2b1-306c-4956-8cca-84d2890c18b7', 'returned', 'RETURNED')
  ) AS m (id, raw_status, normalized_status)
 WHERE p.code = 'demosecure'
ON CONFLICT (provider_id, raw_status) DO NOTHING;

UPDATE providers SET status = 'live', build_failure = NULL, updated_at = now()
 WHERE code = 'demosecure';
