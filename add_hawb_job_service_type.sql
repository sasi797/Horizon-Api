-- Adds the job service type (delivery / collection / collection and delivery)
-- selector shown on the Job Detail UI above the shipper/consignee panels.

ALTER TABLE hawb_jobs
    ADD COLUMN IF NOT EXISTS job_service_type VARCHAR(30);
