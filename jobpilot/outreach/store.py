"""Local persistence for the free-finder layer.

These tables are **owned by `jobpilot/outreach/`** and created lazily with
`CREATE TABLE IF NOT EXISTS` so that nothing in `jobpilot/db.py` has to change.
(Integrator: see NOTES_FREE_FINDER.md — folding this DDL into `db.SCHEMA` would
be tidier, but it is not required for correctness.)

Two things are remembered here, and both exist to stop us re-learning expensive
lessons:

* `domain_intel` — per-domain MX / catch-all facts. A catch-all probe costs a
  real SMTP connection to someone else's mail server; we do it once per domain
  per TTL, never once per address.
* `dead_patterns` — local-parts that actually bounced, per domain. This is the
  feedback loop that no paid API gives you: after `bounce.py` sees a DSN for
  `first.last@acme.com`, the `first.last` pattern is demoted for acme.com
  forever, so the next Acme guess tries something else.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

DDL = """
CREATE TABLE IF NOT EXISTS domain_intel (
    domain        TEXT PRIMARY KEY,
    mx_hosts      TEXT,            -- JSON list, empty list = domain cannot receive mail
    catch_all     INTEGER,         -- 1 yes / 0 no / NULL undetermined
    catch_all_at  TEXT,            -- when the catch-all probe last ran
    probe_note    TEXT,            -- why we concluded what we concluded
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_patterns (
    domain      TEXT NOT NULL,
    local_part  TEXT NOT NULL,     -- 'first.last' template OR a literal local part
    reason      TEXT,
    bounces     INTEGER DEFAULT 1,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (domain, local_part)
);

CREATE TABLE IF NOT EXISTS bounce_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL,
    smtp_code   TEXT,
    kind        TEXT,              -- hard | soft | unknown
    detail      TEXT,
    seen_at     TEXT NOT NULL,
    UNIQUE(email, seen_at)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Idempotent. Safe to call on every entry point."""
    conn.executescript(DDL)
    conn.commit()


# ── domain intel ─────────────────────────────────────────────────────

def get_domain_intel(conn: sqlite3.Connection, domain: str) -> dict | None:
    ensure_tables(conn)
    row = conn.execute("SELECT * FROM domain_intel WHERE domain = ?",
                       (domain.lower(),)).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["mx_hosts"] = json.loads(data["mx_hosts"] or "[]")
    except (ValueError, TypeError):
        data["mx_hosts"] = []
    if data.get("catch_all") is not None:
        data["catch_all"] = bool(data["catch_all"])
    return data


def save_domain_intel(conn: sqlite3.Connection, domain: str,
                      mx_hosts: list[str] | None = None,
                      catch_all: bool | None = None,
                      probe_note: str | None = None) -> None:
    """Upsert. Passing None for a field leaves the stored value alone."""
    ensure_tables(conn)
    domain = domain.lower()
    existing = get_domain_intel(conn, domain) or {}
    mx = mx_hosts if mx_hosts is not None else existing.get("mx_hosts") or []
    ca = catch_all if catch_all is not None else existing.get("catch_all")
    note = probe_note or existing.get("probe_note")
    ca_at = _now() if catch_all is not None else existing.get("catch_all_at")
    conn.execute(
        """INSERT INTO domain_intel (domain, mx_hosts, catch_all, catch_all_at, probe_note, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(domain) DO UPDATE SET
             mx_hosts=excluded.mx_hosts, catch_all=excluded.catch_all,
             catch_all_at=excluded.catch_all_at, probe_note=excluded.probe_note,
             updated_at=excluded.updated_at""",
        (domain, json.dumps(mx), None if ca is None else int(ca), ca_at, note, _now()),
    )
    conn.commit()


def catch_all_is_fresh(intel: dict | None, ttl_days: int = 30) -> bool:
    """Catch-all config changes rarely, but it does change — re-probe after TTL."""
    if not intel or intel.get("catch_all") is None or not intel.get("catch_all_at"):
        return False
    try:
        when = datetime.fromisoformat(intel["catch_all_at"])
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < timedelta(days=ttl_days)


# ── dead patterns (bounce feedback) ──────────────────────────────────

def mark_pattern_dead(conn: sqlite3.Connection, domain: str, local_part: str,
                      reason: str = "bounced") -> None:
    ensure_tables(conn)
    conn.execute(
        """INSERT INTO dead_patterns (domain, local_part, reason, bounces, updated_at)
           VALUES (?, ?, ?, 1, ?)
           ON CONFLICT(domain, local_part) DO UPDATE SET
             bounces = dead_patterns.bounces + 1, reason = excluded.reason,
             updated_at = excluded.updated_at""",
        (domain.lower(), local_part.lower(), reason, _now()),
    )
    conn.commit()


def dead_patterns_for(conn: sqlite3.Connection, domain: str) -> set[str]:
    ensure_tables(conn)
    rows = conn.execute("SELECT local_part FROM dead_patterns WHERE domain = ?",
                        (domain.lower(),)).fetchall()
    return {r["local_part"] if isinstance(r, sqlite3.Row) else r[0] for r in rows}


def log_bounce(conn: sqlite3.Connection, email: str, smtp_code: str | None,
               kind: str, detail: str | None, seen_at: str | None = None) -> None:
    ensure_tables(conn)
    conn.execute(
        """INSERT OR IGNORE INTO bounce_log (email, smtp_code, kind, detail, seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        (email.lower(), smtp_code, kind, (detail or "")[:500], seen_at or _now()),
    )
    conn.commit()


def bounced_emails(conn: sqlite3.Connection) -> set[str]:
    """Every address we have ever seen bounce — never send to these again."""
    ensure_tables(conn)
    rows = conn.execute("SELECT DISTINCT email FROM bounce_log WHERE kind = 'hard'").fetchall()
    return {r["email"] if isinstance(r, sqlite3.Row) else r[0] for r in rows}
