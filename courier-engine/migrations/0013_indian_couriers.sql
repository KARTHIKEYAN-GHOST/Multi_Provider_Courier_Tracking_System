-- Five Indian couriers: Delhivery, Safexpress, SkyKing, ABT Parcel Service, Balaji Roadlines.
-- See docs/indian-couriers.md for how each is tracked and what is still unconfirmed.
--
-- None of them can be used right after this migration, on purpose:
--   * the two API providers need a credential (requires_credential);
--   * the three website providers need the site owner's permission (scraping_authorized).
-- None is polled automatically; an operator sets a polling policy when a provider is in use.

-- ----------------------------------------------------------------------------- delhivery

-- Delhivery order tracking (pull) API. Needs the account's API token (stored as a credential).
-- Delhivery allows 750 calls per 5 minutes per address, hence the dispatch interval.
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential, min_dispatch_interval_ms)
VALUES ('01900000-0000-7000-8000-000000000301', 'delhivery', 'Delhivery', 'api',
        20000, 900000, 30000, 4, 5000, 300000, true, 500);

INSERT INTO adapters (id, provider_id, key, method, queue_name)
VALUES ('01900000-0000-7000-8000-000000000302', '01900000-0000-7000-8000-000000000301',
        'delhivery', 'api', 'adapter.delhivery.jobs');

-- The adapter reports Delhivery's scan type and scan text as the raw status (direction:status).
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
VALUES
    ('01900000-0000-7000-8000-000000000311', '01900000-0000-7000-8000-000000000301', 'UD:Manifested', 'CREATED'),
    ('01900000-0000-7000-8000-000000000312', '01900000-0000-7000-8000-000000000301', 'UD:Not Picked', 'CREATED'),
    ('01900000-0000-7000-8000-000000000313', '01900000-0000-7000-8000-000000000301', 'UD:In Transit', 'IN_TRANSIT'),
    ('01900000-0000-7000-8000-000000000314', '01900000-0000-7000-8000-000000000301', 'UD:Pending', 'IN_TRANSIT'),
    ('01900000-0000-7000-8000-000000000315', '01900000-0000-7000-8000-000000000301', 'UD:Dispatched', 'OUT_FOR_DELIVERY'),
    ('01900000-0000-7000-8000-000000000316', '01900000-0000-7000-8000-000000000301', 'DL:Delivered', 'DELIVERED'),
    ('01900000-0000-7000-8000-000000000317', '01900000-0000-7000-8000-000000000301', 'RT:In Transit', 'RETURNED'),
    ('01900000-0000-7000-8000-000000000318', '01900000-0000-7000-8000-000000000301', 'RT:Pending', 'RETURNED'),
    ('01900000-0000-7000-8000-000000000319', '01900000-0000-7000-8000-000000000301', 'RT:Dispatched', 'RETURNED'),
    ('01900000-0000-7000-8000-000000000320', '01900000-0000-7000-8000-000000000301', 'DL:RTO', 'RETURNED'),
    ('01900000-0000-7000-8000-000000000321', '01900000-0000-7000-8000-000000000301', 'DL:DTO', 'RETURNED'),
    ('01900000-0000-7000-8000-000000000322', '01900000-0000-7000-8000-000000000301', 'CN:Canceled', 'EXCEPTION'),
    ('01900000-0000-7000-8000-000000000323', '01900000-0000-7000-8000-000000000301', 'LT:LOST', 'EXCEPTION');

-- ----------------------------------------------------------------------------- safexpress

-- Safexpress customer waybill tracking API. Needs client id, client secret and API key
-- (stored as a credential). The public website is not usable: it demands an OTP and a CAPTCHA.
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential, min_dispatch_interval_ms)
VALUES ('01900000-0000-7000-8000-000000000401', 'safexpress', 'Safexpress', 'api',
        30000, 900000, 30000, 4, 5000, 300000, true, 1000);

INSERT INTO adapters (id, provider_id, key, method, queue_name)
VALUES ('01900000-0000-7000-8000-000000000402', '01900000-0000-7000-8000-000000000401',
        'safexpress', 'api', 'adapter.safexpress.jobs');

-- The adapter reduces the provider's wording to these codes (courier-adapters/adapters/india.py);
-- wording it does not recognize arrives unchanged and appears in the list of unmapped statuses.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
VALUES
    ('01900000-0000-7000-8000-000000000411', '01900000-0000-7000-8000-000000000401', 'booked', 'CREATED'),
    ('01900000-0000-7000-8000-000000000412', '01900000-0000-7000-8000-000000000401', 'picked-up', 'PICKED_UP'),
    ('01900000-0000-7000-8000-000000000413', '01900000-0000-7000-8000-000000000401', 'in-transit', 'IN_TRANSIT'),
    ('01900000-0000-7000-8000-000000000414', '01900000-0000-7000-8000-000000000401', 'out-for-delivery', 'OUT_FOR_DELIVERY'),
    ('01900000-0000-7000-8000-000000000415', '01900000-0000-7000-8000-000000000401', 'delivered', 'DELIVERED'),
    ('01900000-0000-7000-8000-000000000416', '01900000-0000-7000-8000-000000000401', 'undelivered', 'EXCEPTION'),
    ('01900000-0000-7000-8000-000000000417', '01900000-0000-7000-8000-000000000401', 'returned', 'RETURNED');

-- ----------------------------------------------------------------------------- skyking

