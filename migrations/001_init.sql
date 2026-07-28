-- JobPilot — Neon (Postgres) schema.  Migration 001, initial.
-- Ported from the SQLite store and extended for the 2026-07-27 re-spec:
-- autonomous email-only outreach, 3-day follow-ups, inbound reply capture,
-- and a live dashboard (scraped jobs / applied progress / funnel / to-dos).
--
-- Conventions: TIMESTAMPTZ for all times (UTC), JSONB for structured blobs,
-- BOOLEAN not 0/1, identity columns for surrogate keys.  Idempotent: safe to
-- re-run.  Apply with:  psql "$DATABASE_URL" -f migrations/001_init.sql

-- ─────────────────────────────────────────────────────────────────────
-- jobs — every posting we've ever seen.  Feeds Dashboard Page 1.
--   status lifecycle:
--     new         just collected, not yet filtered
--     rejected    failed the cheap hard filter (reason in reject_reason)
--     screened    passed hard filter, awaiting LLM scoring
--     scored      LLM-scored, below the apply gate (< 60)
--     shortlisted LLM-scored >= 60 — queued for resume + email
--     applied     an email actually went out (see applications)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    id                 TEXT PRIMARY KEY,          -- sha256(company|title|location)[:20]
    source             TEXT NOT NULL,             -- greenhouse | lever | ashby | remotive | adzuna:in | ...
    company            TEXT NOT NULL,
    title              TEXT NOT NULL,
    location           TEXT,
    country            TEXT,                       -- normalised target country (India, Germany, …) or NULL
    is_remote          BOOLEAN NOT NULL DEFAULT FALSE,
    category           TEXT,                       -- fresher | internship | new_grad | graduate_program | other
    work_auth_required BOOLEAN NOT NULL DEFAULT FALSE,  -- JD hard-requires existing/local work authorization
    url                TEXT,                       -- the apply link shown in the dashboard
    description        TEXT,
    summary            TEXT,                       -- 1-2 line JD summary for the list view
    posted_at          TIMESTAMPTZ,
    collected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status             TEXT NOT NULL DEFAULT 'new',
    reject_reason      TEXT,
    match_score        INTEGER,                    -- 0-100, NULL until scored
    match_json         JSONB                       -- full MatchVerdict (score, gaps, hints, …)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs (company);
CREATE INDEX IF NOT EXISTS idx_jobs_country ON jobs (country);
CREATE INDEX IF NOT EXISTS idx_jobs_score   ON jobs (match_score);
CREATE INDEX IF NOT EXISTS idx_jobs_collected ON jobs (collected_at DESC);

-- ─────────────────────────────────────────────────────────────────────
-- contacts — recruiter / HR addresses discovered per company.
--   tier: 'specific' = a named person's mailbox (higher response),
--         'generic'  = careers@/jobs@/hr@ fallback.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contacts (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company       TEXT NOT NULL,
    name          TEXT,
    title         TEXT,
    email         TEXT,
    linkedin_url  TEXT,
    source        TEXT,                            -- hunter | pattern | site_crawl | github | generic | manual
    tier          TEXT NOT NULL DEFAULT 'generic', -- specific | generic
    confidence    INTEGER NOT NULL DEFAULT 50,     -- 0-100
    verified      BOOLEAN NOT NULL DEFAULT FALSE,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company, email)
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts (company);

-- ─────────────────────────────────────────────────────────────────────
-- tailored_resumes — one artifact per (job, version).  The resume is
-- generated from the user's JSON schema (provided later) before emailing.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tailored_resumes (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id           TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    pdf_path         TEXT,
    docx_path        TEXT,
    json_path        TEXT,                          -- the structured resume JSON we rendered from
    ats_score        INTEGER,
    keywords_matched JSONB,
    keywords_missing JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tailored_job ON tailored_resumes (job_id);

-- ─────────────────────────────────────────────────────────────────────
-- applications — one row per (job, channel).  Channel is 'email' for now.
-- Feeds Dashboard Page 2.  status is the outreach pipeline the user asked for:
--     preparing_resume  resume being generated
--     emailed           first email sent
--     followup_1        first bump sent (~3 days later)
--     followup_2        second bump sent (~3 days after that)
--     replied           company wrote back (see replies)
--     denied            explicit rejection
--     failed            send error (details in error)
--     bounced           address bounced — contact downgraded
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS applications (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id           TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    channel          TEXT NOT NULL DEFAULT 'email',
    status           TEXT NOT NULL,
    contact_id       BIGINT REFERENCES contacts (id) ON DELETE SET NULL,
    resume_path      TEXT,
    portfolio_link   TEXT,
    subject          TEXT,
    body             TEXT,
    followup_count   INTEGER NOT NULL DEFAULT 0,
    next_followup_at TIMESTAMPTZ,                   -- when the follow-up engine should act next
    last_followup_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at          TIMESTAMPTZ,
    error            TEXT,
    UNIQUE (job_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_apps_status   ON applications (status);
CREATE INDEX IF NOT EXISTS idx_apps_followup ON applications (next_followup_at)
    WHERE status IN ('emailed', 'followup_1');

-- ─────────────────────────────────────────────────────────────────────
-- replies — inbound mail matched back to an application.  Feeds the Inbox
-- page and the 🔔 bell (unread count = COUNT(*) WHERE NOT is_read).
--   sentiment: interested | rejected | other  (classified, not auto-answered)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS replies (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    application_id BIGINT REFERENCES applications (id) ON DELETE SET NULL,
    job_id         TEXT REFERENCES jobs (id) ON DELETE SET NULL,
    company        TEXT,
    from_email     TEXT,
    subject        TEXT,
    body           TEXT,
    sentiment      TEXT,                            -- interested | rejected | other
    message_id     TEXT UNIQUE,                     -- IMAP Message-ID, for dedup
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_read        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_replies_unread ON replies (is_read, received_at DESC);

-- ─────────────────────────────────────────────────────────────────────
-- notifications — the single feed behind the dashboard bell.
-- Populated from new replies, run failures, and milestones.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       TEXT NOT NULL,                       -- reply | run_error | milestone
    title      TEXT NOT NULL,
    body       TEXT,
    link       TEXT,                                -- dashboard path to open
    is_read    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notif_unread ON notifications (is_read, created_at DESC);

-- ─────────────────────────────────────────────────────────────────────
-- agent_runs — every scheduled run, for the Agent Health page.  Records
-- what ran, how long, and API quota burn (Gemini RPM, Hunter 25/mo, Gmail).
--   kind: scrape | score | email | followup | reply_scan
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_runs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',    -- running | ok | error
    stats       JSONB,                              -- {jobs_new, scored, emailed, quota_used, …}
    error       TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_runs_kind ON agent_runs (kind, started_at DESC);

-- ─────────────────────────────────────────────────────────────────────
-- todos — Dashboard Page 4.  Manual "apply on this portal" reminders,
-- optionally linked to a job so its apply URL travels with the task.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS todos (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id     TEXT REFERENCES jobs (id) ON DELETE SET NULL,
    company    TEXT,
    title      TEXT,
    portal_url TEXT,
    note       TEXT,
    done       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_todos_open ON todos (done, created_at DESC);

-- ─────────────────────────────────────────────────────────────────────
-- app_config — UI-editable settings overlay (Settings page).  Key/value so
-- the dashboard can persist country list, thresholds, and caps to Neon
-- without a schema change.  settings.yaml remains the boot default.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app_config (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
