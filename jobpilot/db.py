"""Neon (Postgres) data layer.

Migrated from SQLite 2026-07-27. Talks to the database in $DATABASE_URL (loaded
from the project .env). Schema lives in migrations/001_init.sql — apply it with
`python migrations/run.py` before first use.

Connections are opened per unit of work with `connect()`, use dict rows, and
disable prepared statements so they work through Neon's PgBouncer pooler.

Job lifecycle (the jobs.status column):
    new          just collected, not yet filtered
    rejected     failed the hard filter (reason in reject_reason)
    screened     passed hard filter, awaiting LLM scoring
    scored       LLM-scored, below the apply gate (< 60)
    shortlisted  LLM-scored >= 60 — queued for resume + email
    applied      an email actually went out
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent


# ── Connection ───────────────────────────────────────────────────────

def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def dsn() -> str:
    _load_env()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set — add your Neon connection string to .env "
            "(see migrations/run.py)."
        )
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    """Open a Neon connection with dict rows. Caller closes it (or uses `with`)."""
    conn = psycopg.connect(url or dsn(), row_factory=dict_row, prepare_threshold=None)
    return conn


# ── Job model ────────────────────────────────────────────────────────

@dataclass
class Job:
    source: str
    company: str
    title: str
    location: str | None = None
    is_remote: bool = False
    url: str | None = None
    description: str | None = None
    posted_at: str | None = None
    # Re-spec 2026-07-27 additions (all optional so collectors need no changes):
    country: str | None = None
    category: str | None = None            # fresher | internship | new_grad | graduate_program | other
    work_auth_required: bool = False
    summary: str | None = None             # 1-2 line JD summary for the list view
    id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.id:
            raw = (f"{self.company.lower().strip()}|{self.title.lower().strip()}|"
                   f"{(self.location or '').lower().strip()}")
            self.id = hashlib.sha256(raw.encode()).hexdigest()[:20]


# ── Jobs ─────────────────────────────────────────────────────────────

def upsert_jobs(conn: psycopg.Connection, jobs: list[Job]) -> int:
    """Insert jobs, skipping fingerprints we already have. Returns # inserted."""
    inserted = 0
    with conn.cursor() as cur:
        for job in jobs:
            cur.execute(
                """INSERT INTO jobs
                   (id, source, company, title, location, country, is_remote,
                    category, work_auth_required, url, description, summary, posted_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (job.id, job.source, job.company, job.title, job.location, job.country,
                 job.is_remote, job.category, job.work_auth_required, job.url,
                 job.description, job.summary, job.posted_at),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def jobs_with_status(conn: psycopg.Connection, status: str,
                     limit: int | None = None) -> list[dict]:
    sql = "SELECT * FROM jobs WHERE status = %s ORDER BY collected_at DESC"
    params: list = [status]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def set_status(conn: psycopg.Connection, job_id: str, status: str,
               reject_reason: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = %s, reject_reason = %s WHERE id = %s",
            (status, reject_reason, job_id),
        )
    conn.commit()


def set_match(conn: psycopg.Connection, job_id: str, score: int, verdict: dict,
              shortlisted: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = %s, match_score = %s, match_json = %s WHERE id = %s",
            ("shortlisted" if shortlisted else "scored", score, Jsonb(verdict), job_id),
        )
    conn.commit()


def set_job_classification(conn: psycopg.Connection, job_id: str, *,
                           country: str | None = None, category: str | None = None,
                           work_auth_required: bool | None = None,
                           summary: str | None = None) -> None:
    """Update only the classification fields that are provided (COALESCE-style)."""
    sets, params = [], []
    for col, val in (("country", country), ("category", category),
                     ("work_auth_required", work_auth_required), ("summary", summary)):
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(val)
    if not sets:
        return
    params.append(job_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = %s", params)
    conn.commit()


def set_job_sponsorship(conn: psycopg.Connection, job_id: str, signal: str,
                        registry_match: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET sponsorship_signal = %s, sponsorship_registry_match = %s WHERE id = %s",
            (signal, registry_match, job_id))
    conn.commit()


def counts_by_status(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in cur.fetchall()}


def get_job(conn: psycopg.Connection, job_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


# ── Contacts ─────────────────────────────────────────────────────────

def add_contact(conn: psycopg.Connection, company: str, email: str | None = None,
                name: str | None = None, title: str | None = None,
                linkedin_url: str | None = None, source: str = "manual",
                tier: str = "generic", confidence: int = 50,
                verified: bool = False) -> int:
    """Insert or refresh a contact, keyed on (company, email). Returns its row id.

    Upserts rather than ignoring conflicts so re-discovery can *lower* confidence
    (a bounce must win over a stale optimistic score). Name/title/linkedin only
    overwrite when the new value is non-empty, so a thin re-scrape can't erase
    richer earlier data.
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO contacts
               (company, name, title, email, linkedin_url, source, tier, confidence, verified)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (company, email) DO UPDATE SET
                 name         = COALESCE(NULLIF(EXCLUDED.name, ''), contacts.name),
                 title        = COALESCE(NULLIF(EXCLUDED.title, ''), contacts.title),
                 linkedin_url = COALESCE(NULLIF(EXCLUDED.linkedin_url, ''), contacts.linkedin_url),
                 source       = EXCLUDED.source,
                 tier         = EXCLUDED.tier,
                 confidence   = EXCLUDED.confidence,
                 verified     = EXCLUDED.verified
               RETURNING id""",
            (company, name, title, email, linkedin_url, source, tier, confidence, verified),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"] if row else 0


def contacts_for_company(conn: psycopg.Connection, company: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM contacts WHERE company = %s ORDER BY confidence DESC", (company,))
        return cur.fetchall()


# ── Tailored resumes ─────────────────────────────────────────────────

def add_tailored_resume(conn: psycopg.Connection, job_id: str, pdf_path: str | None,
                        docx_path: str | None, ats_score: int | None,
                        keywords_matched: list[str] | None = None,
                        keywords_missing: list[str] | None = None,
                        json_path: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO tailored_resumes
               (job_id, pdf_path, docx_path, json_path, ats_score, keywords_matched, keywords_missing)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (job_id, pdf_path, docx_path, json_path, ats_score,
             Jsonb(keywords_matched or []), Jsonb(keywords_missing or [])),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"] if row else 0


def latest_resume_for_job(conn: psycopg.Connection, job_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM tailored_resumes WHERE job_id = %s ORDER BY created_at DESC LIMIT 1",
            (job_id,))
        return cur.fetchone()


# ── Applications ─────────────────────────────────────────────────────

def upsert_application(conn: psycopg.Connection, job_id: str, channel: str, status: str,
                       contact_id: int | None = None, resume_path: str | None = None,
                       subject: str | None = None, body: str | None = None,
                       portfolio_link: str | None = None, error: str | None = None) -> int:
    """Create or update the application record for (job_id, channel)."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO applications
               (job_id, channel, status, contact_id, resume_path, subject, body, portfolio_link, error)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (job_id, channel) DO UPDATE SET
                 status=EXCLUDED.status, contact_id=EXCLUDED.contact_id,
                 resume_path=EXCLUDED.resume_path, subject=EXCLUDED.subject,
                 body=EXCLUDED.body, portfolio_link=EXCLUDED.portfolio_link,
                 error=EXCLUDED.error
               RETURNING id""",
            (job_id, channel, status, contact_id, resume_path, subject, body,
             portfolio_link, error),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"] if row else 0


def mark_application_sent(conn: psycopg.Connection, application_id: int,
                          next_followup_at=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE applications SET status = 'emailed', sent_at = now(), error = NULL, "
            "next_followup_at = %s WHERE id = %s",
            (next_followup_at, application_id),
        )
    conn.commit()


def record_followup(conn: psycopg.Connection, application_id: int,
                    next_followup_at=None) -> None:
    """Log one follow-up: bump the count, keep status 'emailed', schedule the next bump."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE applications SET followup_count = followup_count + 1, "
            "last_followup_at = now(), next_followup_at = %s WHERE id = %s",
            (next_followup_at, application_id),
        )
    conn.commit()


