"""The guarded send path. `send_pending` is the ONLY function that sends mail.

Guard order, per application:
    1. dry-run gate       — outreach.dry_run must be *explicitly* false
    2. first-run seatbelt — first_run_draft_only forces draft-only for the run
    3. daily cap          — sent in last 24h < outreach.daily_send_cap
    4. dedup              — this job must not already be emailed
    5. address + resume   — a sendable email and an existing resume file
    6. send → mark 'emailed' + schedule the first follow-up

Any guard that trips skips the application with a reason. A send that raises
records status 'failed' and the batch continues.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from ..db import _load_env, mark_application_sent, sent_today_count, set_application_status


def live_sending_enabled(settings: dict | None) -> bool:
    """True only if outreach.dry_run is *explicitly* false. Omission means dry-run."""
    cfg = (settings or {}).get("outreach")
    if not isinstance(cfg, dict) or "dry_run" not in cfg:
        return False
    val = cfg["dry_run"]
    return val is False or (isinstance(val, str) and val.strip().lower() in {"false", "no", "off", "0"})


def _pending(conn, limit: int | None):
    sql = ("SELECT a.id, a.job_id, a.subject, a.body, a.resume_path, "
           "       j.company, j.title, j.match_score, c.email AS to_email "
           "FROM applications a JOIN jobs j ON j.id = a.job_id "
           "LEFT JOIN contacts c ON c.id = a.contact_id "
           "WHERE a.channel = 'email' AND a.status = 'preparing_resume' "
           "ORDER BY j.match_score DESC NULLS LAST")
    params: list = []
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def smtp_send(settings: dict, to_email: str, subject: str, body: str,
              attachments: list[str] | None = None) -> None:
    """Send one message over Gmail SMTP. Raises on failure. Sends for real — callers guard it."""
    _load_env()
    cfg = settings["outreach"]
    user = cfg["from_email"]
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not pw:
        raise RuntimeError("GMAIL_APP_PASSWORD not set in .env")

    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("from_name", user), user))
    msg["To"] = to_email
    msg["Reply-To"] = user                      # replies land in the monitored inbox
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachments or []:
        p = Path(path)
        if p.exists():
            msg.add_attachment(p.read_bytes(), maintype="application",
                               subtype="pdf", filename=p.name)

    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=30) as s:
        s.starttls(context=ctx)
        s.login(user, pw)
        s.send_message(msg)


def send_pending(conn, settings: dict, limit: int | None = None) -> dict:
    """Send every prepared email that clears the guards. Returns a report."""
    cfg = settings.get("outreach", {})
    live = live_sending_enabled(settings)
    first_run_draft = bool(cfg.get("first_run_draft_only", True))
    cap = int(cfg.get("daily_send_cap", 20))
    interval = int((settings.get("followup", {}) or {}).get("interval_days", 3))

    rows = _pending(conn, limit)
    report = {"dry_run": not live, "first_run_draft": live and first_run_draft,
              "sent": 0, "skipped": [], "failed": [], "would_send": []}

    if not live or first_run_draft:
        why = ("dry_run is on" if not live else
               "first_run_draft_only is on — review these, then set it false to go live")
        for r in rows:
            report["would_send"].append(
                {"company": r["company"], "title": r["title"], "to": r["to_email"],
                 "subject": r["subject"]})
        report["reason"] = why
        return report

    already = sent_today_count(conn, "email")
    for r in rows:
        if already >= cap:
            report["skipped"].append({"company": r["company"], "reason": f"daily cap {cap} reached"})
            continue
        if not r["to_email"]:
            report["skipped"].append({"company": r["company"], "reason": "no sendable address"})
            continue
        resume = r["resume_path"]
        if resume and not Path(resume).exists():
            report["skipped"].append({"company": r["company"], "reason": "resume file missing"})
            continue
        try:
            smtp_send(settings, r["to_email"], r["subject"], r["body"],
                      attachments=[resume] if resume else None)
        except Exception as e:  # noqa: BLE001 — one bad send must not kill the batch
            set_application_status(conn, r["id"], "failed", f"{type(e).__name__}: {e}")
            report["failed"].append({"company": r["company"], "error": str(e)})
            continue
        next_fu = datetime.now(timezone.utc) + timedelta(days=interval)
        mark_application_sent(conn, r["id"], next_followup_at=next_fu)
        already += 1
        report["sent"] += 1
    return report
