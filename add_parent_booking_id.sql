-- Migration: Add parent_booking_id self-referential FK to bookings table
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS parent_booking_id VARCHAR(25)
        REFERENCES bookings(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_bookings_parent_booking_id
    ON bookings(parent_booking_id);
