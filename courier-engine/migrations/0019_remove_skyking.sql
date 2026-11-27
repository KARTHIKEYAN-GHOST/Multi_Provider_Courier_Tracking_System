-- Phase 11: the SkyKing adapter is removed from the system.
--
-- Its rows go with it. A provider that already has shipments on record cannot vanish
-- without losing their history; such an installation keeps the rows, switched off, and
-- nothing is sent to SkyKing any more.
DO $$
DECLARE
    skyking constant uuid := '01900000-0000-7000-8000-000000000501';
BEGIN
    IF EXISTS (SELECT 1 FROM shipments WHERE provider_id = skyking) THEN
        UPDATE adapters SET enabled = false, updated_at = now() WHERE provider_id = skyking;
        UPDATE providers SET enabled = false, scraping_authorized = false, updated_at = now()
        WHERE id = skyking;
    ELSE
        DELETE FROM worker_tokens
        WHERE adapter_id IN (SELECT id FROM adapters WHERE provider_id = skyking);
        DELETE FROM adapter_versions
        WHERE adapter_id IN (SELECT id FROM adapters WHERE provider_id = skyking);
        DELETE FROM provider_credentials WHERE provider_id = skyking;
        DELETE FROM provider_status_mappings WHERE provider_id = skyking;
        DELETE FROM adapters WHERE provider_id = skyking;
        DELETE FROM providers WHERE id = skyking;
    END IF;
    -- Repair work for an adapter that no longer exists has nothing left to do.
    DELETE FROM repair_tasks WHERE adapter_key = 'skyking' AND status IN ('pending', 'running');
END
$$;