-- SkyKing: the data service behind skyking.co/track. A website, so it stays switched off
-- until SkyKing's permission is recorded (admin set-scraping-authorized).
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential, min_dispatch_interval_ms)
VALUES ('01900000-0000-7000-8000-000000000501', 'skyking', 'SkyKing', 'scrape',
        20000, 900000, 30000, 3, 5000, 300000, false, 5000);

INSERT INTO adapters (id, provider_id, key, method, queue_name)
VALUES ('01900000-0000-7000-8000-000000000502', '01900000-0000-7000-8000-000000000501',
        'skyking', 'scrape', 'adapter.skyking.jobs');

-- The adapter reduces the provider's wording to these codes (courier-adapters/adapters/india.py);
-- wording it does not recognize arrives unchanged and appears in the list of unmapped statuses.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
VALUES
    ('01900000-0000-7000-8000-000000000511', '01900000-0000-7000-8000-000000000501', 'booked', 'CREATED'),
    ('01900000-0000-7000-8000-000000000512', '01900000-0000-7000-8000-000000000501', 'picked-up', 'PICKED_UP'),
    ('01900000-0000-7000-8000-000000000513', '01900000-0000-7000-8000-000000000501', 'in-transit', 'IN_TRANSIT'),
    ('01900000-0000-7000-8000-000000000514', '01900000-0000-7000-8000-000000000501', 'out-for-delivery', 'OUT_FOR_DELIVERY'),
    ('01900000-0000-7000-8000-000000000515', '01900000-0000-7000-8000-000000000501', 'delivered', 'DELIVERED'),
    ('01900000-0000-7000-8000-000000000516', '01900000-0000-7000-8000-000000000501', 'undelivered', 'EXCEPTION'),
    ('01900000-0000-7000-8000-000000000517', '01900000-0000-7000-8000-000000000501', 'returned', 'RETURNED');

-- ----------------------------------------------------------------------------- abt

-- ABT Parcel Service: the tracking form on abtlogistics.com. A website, so it stays switched
-- off until ABT's permission is recorded (admin set-scraping-authorized).
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential, min_dispatch_interval_ms)
VALUES ('01900000-0000-7000-8000-000000000601', 'abt', 'ABT Parcel Service', 'scrape',
        20000, 900000, 30000, 3, 10000, 600000, false, 5000);

INSERT INTO adapters (id, provider_id, key, method, queue_name)
VALUES ('01900000-0000-7000-8000-000000000602', '01900000-0000-7000-8000-000000000601',
        'abt', 'scrape', 'adapter.abt.jobs');

-- The adapter reduces the provider's wording to these codes (courier-adapters/adapters/india.py);
-- wording it does not recognize arrives unchanged and appears in the list of unmapped statuses.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
VALUES
    ('01900000-0000-7000-8000-000000000611', '01900000-0000-7000-8000-000000000601', 'booked', 'CREATED'),
    ('01900000-0000-7000-8000-000000000612', '01900000-0000-7000-8000-000000000601', 'picked-up', 'PICKED_UP'),
    ('01900000-0000-7000-8000-000000000613', '01900000-0000-7000-8000-000000000601', 'in-transit', 'IN_TRANSIT'),
    ('01900000-0000-7000-8000-000000000614', '01900000-0000-7000-8000-000000000601', 'out-for-delivery', 'OUT_FOR_DELIVERY'),
    ('01900000-0000-7000-8000-000000000615', '01900000-0000-7000-8000-000000000601', 'delivered', 'DELIVERED'),
    ('01900000-0000-7000-8000-000000000616', '01900000-0000-7000-8000-000000000601', 'undelivered', 'EXCEPTION'),
    ('01900000-0000-7000-8000-000000000617', '01900000-0000-7000-8000-000000000601', 'returned', 'RETURNED');

-- ----------------------------------------------------------------------------- balaji

-- Balaji Roadlines: the LR tracking page of Sri Balaji Transport Lines (Regd.). A website, so
-- it stays switched off until the company's permission is recorded.
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential, min_dispatch_interval_ms)
VALUES ('01900000-0000-7000-8000-000000000701', 'balaji', 'Balaji Roadlines (Sri Balaji Transport Lines)', 'scrape',
        20000, 900000, 30000, 3, 5000, 300000, false, 5000);

INSERT INTO adapters (id, provider_id, key, method, queue_name)
VALUES ('01900000-0000-7000-8000-000000000702', '01900000-0000-7000-8000-000000000701',
        'balaji', 'scrape', 'adapter.balaji.jobs');

-- The adapter reduces the provider's wording to these codes (courier-adapters/adapters/india.py);
-- wording it does not recognize arrives unchanged and appears in the list of unmapped statuses.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
VALUES
    ('01900000-0000-7000-8000-000000000711', '01900000-0000-7000-8000-000000000701', 'booked', 'CREATED'),
    ('01900000-0000-7000-8000-000000000712', '01900000-0000-7000-8000-000000000701', 'picked-up', 'PICKED_UP'),
    ('01900000-0000-7000-8000-000000000713', '01900000-0000-7000-8000-000000000701', 'in-transit', 'IN_TRANSIT'),
    ('01900000-0000-7000-8000-000000000714', '01900000-0000-7000-8000-000000000701', 'out-for-delivery', 'OUT_FOR_DELIVERY'),
    ('01900000-0000-7000-8000-000000000715', '01900000-0000-7000-8000-000000000701', 'delivered', 'DELIVERED'),
    ('01900000-0000-7000-8000-000000000716', '01900000-0000-7000-8000-000000000701', 'undelivered', 'EXCEPTION'),
    ('01900000-0000-7000-8000-000000000717', '01900000-0000-7000-8000-000000000701', 'returned', 'RETURNED');
