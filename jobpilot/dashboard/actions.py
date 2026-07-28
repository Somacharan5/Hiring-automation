"""The writes the dashboard is allowed to perform (Neon / Postgres).

The dashboard is a cockpit, not an autopilot. It can re-file a job, manage the
manual to-do list, and clear notifications. It has NO send path — the outreach
run owns sending, with its own guards.
"""

from __future__ import annotations

import psycopg

from .. import db
from .queries import MANUAL_STATUSES


class ActionError(Exception):
    """A refused write. Carries an HTTP status so the route can pass it on."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def set_job_status(conn: psycopg.Connection, job_id: str, status: str,
                   reason: str | None = None) -> dict:
    """Manually re-file a job. `reason` is only kept for 'rejected'."""
    status = (status or "").strip().lower()
    if status not in MANUAL_STATUSES:
        raise ActionError(f"Unknown status '{status}'. Allowed: {', '.join(MANUAL_STATUSES)}.", 422)

    row = db.get_job(conn, job_id)
    if row is None:
        raise ActionError(f"No job {job_id}", 404)

    previous = row["status"]
    reject_reason = ((reason or "").strip() or row.get("reject_reason") or "manual override") \
        if status == "rejected" else None
    db.set_status(conn, job_id, status, reject_reason)
    return {"id": job_id, "status": status, "previous": previous,
            "reject_reason": reject_reason, "changed": previous != status,
            "message": f"{row['company']} · {row['title']} moved {previous} → {status}."}


# ── To-dos ───────────────────────────────────────────────────────────

def add_todo(conn: psycopg.Connection, *, company: str = "", title: str = "",
             portal_url: str = "", note: str = "", job_id: str | None = None) -> dict:
    title = (title or "").strip()
    portal_url = (portal_url or "").strip()
    if not title and not company and not portal_url:
        raise ActionError("A to-do needs at least a title, company, or portal URL.", 422)
    todo_id = db.add_todo(conn, company=company.strip() or None, title=title or None,
                          portal_url=portal_url or None, note=(note or "").strip() or None,
                          job_id=job_id)
    return {"id": todo_id, "message": "To-do added."}


def toggle_todo(conn: psycopg.Connection, todo_id: int, done: bool = True) -> dict:
    db.set_todo_done(conn, todo_id, done)
    return {"id": todo_id, "done": done, "message": "Updated."}


# ── Notifications (bell) ─────────────────────────────────────────────

def mark_notifications_read(conn: psycopg.Connection) -> dict:
    n = db.mark_all_notifications_read(conn)
    return {"cleared": n, "message": f"Cleared {n} notification(s)."}


def mark_reply_read(conn: psycopg.Connection, reply_id: int) -> dict:
    db.mark_reply_read(conn, reply_id)
    return {"id": reply_id, "message": "Marked read."}
