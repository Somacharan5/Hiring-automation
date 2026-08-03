"""Read-only aggregation layer for the dashboard — Neon (Postgres).

Every function takes an open psycopg connection (row_factory=dict_row) and
returns plain dicts/lists, so templates and the JSON API share the same numbers.
Nothing here writes; the write helpers live in actions.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg

# ── Vocabulary ───────────────────────────────────────────────────────

#: Job lifecycle statuses, in pipeline order (see jobpilot/db.py docstring).
JOB_STATUSES: tuple[str, ...] = (
    "new", "screened", "scored", "shortlisted", "applied", "rejected",
)

#: Statuses a human may set from the UI. Setting a status never sends anything.
MANUAL_STATUSES: tuple[str, ...] = (
    "new", "screened", "scored", "shortlisted", "applied", "rejected",
)

#: The email outreach pipeline shown on the Applications page (Page 2).
#: Follow-ups are tracked by count on the 'emailed' row, not as separate statuses.
APPLICATION_STATUSES: tuple[str, ...] = (
    "preparing_resume", "emailed", "replied", "denied", "failed", "bounced",
)

#: Score buckets requested by the user: tiles at 50 / 70 / 90.
#: (key, label, low, high) — high exclusive.
SCORE_BANDS: tuple[tuple[str, str, int, int], ...] = (
    ("high", "90+", 90, 1000),
    ("good", "70–89", 70, 90),
    ("mid", "50–69", 50, 70),
    ("low", "under 50", -1000, 50),
)

APPLY_GATE = 60  # score >= this is shortlisted / mailed


def band_for(score: int | None) -> str | None:
    if score is None:
        return None
    for key, _label, low, high in SCORE_BANDS:
        if low <= score < high:
            return key
    return "low"


def band_label(key: str | None) -> str:
    for k, label, _lo, _hi in SCORE_BANDS:
        if k == key:
            return label
    return "unscored"


# ── Small helpers ────────────────────────────────────────────────────

def _rows(conn: psycopg.Connection, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _one(conn: psycopg.Connection, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _scalar(conn: psycopg.Connection, sql: str, params: tuple | list = (), default: Any = 0) -> Any:
    row = _one(conn, sql, params)
    if not row:
        return default
    val = next(iter(row.values()))
    return default if val is None else val


# ── Overview / funnel (Page 3) ───────────────────────────────────────

def _status_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in
            _rows(conn, "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


_FUNNEL_STAGES: tuple[tuple[str, str, frozenset[str] | None, str], ...] = (
    ("collected", "Collected", None, "Every job pulled from every source."),
    ("screened", "Passed screen",
     frozenset({"screened", "scored", "shortlisted", "applied"}),
     "Survived the cheap title / experience / work-auth hard filter."),
    ("scored", "LLM-scored", frozenset({"scored", "shortlisted", "applied"}),
     "Graded against your profile by the matcher."),
    ("shortlisted", "Shortlisted (≥60)", frozenset({"shortlisted", "applied"}),
     "At or above the apply gate — queued for resume + email."),
    ("applied", "Emailed", frozenset({"applied"}),
     "An application email actually went out."),
    ("replied", "Replied", frozenset(), "A company wrote back."),
)


def _app_status_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in
            _rows(conn, "SELECT status, COUNT(*) AS n FROM applications GROUP BY status")}


def funnel(conn: psycopg.Connection, counts: dict | None = None,
           app_counts: dict | None = None) -> list[dict[str, Any]]:
    counts = counts if counts is not None else _status_counts(conn)
    app_counts = app_counts if app_counts is not None else _app_status_counts(conn)
    total = sum(counts.values())
    # each job has <=1 email application (UNIQUE job_id+channel), so status counts == distinct-job counts
    applied_from_apps = sum(app_counts.get(s, 0) for s in
                            ("emailed", "followup_1", "followup_2", "replied", "denied"))
    replied_from_apps = sum(app_counts.get(s, 0) for s in ("replied", "denied"))

    stages: list[dict[str, Any]] = []
    prev: int | None = None
    for key, label, members, hint in _FUNNEL_STAGES:
        if key == "collected":
            n = total
        elif key == "applied":
            n = max(counts.get("applied", 0), applied_from_apps)
        elif key == "replied":
            n = replied_from_apps
        else:
            n = sum(counts.get(s, 0) for s in (members or ()))
        if prev is not None:
            n = min(n, prev)
        drop = None if prev is None else prev - n
        stages.append({
            "key": key, "label": label, "hint": hint, "count": n,
            "pct_of_top": round(100 * n / total, 1) if total else 0.0,
            "drop": drop,
            "carry_pct": (round(100 * n / prev, 1) if prev else None),
        })
        prev = n
    leaks = [s for s in stages if s["drop"]]
    if leaks:
        max(leaks, key=lambda s: s["drop"])["is_worst_leak"] = True
    return stages


def reject_reasons(conn: psycopg.Connection, limit: int = 6) -> list[dict[str, Any]]:
    return _rows(conn,
        "SELECT reject_reason AS reason, COUNT(*) AS n FROM jobs "
        "WHERE status = 'rejected' AND reject_reason IS NOT NULL AND reject_reason <> '' "
        "GROUP BY reject_reason ORDER BY n DESC LIMIT %s", (limit,))


def source_breakdown(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = _rows(conn, """
        SELECT source,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
               SUM(CASE WHEN status IN ('shortlisted','applied') THEN 1 ELSE 0 END) AS shortlisted,
               SUM(CASE WHEN match_score IS NOT NULL THEN 1 ELSE 0 END) AS scored,
               AVG(match_score) AS avg_score,
               MAX(match_score) AS best_score
        FROM jobs GROUP BY source ORDER BY total DESC
    """)
    grand = max((r["total"] for r in rows), default=0)
    out = []
    for r in rows:
        total = r["total"] or 0
        rejected = r["rejected"] or 0
        shortlisted = r["shortlisted"] or 0
        live = max(total - rejected - shortlisted, 0)
        out.append({
            "source": r["source"], "total": total, "rejected": rejected, "live": live,
            "shortlisted": shortlisted, "scored": r["scored"] or 0,
            "avg_score": round(float(r["avg_score"]), 1) if r["avg_score"] is not None else None,
            "best_score": r["best_score"],
            "pass_pct": round(100 * (total - rejected) / total, 1) if total else 0.0,
            "width_pct": round(100 * total / grand, 1) if grand else 0.0,
            "seg_rejected_pct": round(100 * rejected / total, 1) if total else 0.0,
            "seg_live_pct": round(100 * live / total, 1) if total else 0.0,
            "seg_shortlisted_pct": round(100 * shortlisted / total, 1) if total else 0.0,
        })
    return out


def score_distribution(conn: psycopg.Connection, counts: dict | None = None) -> dict[str, Any]:
    values = [r["s"] for r in
              _rows(conn, "SELECT match_score AS s FROM jobs WHERE match_score IS NOT NULL")]
    # unscored = NULL score and not rejected = the 'new' + 'screened' buckets
    unscored = ((counts.get("new", 0) + counts.get("screened", 0)) if counts is not None
                else _scalar(conn, "SELECT COUNT(*) FROM jobs WHERE match_score IS NULL AND status <> 'rejected'"))
    bands = []
    for key, label, low, high in SCORE_BANDS:
        n = sum(1 for v in values if low <= v < high)
        bands.append({"key": key, "label": label, "count": n,
                      "pct": round(100 * n / len(values), 1) if values else 0.0})
    peak = max((b["count"] for b in bands), default=0)
    for b in bands:
        b["height_pct"] = round(100 * b["count"] / peak, 1) if peak else 0.0
    ordered = sorted(values)
    median = None
    if ordered:
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else round((ordered[mid - 1] + ordered[mid]) / 2, 1)
    return {
        "bands": bands, "scored_total": len(values), "unscored_total": unscored,
        "avg": round(sum(values) / len(values), 1) if values else None,
        "median": median, "best": max(values) if values else None,
    }


def today_activity(conn: psycopg.Connection, caps: dict[str, int], app_counts: dict | None = None,
                   email_sent: int | None = None, collected_24h: int | None = None) -> dict[str, Any]:
    cap = int(caps.get("email_cap") or 0)
    sent = email_sent if email_sent is not None else _scalar(conn,
        "SELECT COUNT(*) FROM applications WHERE channel = 'email' "
        "AND sent_at >= now() - interval '1 day'")
    app_counts = app_counts if app_counts is not None else _app_status_counts(conn)
    return {
        "email": {"sent": sent, "cap": cap,
                  "pct": round(100 * sent / cap, 1) if cap else 0.0,
                  "remaining": max(cap - sent, 0) if cap else None},
        "preparing": app_counts.get("preparing_resume", 0),
        "emailed": app_counts.get("emailed", 0),
        "following_up": app_counts.get("followup_1", 0) + app_counts.get("followup_2", 0),
        "replied": app_counts.get("replied", 0),
        "failed": app_counts.get("failed", 0),
        "collected_last_24h": collected_24h if collected_24h is not None else _scalar(conn,
            "SELECT COUNT(*) FROM jobs WHERE collected_at >= now() - interval '1 day'"),
        "dry_run": bool(caps.get("dry_run", True)),
    }


def overview(conn: psycopg.Connection, caps: dict[str, int]) -> dict[str, Any]:
    # Batched: 6 round-trips instead of ~15. Shared counts fetched once and passed
    # down; all the scalar totals collapse into a single query.
    counts = _status_counts(conn)                       # Q1: job status
    app_counts = _app_status_counts(conn)               # Q2: application status
    summ = _one(conn, """                                -- Q3: every scalar total in one round-trip
        SELECT (SELECT COUNT(*) FROM contacts) AS contacts,
               (SELECT COUNT(DISTINCT company) FROM jobs) AS companies,
               (SELECT COUNT(*) FROM applications) AS applications,
               (SELECT COUNT(*) FROM tailored_resumes) AS resumes,
               (SELECT MAX(collected_at) FROM jobs) AS last_collected,
               (SELECT COUNT(*) FROM jobs WHERE collected_at >= now() - interval '1 day') AS collected_24h,
               (SELECT COUNT(*) FROM applications WHERE channel='email'
                  AND sent_at >= now() - interval '1 day') AS email_sent_24h
    """) or {}
    total_jobs = sum(counts.values())
    stages = funnel(conn, counts, app_counts)           # 0 queries (uses prefetched)
    by_key = {s["key"]: s["count"] for s in stages}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "jobs": total_jobs,
            "rejected": counts.get("rejected", 0),
            "screened": by_key.get("screened", 0),
            "scored": by_key.get("scored", 0),
            "shortlisted": by_key.get("shortlisted", 0),
            "applied": by_key.get("applied", 0),
            "replied": by_key.get("replied", 0),
            "awaiting_score": counts.get("screened", 0),
            "contacts": summ.get("contacts", 0),
            "companies": summ.get("companies", 0),
            "applications": summ.get("applications", 0),
            "resumes": summ.get("resumes", 0),
        },
        "funnel": stages,
        "reject_reasons": reject_reasons(conn),          # Q4
        "sources": source_breakdown(conn),               # Q5
        "scores": score_distribution(conn, counts),      # Q6
        "today": today_activity(conn, caps, app_counts,
                                summ.get("email_sent_24h"), summ.get("collected_24h")),  # 0 queries
        "last_collected_at": summ.get("last_collected"),
        "apply_gate": APPLY_GATE,
    }


# ── Jobs list (Page 1) ───────────────────────────────────────────────

_SORTABLE = {
    "company": "LOWER(company)", "title": "LOWER(title)",
    "location": "LOWER(COALESCE(location, ''))", "source": "LOWER(source)",
    "score": "match_score", "status": "status", "collected": "collected_at",
}


def job_buckets(conn: psycopg.Connection) -> dict[str, Any]:
    """Summary tiles for Page 1: total scraped + counts at each score threshold."""
    total = _scalar(conn, "SELECT COUNT(*) FROM jobs")
    def above(n: int) -> int:
        return _scalar(conn, "SELECT COUNT(*) FROM jobs WHERE match_score >= %s", (n,))
    return {
        "total": total,
        "above_50": above(50),
        "above_70": above(70),
        "above_90": above(90),
        "shortlisted": _scalar(conn, "SELECT COUNT(*) FROM jobs WHERE status = 'shortlisted'"),
    }


def job_filter_options(conn: psycopg.Connection) -> dict[str, list[str]]:
    return {
        "sources": [r["source"] for r in
                    _rows(conn, "SELECT DISTINCT source FROM jobs ORDER BY source")],
        "statuses": [r["status"] for r in
                     _rows(conn, "SELECT DISTINCT status FROM jobs ORDER BY status")],
        "countries": [r["country"] for r in
                      _rows(conn, "SELECT DISTINCT country FROM jobs WHERE country IS NOT NULL ORDER BY country")],
    }


def search_jobs(conn: psycopg.Connection, *, status: str = "", source: str = "",
                country: str = "", min_score: int | None = None, remote_only: bool = False,
                q: str = "", sort: str = "collected", direction: str = "desc",
                limit: int = 1000) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        if status == "unscored":
            where.append("match_score IS NULL AND status <> 'rejected'")
        elif status == "active":
            where.append("status <> 'rejected'")
        else:
            where.append("status = %s"); params.append(status)
    if source:
        where.append("source = %s"); params.append(source)
    if country:
        where.append("country = %s"); params.append(country)
    if min_score is not None:
        where.append("match_score IS NOT NULL AND match_score >= %s"); params.append(min_score)
    if remote_only:
        where.append("is_remote = TRUE")
    if q:
        where.append("(LOWER(company) LIKE %s OR LOWER(title) LIKE %s OR LOWER(COALESCE(location,'')) LIKE %s)")
        needle = f"%{q.lower()}%"; params += [needle, needle, needle]

    col = _SORTABLE.get(sort, _SORTABLE["collected"])
    dir_sql = "ASC" if direction.lower() == "asc" else "DESC"
    null_guard = f"({col} IS NULL), " if sort == "score" else ""
    sql = (f"SELECT id, source, company, title, location, country, category, is_remote, url, "
           f"status, reject_reason, match_score, summary, sponsorship_signal, company_domain, "
           f"collected_at, posted_at FROM jobs "
           f"{'WHERE ' + ' AND '.join(where) if where else ''} "
           f"ORDER BY {null_guard}{col} {dir_sql}, LOWER(company) ASC LIMIT %s")
    params.append(int(limit))
    out = _rows(conn, sql, params)
    for r in out:
        r["band"] = band_for(r["match_score"])
    return out


# ── Job detail ───────────────────────────────────────────────────────

def parse_verdict(raw: Any) -> dict[str, Any] | None:
    """jobs.match_json is already a dict (JSONB). Normalise list fields."""
    if not raw or not isinstance(raw, dict):
        return None

    def _list(key: str) -> list[str]:
        val = raw.get(key)
        if isinstance(val, str):
            return [val] if val.strip() else []
        if isinstance(val, list):
            return [str(v) for v in val if str(v).strip()]
        return []

    return {
        "score": raw.get("score"),
        "verdict": (raw.get("verdict") or "").strip() or None,
        "matched_strengths": _list("matched_strengths"),
        "gaps": _list("gaps"),
        "hard_blockers": _list("hard_blockers"),
        "tailoring_hints": _list("tailoring_hints"),
        "reasoning": (raw.get("reasoning") or "").strip() or None,
    }


def job_detail(conn: psycopg.Connection, job_id: str) -> dict[str, Any] | None:
    job = _one(conn, "SELECT * FROM jobs WHERE id = %s", (job_id,))
    if job is None:
        return None
    job["band"] = band_for(job.get("match_score"))
    resumes = _rows(conn,
        "SELECT * FROM tailored_resumes WHERE job_id = %s ORDER BY created_at DESC", (job_id,))
    applications = _rows(conn, """
        SELECT a.*, c.name AS contact_name, c.email AS contact_email
        FROM applications a LEFT JOIN contacts c ON c.id = a.contact_id
        WHERE a.job_id = %s ORDER BY a.created_at DESC""", (job_id,))
    contacts = _rows(conn,
        "SELECT * FROM contacts WHERE company = %s ORDER BY confidence DESC, name", (job["company"],))
    siblings = _rows(conn,
        "SELECT id, title, status, match_score FROM jobs WHERE company = %s AND id <> %s "
        "ORDER BY collected_at DESC LIMIT 8", (job["company"], job_id))
    for s in siblings:
        s["band"] = band_for(s["match_score"])
    return {"job": job, "verdict": parse_verdict(job.get("match_json")),
            "resumes": resumes, "applications": applications,
            "contacts": contacts, "siblings": siblings}


# ── Applications (Page 2) ────────────────────────────────────────────

def all_applications(conn: psycopg.Connection, *, status: str = "") -> list[dict[str, Any]]:
    where, params = [], []
    if status:
        where.append("a.status = %s"); params.append(status)
    rows = _rows(conn, f"""
        SELECT a.*, j.company, j.title, j.url AS job_url, j.match_score, j.country,
               c.name AS contact_name, c.email AS contact_email
        FROM applications a JOIN jobs j ON j.id = a.job_id
        LEFT JOIN contacts c ON c.id = a.contact_id
        {'WHERE ' + ' AND '.join(where) if where else ''}
        ORDER BY COALESCE(a.sent_at, a.created_at) DESC""", params)
    for r in rows:
        r["band"] = band_for(r["match_score"])
    return rows


def application_summary(conn: psycopg.Connection) -> dict[str, Any]:
    by_status = {r["status"]: r["n"] for r in
                 _rows(conn, "SELECT status, COUNT(*) AS n FROM applications GROUP BY status")}
    following_up = _scalar(conn,
        "SELECT COUNT(*) FROM applications WHERE status = 'emailed' AND followup_count > 0")
    return {"total": sum(by_status.values()),
            "by_status": {s: by_status.get(s, 0) for s in APPLICATION_STATUSES},
            "following_up": following_up}


# ── Inbox (replies) + bell ───────────────────────────────────────────

def inbox(conn: psycopg.Connection, limit: int = 100) -> list[dict[str, Any]]:
    return _rows(conn, """
        SELECT r.*, j.title AS job_title FROM replies r
        LEFT JOIN jobs j ON j.id = r.job_id
        ORDER BY r.received_at DESC LIMIT %s""", (limit,))


def bell(conn: psycopg.Connection) -> dict[str, Any]:
    return {
        "unread": _scalar(conn, "SELECT COUNT(*) FROM notifications WHERE is_read = FALSE"),
        "notes": _rows(conn,
            "SELECT id, kind, title, body, link, is_read, created_at FROM notifications "
            "ORDER BY created_at DESC LIMIT 12"),
    }


# ── Agent health ─────────────────────────────────────────────────────

def health(conn: psycopg.Connection) -> dict[str, Any]:
    runs = _rows(conn, "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT 25")
    last_by_kind = {}
    for r in runs:
        last_by_kind.setdefault(r["kind"], r)
    return {"runs": runs, "last_by_kind": last_by_kind,
            "errors_24h": _scalar(conn,
                "SELECT COUNT(*) FROM agent_runs WHERE status = 'error' "
                "AND started_at >= now() - interval '1 day'")}


# ── To-dos (Page 4) ──────────────────────────────────────────────────

def todos(conn: psycopg.Connection, include_done: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM todos"
    if not include_done:
        sql += " WHERE done = FALSE"
    return _rows(conn, sql + " ORDER BY done, created_at DESC")


# ── Resumes (tailored library) ───────────────────────────────────────

def resumes(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every tailored resume, newest first, with its job context and file state."""
    from pathlib import Path
    rows = _rows(conn, """
        SELECT r.id, r.pdf_path, r.json_path, r.ats_score, r.created_at,
               j.id AS job_id, j.company, j.title, j.match_score
        FROM tailored_resumes r JOIN jobs j ON j.id = r.job_id
        ORDER BY r.created_at DESC""")
    for r in rows:
        r["band"] = band_for(r["match_score"])
        r["has_file"] = bool(r["pdf_path"]) and Path(r["pdf_path"]).exists()
        r["filename"] = Path(r["pdf_path"]).name if r["pdf_path"] else None
    return rows


def resume_row(conn: psycopg.Connection, resume_id: int) -> dict[str, Any] | None:
    return _one(conn, """
        SELECT r.*, j.company, j.title FROM tailored_resumes r
        JOIN jobs j ON j.id = r.job_id WHERE r.id = %s""", (resume_id,))
