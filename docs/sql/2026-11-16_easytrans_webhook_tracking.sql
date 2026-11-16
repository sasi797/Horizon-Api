-- EasyTrans status/POD webhook tracking (app/routers/easytrans_webhook.py).
-- Safe to re-run.

ALTER TABLE hawb_manifests
    ADD COLUMN IF NOT EXISTS easytrans_status     VARCHAR(20),
    ADD COLUMN IF NOT EXISTS easytrans_sub_status VARCHAR(150),
    ADD COLUMN IF NOT EXISTS easytrans_status_at  TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS easytrans_webhook_events (
    id              UUID PRIMARY KEY,
    manifest_id     UUID REFERENCES hawb_manifests(id) ON DELETE SET NULL,
    order_no        VARCHAR(50),
    status          VARCHAR(20),
    sub_status_id   INTEGER,
    sub_status_name VARCHAR(150),
    event_time      TIMESTAMPTZ,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_easytrans_webhook_events_manifest_id ON easytrans_webhook_events (manifest_id);
CREATE INDEX IF NOT EXISTS ix_easytrans_webhook_events_order_no    ON easytrans_webhook_events (order_no);
CREATE INDEX IF NOT EXISTS ix_hawb_manifests_mytransport_order_no  ON hawb_manifests (mytransport_order_no);
