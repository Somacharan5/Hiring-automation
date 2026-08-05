-- JobPilot — migration 003.
-- Dual-recipient outreach: an application can now go to the hiring person AND the
-- careers@ inbox in one email. `recipients` is the ordered list of To: addresses;
-- NULL means "fall back to the single linked contact" (back-compatible with old rows).
-- Idempotent. Apply with:  psql "$DATABASE_URL" -f migrations/003_outreach_recipients.sql

ALTER TABLE applications ADD COLUMN IF NOT EXISTS recipients JSONB;
