-- Adds manifest export lifecycle (draft/exported + timestamp) and a per-job
-- run-order sequence within a manifest, for the manifest detail page's
-- drag-to-reorder run order and Export manifest action.

ALTER TABLE hawb_manifests
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'exported')),
    ADD COLUMN IF NOT EXISTS exported_at TIMESTAMPTZ;

ALTER TABLE hawb_jobs
    ADD COLUMN IF NOT EXISTS manifest_sequence INTEGER;
