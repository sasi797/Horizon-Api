-- "Pending Review" becomes a real manifest status (not a separate filtered
-- list): a manifest starts as pending_review if any of its jobs still need
-- approval, and automatically flips to open once every job has been
-- approved (see approve_job / apply_job_update in app/routers/hawb.py).

ALTER TABLE hawb_manifests
    DROP CONSTRAINT IF EXISTS hawb_manifests_status_check;

ALTER TABLE hawb_manifests
    ADD CONSTRAINT hawb_manifests_status_check
    CHECK (status IN ('pending_review', 'open', 'booked', 'confirmed', 'on_hold', 'exported'));
