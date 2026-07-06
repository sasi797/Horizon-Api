-- Migration: add graph_message_id to email_messages
-- Stores the Graph API internal message ID so the reply endpoint can use
-- the /messages/{id}/reply endpoint for proper email thread chaining.
ALTER TABLE email_messages
    ADD COLUMN IF NOT EXISTS graph_message_id VARCHAR(500);
