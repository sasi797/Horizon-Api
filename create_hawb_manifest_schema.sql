-- HAWB Manifest Tool — initial database + schema
--
-- HOW TO RUN IN pgAdmin:
--   1. Open a Query Tool connected to any existing database (e.g. "postgres").
--      Run ONLY the CREATE DATABASE statement below (it cannot run inside a
--      transaction block together with other statements).
--   2. Right-click the new "horizon_dev" database in the tree -> Query Tool,
--      so you are now connected to horizon_dev.
--   3. Run everything from "-- ### SCHEMA" downward in that new Query Tool tab.

-- ### STEP 1 — run this alone, connected to any existing database
CREATE DATABASE horizon_dev;

-- ### STEP 2 — run everything below, connected to the horizon_dev database

-- ### SCHEMA -------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------
-- roles / users — operator accounts for this app (Stage 2/3 auth)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roles (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(50) NOT NULL UNIQUE,
    key         VARCHAR(50) NOT NULL UNIQUE,
    permissions TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO roles (name, key, permissions) VALUES
    ('Admin',    'admin',    'all'),
    ('Operator', 'operator', 'jobs:read,jobs:edit,manifest:create')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS users (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name          VARCHAR(100) NOT NULL,
    email         VARCHAR(150) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role_id       UUID         NOT NULL REFERENCES roles(id),
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------
-- hawb_processed_emails — Stage 1 mailbox dedup so the 1-minute poll
-- never re-ingests the same email twice.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hawb_processed_emails (
    message_id   VARCHAR(998) PRIMARY KEY,
    processed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------
-- hawb_documents — Stage 1: one row per PDF attachment that arrived.
-- A single email can carry more than one PDF, so this is keyed off the
-- attachment, not the email. The file itself lives in S3
-- (bucket "horizon-dev"); only the object key is stored here.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hawb_documents (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    source_message_id VARCHAR(998),
    sender_email      VARCHAR(150),
    subject           VARCHAR(255),
    filename          VARCHAR(255) NOT NULL,
    storage_bucket    VARCHAR(100) NOT NULL DEFAULT 'horizon-dev',
    storage_key       TEXT         NOT NULL,
    received_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    processed_at      TIMESTAMPTZ,
    job_count         INTEGER      NOT NULL DEFAULT 0,
    status            VARCHAR(20)  NOT NULL DEFAULT 'processed'
                        CHECK (status IN ('processed', 'failed')),
    error_message     TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hawb_documents_source_message_id ON hawb_documents(source_message_id);
CREATE INDEX IF NOT EXISTS idx_hawb_documents_received_at ON hawb_documents(received_at DESC);

-- ---------------------------------------------------------------------
-- hawb_manifests — Stage 3: a manifest groups one or more "Ready" jobs
-- under a single reference number and locks them.
-- ---------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS hawb_manifest_ref_seq START 1;

CREATE TABLE IF NOT EXISTS hawb_manifests (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    reference_number VARCHAR(20) NOT NULL UNIQUE
                        DEFAULT ('MNF-' || lpad(nextval('hawb_manifest_ref_seq')::text, 4, '0')),
    job_count       INTEGER      NOT NULL,
    total_weight_kg NUMERIC(10,2) NOT NULL,
    created_by      UUID         NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hawb_manifests_created_at ON hawb_manifests(created_at DESC);

-- ---------------------------------------------------------------------
-- hawb_jobs — Stage 2/3: one row per HAWB number split out of a PDF.
-- extracted_data keeps the original Stage-1 read as a JSON snapshot so
-- operator corrections in the editable columns can always be diffed
-- against what the PDF actually said.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hawb_jobs (
    id                    UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id           UUID          NOT NULL REFERENCES hawb_documents(id) ON DELETE CASCADE,
    hawb_number           VARCHAR(50)   NOT NULL UNIQUE,
    shipper               VARCHAR(255),
    consignee             VARCHAR(255),
    collection_at         TIMESTAMPTZ,
    delivery_at           TIMESTAMPTZ,
    package_qty           INTEGER,
    dangerous_goods       BOOLEAN       NOT NULL DEFAULT FALSE,
    dangerous_goods_notes TEXT,
    weight_kg             NUMERIC(10,2),
    extracted_data        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    status                VARCHAR(20)   NOT NULL DEFAULT 'pending_review'
                            CHECK (status IN ('pending_review', 'ready_to_manifest', 'manifested')),
    manifest_id           UUID          REFERENCES hawb_manifests(id) ON DELETE SET NULL,
    locked                BOOLEAN       NOT NULL DEFAULT FALSE,
    ready_at              TIMESTAMPTZ,
    manifested_at         TIMESTAMPTZ,
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hawb_jobs_document_id ON hawb_jobs(document_id);
CREATE INDEX IF NOT EXISTS idx_hawb_jobs_manifest_id ON hawb_jobs(manifest_id);
CREATE INDEX IF NOT EXISTS idx_hawb_jobs_status ON hawb_jobs(status);
CREATE INDEX IF NOT EXISTS idx_hawb_jobs_created_at ON hawb_jobs(created_at DESC);
