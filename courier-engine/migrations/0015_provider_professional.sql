-- Provider 'professional' (The Professional Couriers), added by the AI repair service on request.
-- Generated from a template; the adapter code itself is in courier-adapters/adapters/professional/.
-- The provider cannot be used until its owner's permission is recorded (admin set-scraping-authorized).
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential,
                       min_dispatch_interval_ms)
VALUES ('1dbcf2c8-7960-46f4-b396-6e42614b0ec0', 'professional', 'The Professional Couriers', 'scrape',
        20000, 900000, 30000, 3, 5000, 300000, false, 2000);

INSERT INTO adapters (id, provider_id, key, method, queue_name)
VALUES ('bc6cd04f-abdd-4785-be8e-e4603a3ea73a', '1dbcf2c8-7960-46f4-b396-6e42614b0ec0',
        'professional', 'scrape', 'adapter.professional.jobs');

-- The status codes of courier-adapters/adapters/india.py. Wording the adapter does not
-- recognize arrives unchanged and appears in the list of unmapped statuses.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
VALUES
    ('e671c8d7-d1e9-43c8-bffd-27ab257a4e24', '1dbcf2c8-7960-46f4-b396-6e42614b0ec0', 'booked', 'CREATED'),
    ('86f1b5a0-0ab0-4019-82be-1e6164052e92', '1dbcf2c8-7960-46f4-b396-6e42614b0ec0', 'picked-up', 'PICKED_UP'),
    ('d5b6610b-905d-422b-8ff5-90dc3deef43b', '1dbcf2c8-7960-46f4-b396-6e42614b0ec0', 'in-transit', 'IN_TRANSIT'),
    ('7c87b9dd-c1f5-433b-b319-855b2b961280', '1dbcf2c8-7960-46f4-b396-6e42614b0ec0', 'out-for-delivery', 'OUT_FOR_DELIVERY'),
    ('05cf0e30-1f0d-4584-ae84-414140f5f739', '1dbcf2c8-7960-46f4-b396-6e42614b0ec0', 'delivered', 'DELIVERED'),
    ('54a899ef-3056-4154-b102-c5ecdcca2ee0', '1dbcf2c8-7960-46f4-b396-6e42614b0ec0', 'undelivered', 'EXCEPTION'),
    ('f7e4ce57-35f4-487f-a57e-974e66ade51e', '1dbcf2c8-7960-46f4-b396-6e42614b0ec0', 'returned', 'RETURNED');
