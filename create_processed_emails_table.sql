-- Run once to create the processed_emails dedup table.
-- This table has no FK to bookings, so deleting bookings never removes these records.
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id   VARCHAR(998) PRIMARY KEY,
    processed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Back-fill from existing email_messages so already-processed emails
-- are not re-ingested after this migration.
INSERT INTO processed_emails (message_id, processed_at)
SELECT DISTINCT message_id, MIN(sent_at)
FROM   email_messages
WHERE  message_id IS NOT NULL
GROUP  BY message_id
ON CONFLICT (message_id) DO NOTHING;