def set_application_status(conn: psycopg.Connection, application_id: int, status: str,
                          error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE applications SET status = %s, error = %s WHERE id = %s",
                    (status, error, application_id))
    conn.commit()


def match_application_for_reply(conn: psycopg.Connection, from_email: str) -> dict | None:
    """Find the application a reply belongs to, by the contact's address or its domain."""
    from_email = (from_email or "").strip().lower()
    if not from_email:
        return None
    domain = from_email.split("@", 1)[1] if "@" in from_email else ""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.id, a.job_id, j.company FROM applications a
               JOIN jobs j ON j.id = a.job_id
               LEFT JOIN contacts c ON c.id = a.contact_id
               WHERE a.channel = 'email' AND a.sent_at IS NOT NULL
                 AND (LOWER(c.email) = %s OR (%s <> '' AND LOWER(c.email) LIKE %s))
               ORDER BY a.sent_at DESC LIMIT 1""",
            (from_email, domain, f"%@{domain}"))
        return cur.fetchone()


def applications_with_status(conn: psycopg.Connection, status: str,
                             channel: str | None = None) -> list[dict]:
    sql = ("SELECT a.*, j.company, j.title, j.url FROM applications a "
           "JOIN jobs j ON j.id = a.job_id WHERE a.status = %s")
    params: list = [status]
    if channel:
        sql += " AND a.channel = %s"
        params.append(channel)
    with conn.cursor() as cur:
        cur.execute(sql + " ORDER BY a.created_at DESC", params)
        return cur.fetchall()


def followups_due(conn: psycopg.Connection, max_followups: int = 5,
                  limit: int | None = None) -> list[dict]:
    """Emailed applications whose next bump is due and still under the follow-up cap."""
    sql = ("SELECT a.*, j.company, j.title, c.email AS contact_email FROM applications a "
           "JOIN jobs j ON j.id = a.job_id "
           "LEFT JOIN contacts c ON c.id = a.contact_id "
           "WHERE a.status = 'emailed' AND a.followup_count < %s "
           "AND a.next_followup_at IS NOT NULL AND a.next_followup_at <= now() "
           "ORDER BY a.next_followup_at ASC")
    params: list = [int(max_followups)]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def sent_today_count(conn: psycopg.Connection, channel: str) -> int:
    """Emails sent in the last 24h on a channel — for daily-cap enforcement."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE channel = %s "
            "AND sent_at >= now() - interval '1 day'", (channel,))
        row = cur.fetchone()
    return row["n"] if row else 0


