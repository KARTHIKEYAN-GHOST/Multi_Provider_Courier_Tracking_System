-- Provider 'demoexpress' (Demo Express), added by the AI repair service on request.
-- Generated from a template; the adapter code itself is in courier-adapters/adapters/demoexpress/.
-- On a database where somebody added the courier already (Phase 12: status 'building'), its
-- row is kept as it is (with its login and recorded permission) and only the adapter is
-- added; elsewhere the row is created here. Either way the courier is live afterwards.
-- Elsewhere the provider cannot be used until its owner's permission is recorded (admin set-scraping-authorized).
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential,
                       min_dispatch_interval_ms)
VALUES ('866ac402-0463-4873-8220-fd17cafba4f7', 'demoexpress', 'Demo Express', 'scrape',
        20000, 900000, 30000, 3, 5000, 300000, false, 2000)
ON CONFLICT (code) DO NOTHING;

INSERT INTO adapters (id, provider_id, key, method, queue_name)
SELECT '3d08ccb2-38dd-436f-bbf4-2711dd54e82a', id, 'demoexpress', 'scrape', 'adapter.demoexpress.jobs'
  FROM providers WHERE code = 'demoexpress'
ON CONFLICT (key) DO NOTHING;

-- The status codes of courier-adapters/adapters/india.py. Wording the adapter does not
-- recognize arrives unchanged and appears in the list of unmapped statuses.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
SELECT m.id::uuid, p.id, m.raw_status, m.normalized_status::shipment_status
  FROM providers p, (VALUES
    ('52541fb8-f596-4002-8780-2ccb2fb87584', 'booked', 'CREATED'),
    ('7ba17485-165b-4d35-bd57-fbd94239d7a9', 'picked-up', 'PICKED_UP'),
    ('caad7d67-8084-4a61-962e-b7363811638f', 'in-transit', 'IN_TRANSIT'),
    ('a7e3a52f-f9ba-4da3-9f68-60d35a2c15e0', 'out-for-delivery', 'OUT_FOR_DELIVERY'),
    ('6bd8031b-6f37-4b31-a380-740490f065c7', 'delivered', 'DELIVERED'),
    ('eabf1ade-ff9b-414e-9186-f7b582a62ed9', 'undelivered', 'EXCEPTION'),
    ('04f3c3a7-8ec7-4684-9b42-0bba99184d41', 'returned', 'RETURNED')
  ) AS m (id, raw_status, normalized_status)
 WHERE p.code = 'demoexpress'
ON CONFLICT (provider_id, raw_status) DO NOTHING;

UPDATE providers SET status = 'live', build_failure = NULL, updated_at = now()
 WHERE code = 'demoexpress';
