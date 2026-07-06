-- Adds the source-PDF starting page number for each HAWB job, so the Job Detail
-- UI can jump the PDF viewer to the right page and step between sibling jobs
-- from the same document in page order.

ALTER TABLE hawb_jobs
    ADD COLUMN IF NOT EXISTS page_start INTEGER;
