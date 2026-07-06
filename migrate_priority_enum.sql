-- Migration: rename priority enum values
-- Old: Urgent, Standard, Economy
-- New: Very Urgent, Urgent, Not Urgent
--
-- Run this once against your PostgreSQL database:
--   docker exec -i bts_postgres psql -U <user> -d <dbname> < migrate_priority_enum.sql

BEGIN;

-- 1. Rename the existing enum type out of the way
ALTER TYPE priority_enum RENAME TO priority_enum_old;

-- 2. Create the new enum type
CREATE TYPE priority_enum AS ENUM ('Very Urgent', 'Urgent', 'Not Urgent');

-- 3. Migrate the column data, mapping old values to new
ALTER TABLE bookings
  ALTER COLUMN priority DROP DEFAULT,
  ALTER COLUMN priority TYPE priority_enum
    USING (
      CASE priority::text
        WHEN 'Urgent'   THEN 'Very Urgent'
        WHEN 'Standard' THEN 'Urgent'
        WHEN 'Economy'  THEN 'Not Urgent'
      END
    )::priority_enum,
  ALTER COLUMN priority SET DEFAULT 'Urgent';

-- 4. Drop the old enum type
DROP TYPE priority_enum_old;

COMMIT;
