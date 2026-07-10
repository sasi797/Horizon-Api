-- Client requirement change: manifests are now auto-created by the ingestion
-- pipeline (one PDF = one manifest) instead of manually by an operator, so
-- created_by no longer always has a user. Also adds the manually-entered
-- start/end point fields for the driver run.

ALTER TABLE hawb_manifests
    ALTER COLUMN created_by DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS start_point TEXT,
    ADD COLUMN IF NOT EXISTS end_point TEXT;
