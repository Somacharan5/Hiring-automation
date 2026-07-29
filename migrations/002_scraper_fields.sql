-- JobPilot migration 002 — richer scraper fields (2026-07-29).
-- From pm-job-scraper-spec.md: company_domain (also fixes email-discovery guessing),
-- visa-sponsorship signal + which registry matched, and salary. Idempotent.

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS company_domain              TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS sponsorship_signal         TEXT;   -- registry_confirmed | jd_mentioned | unknown | likely_no
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS sponsorship_registry_match TEXT;   -- UK_sponsor_register | US_H1B | CA_LMIA | AU_sponsor | none
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS salary_min                 INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS salary_max                 INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS salary_currency            TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_sponsorship ON jobs (sponsorship_signal);
CREATE INDEX IF NOT EXISTS idx_jobs_company_domain ON jobs (company_domain);