# ── Replies (inbound) ────────────────────────────────────────────────

def add_reply(conn: psycopg.Connection, *, message_id: str, from_email: str | None,
              subject: str | None, body: str | None, company: str | None = None,
              application_id: int | None = None, job_id: str | None = None,
              sentiment: str | None = None) -> int | None:
    """Insert an inbound reply, deduped on IMAP Message-ID. Returns id, or None if dup."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO replies
               (message_id, from_email, subject, body, company, application_id, job_id, sentiment)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (message_id) DO NOTHING RETURNING id""",
            (message_id, from_email, subject, body, company, application_id, job_id, sentiment),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"] if row else None


def unread_replies(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM replies ORDER BY received_at DESC LIMIT %s", (limit,))
        return cur.fetchall()


def mark_reply_read(conn: psycopg.Connection, reply_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE replies SET is_read = TRUE WHERE id = %s", (reply_id,))
    conn.commit()


# ── Notifications (the bell) ─────────────────────────────────────────

def add_notification(conn: psycopg.Connection, kind: str, title: str,
                     body: str | None = None, link: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notifications (kind, title, body, link) VALUES (%s, %s, %s, %s) RETURNING id",
            (kind, title, body, link))
        row = cur.fetchone()
    conn.commit()
    return row["id"] if row else 0


def unread_notification_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE is_read = FALSE")
        return cur.fetchone()["n"]


def recent_notifications(conn: psycopg.Connection, limit: int = 20) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT %s", (limit,))
        return cur.fetchall()


def mark_all_notifications_read(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("UPDATE notifications SET is_read = TRUE WHERE is_read = FALSE")
        n = cur.rowcount
    conn.commit()
    return n


# ── Agent runs (health page) ─────────────────────────────────────────

def start_run(conn: psycopg.Connection, kind: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs (kind, status) VALUES (%s, 'running') RETURNING id", (kind,))
        row = cur.fetchone()
    conn.commit()
    return row["id"] if row else 0


def finish_run(conn: psycopg.Connection, run_id: int, *, status: str = "ok",
               stats: dict | None = None, error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_runs SET status = %s, stats = %s, error = %s, finished_at = now() "
            "WHERE id = %s",
            (status, Jsonb(stats) if stats is not None else None, error, run_id),
        )
    conn.commit()


def recent_runs(conn: psycopg.Connection, limit: int = 20) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT %s", (limit,))
        return cur.fetchall()


# ── To-dos (page 4) ──────────────────────────────────────────────────

def add_todo(conn: psycopg.Connection, *, company: str | None = None, title: str | None = None,
             portal_url: str | None = None, note: str | None = None,
             job_id: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO todos (job_id, company, title, portal_url, note) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (job_id, company, title, portal_url, note))
        row = cur.fetchone()
    conn.commit()
    return row["id"] if row else 0


def list_todos(conn: psycopg.Connection, include_done: bool = False) -> list[dict]:
    sql = "SELECT * FROM todos"
    if not include_done:
        sql += " WHERE done = FALSE"
    sql += " ORDER BY done, created_at DESC"
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def set_todo_done(conn: psycopg.Connection, todo_id: int, done: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE todos SET done = %s WHERE id = %s", (done, todo_id))
    conn.commit()
