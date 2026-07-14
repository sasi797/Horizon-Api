-- Manifest status lifecycle: open -> booked -> confirmed/on_hold -> exported,
-- replacing the old draft/exported binary. "Booked" happens via the existing
-- export action (CSV + job lock, unchanged mechanically, just relabeled);
-- confirm/hold/mark-exported are new explicit manual steps a reviewer takes
-- afterward (automatic validation may replace the manual confirm/hold click
-- later, but for now it's a plain button).

ALTER TABLE hawb_manifests
    DROP CONSTRAINT IF EXISTS hawb_manifests_status_check;

UPDATE hawb_manifests SET status = 'open' WHERE status = 'draft';

ALTER TABLE hawb_manifests
    ADD CONSTRAINT hawb_manifests_status_check
    CHECK (status IN ('open', 'booked', 'confirmed', 'on_hold', 'exported'));

ALTER TABLE hawb_manifests
    ALTER COLUMN status SET DEFAULT 'open';
