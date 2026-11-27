-- Provider 'demoexpress' (Demo Express), added by the AI repair service on request.
-- Generated from a template; the adapter code itself is in courier-adapters/adapters/demoexpress/.
-- On a database where somebody added the courier already (Phase 12: status 'building'), its
-- row is kept as it is (with its login and recorded permission) and only the adapter is
-- added; elsewhere the row is created here. Either way the courier is live afterwards.
-- Elsewhere the provider cannot be used until its owner's permission is recorded (admin set-scraping-authorized).
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential,
                       min_dispatch_interval_ms)
VALUES ('4a7ee18a-3330-4fc7-af46-4dcd7929bfd8', 'demoexpress', 'Demo Express', 'scrape',
        20000, 900000, 30000, 3, 5000, 300000, false, 2000)
ON CONFLICT (code) DO NOTHING;

INSERT INTO adapters (id, provider_id, key, method, queue_name)
SELECT 'f4045fc0-29ad-42c5-b24d-d4c1a5cadcb8', id, 'demoexpress', 'scrape', 'adapter.demoexpress.jobs'
  FROM providers WHERE code = 'demoexpress'
ON CONFLICT (key) DO NOTHING;

-- The status codes of courier-adapters/adapters/india.py. Wording the adapter does not
-- recognize arrives unchanged and appears in the list of unmapped statuses.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
SELECT m.id::uuid, p.id, m.raw_status, m.normalized_status::shipment_status
  FROM providers p, (VALUES
    ('661ed8f8-3bba-4a9d-b1b7-c34d1aa55825', 'booked', 'CREATED'),
    ('69c56736-baf9-4941-a4ed-3a4d0f15af21', 'picked-up', 'PICKED_UP'),
    ('5e43574b-0434-4de3-a54e-7a52b74750b0', 'in-transit', 'IN_TRANSIT'),
    ('67e47360-9e5f-4667-ab0f-71cf0db275e3', 'out-for-delivery', 'OUT_FOR_DELIVERY'),
    ('96d30186-bfe0-4435-9e84-81ddc47015bf', 'delivered', 'DELIVERED'),
    ('751f1378-a0ed-4a45-9f77-848182884ae9', 'undelivered', 'EXCEPTION'),
    ('df9a90c3-90a0-40df-9980-ce7bec7de229', 'returned', 'RETURNED')
  ) AS m (id, raw_status, normalized_status)
 WHERE p.code = 'demoexpress'
ON CONFLICT (provider_id, raw_status) DO NOTHING;

UPDATE providers SET status = 'live', build_failure = NULL, updated_at = now()
 WHERE code = 'demoexpress';
