-- Adds the remaining HAWB Manifest Tool functional-spec "Data dictionary" fields
-- (section 8) to hawb_jobs as first-class columns, so they're editable in the
-- Job Detail UI instead of sitting only inside extracted_data.extra.

ALTER TABLE hawb_jobs
    ADD COLUMN IF NOT EXISTS client_account          VARCHAR(50),
    ADD COLUMN IF NOT EXISTS package_sequence        VARCHAR(20),
    ADD COLUMN IF NOT EXISTS shipper_contact         VARCHAR(150),
    ADD COLUMN IF NOT EXISTS shipper_phone           VARCHAR(50),
    ADD COLUMN IF NOT EXISTS shipper_reference       VARCHAR(100),
    ADD COLUMN IF NOT EXISTS consignee_contact       VARCHAR(150),
    ADD COLUMN IF NOT EXISTS consignee_phone         VARCHAR(50),
    ADD COLUMN IF NOT EXISTS consignee_reference     VARCHAR(100),
    ADD COLUMN IF NOT EXISTS temperature_range       VARCHAR(100),
    ADD COLUMN IF NOT EXISTS dimensions              VARCHAR(100),
    ADD COLUMN IF NOT EXISTS volumetric_weight_kg    NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS declared_value          NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS declared_value_currency VARCHAR(10),
    ADD COLUMN IF NOT EXISTS direction               VARCHAR(20),
    ADD COLUMN IF NOT EXISTS special_handling        TEXT,
    ADD COLUMN IF NOT EXISTS packages                JSONB NOT NULL DEFAULT '[]'::jsonb;
