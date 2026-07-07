-- Backfill: correct collection_at / delivery_at values that were stored
-- 5 hours 30 minutes early.
--
-- Root cause (fixed in app/services/hawb_ingest.py and app/schemas/hawb.py):
-- HAWB ingestion parsed the AI-extracted collection/delivery time into a
-- naive (timezone-less) Python datetime before saving it to a `timestamptz`
-- column. Because the DB session's TimeZone is Asia/Kolkata (UTC+5:30), the
-- driver silently reinterpreted that naive local wall-clock time as IST and
-- converted it to UTC, storing 14:00 on the PDF as 08:30 in the database.
--
-- This script re-adds the missing 5:30 to every affected row. It only
-- touches jobs that look untouched since ingestion (updated_at ~= created_at,
-- 5 second tolerance) so a job an operator has already corrected by hand is
-- left alone and not double-shifted.
--
-- HOW TO RUN IN pgAdmin:
--   1. Connect to the horizon_dev database.
--   2. Run STEP 1 first and review the rows it lists.
--   3. Only if that list matches what you expect, run STEP 2.

-- ### STEP 1 — preview affected rows (read-only, safe to run any time)
SELECT
    id,
    hawb_number,
    status,
    collection_at,
    collection_at + INTERVAL '5:30' AS collection_at_fixed,
    delivery_at,
    delivery_at + INTERVAL '5:30' AS delivery_at_fixed,
    created_at,
    updated_at
FROM hawb_jobs
WHERE (collection_at IS NOT NULL OR delivery_at IS NOT NULL)
  AND ABS(EXTRACT(EPOCH FROM (updated_at - created_at))) < 5
ORDER BY created_at;

-- ### STEP 2 — apply the fix to exactly those rows
BEGIN;

UPDATE hawb_jobs
SET
    collection_at = collection_at + INTERVAL '5:30',
    delivery_at   = delivery_at + INTERVAL '5:30'
WHERE (collection_at IS NOT NULL OR delivery_at IS NOT NULL)
  AND ABS(EXTRACT(EPOCH FROM (updated_at - created_at))) < 5;

COMMIT;
