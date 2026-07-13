-- Blind HAWB feature: an attachment filename containing "MF-PCS" marks a
-- shipment whose shipping-label PDF has some fields (which fields varies)
-- redacted; a companion "booking form" PDF for the same hawb_number supplies
-- the real values, and the email body may contribute further fields. The
-- merged result is a "blind" job/document, as opposed to today's "plain"
-- single-PDF jobs.
--
-- source_kind distinguishes plain vs blind on documents, jobs, and (denormalized,
-- same pattern as job_count/total_weight_kg) manifests, so the manifest list
-- page can badge rows without an extra join. email_body_text stores the body
-- used for extraction so reviewers can cross-check it. blind_document_id links
-- a merged job back to the booking-form document that contributed to it.

ALTER TABLE hawb_documents
    ADD COLUMN IF NOT EXISTS source_kind VARCHAR(20) NOT NULL DEFAULT 'plain',
    ADD COLUMN IF NOT EXISTS email_body_text TEXT;

ALTER TABLE hawb_jobs
    ADD COLUMN IF NOT EXISTS source_kind VARCHAR(20) NOT NULL DEFAULT 'plain',
    ADD COLUMN IF NOT EXISTS blind_document_id UUID REFERENCES hawb_documents(id);

ALTER TABLE hawb_manifests
    ADD COLUMN IF NOT EXISTS source_kind VARCHAR(20) NOT NULL DEFAULT 'plain';
