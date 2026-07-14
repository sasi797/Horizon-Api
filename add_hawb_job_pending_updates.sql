-- A newer extraction for a hawb_number that already has a job used to be
-- silently discarded (only a terse note on the source document's
-- error_message). This table holds those "duplicates" for manual review
-- instead of dropping them: reason='duplicate_resend' is a plain field-diff
-- case (a HAWB was re-sent, possibly with corrected data); reason=
-- 'blind_companion_merge' is the case where the "duplicate" is actually the
-- missing plain/MF-PCS companion for an existing unmatched blind job,
-- arriving in a later email — proposed_data there is already the merged
-- result. Never auto-applied, including for already-exported/locked jobs.

CREATE TABLE IF NOT EXISTS hawb_job_pending_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES hawb_jobs(id) ON DELETE CASCADE,
    source_document_id UUID NOT NULL REFERENCES hawb_documents(id) ON DELETE CASCADE,
    reason VARCHAR(30) NOT NULL CHECK (reason IN ('duplicate_resend', 'blind_companion_merge')),
    proposed_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'dismissed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_hawb_job_pending_updates_job_id ON hawb_job_pending_updates(job_id);
CREATE INDEX IF NOT EXISTS ix_hawb_job_pending_updates_status ON hawb_job_pending_updates(status);
