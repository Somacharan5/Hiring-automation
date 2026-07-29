"""Follow-up engine — bump each emailed application every N days, up to the cap.

Reads applications whose next_followup_at is due and are still under
followup.max_followups, sends a short one-line bump in the original thread
("Re: <subject>"), and reschedules. Same dry-run / cap guards as the send path.
Replies stop follow-ups: the reply reader clears next_followup_at.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..db import followups_due, record_followup, sent_today_count
from .composer import _signature_lines
from .send import live_sending_enabled, smtp_send

DEFAULT_TEMPLATE = "Bumping this mail up, I am really looking forward to work with team of {company}."


def _bump_body(settings: dict, company: str) -> str:
    cfg = settings.get("outreach", {})
    tmpl = (settings.get("followup", {}) or {}).get("template") or DEFAULT_TEMPLATE
    line = tmpl.format(company=company)
    name = cfg.get("from_name", "")
    sig = "\n".join(_signature_lines(cfg))
    return f"{line}\n\nBest,\n{name}\n{sig}"


def run_followups(conn, settings: dict, limit: int | None = None) -> dict:
    """Send due follow-ups (or list them in dry-run). Returns a report."""
    cfg = settings.get("outreach", {})
    fu_cfg = settings.get("followup", {}) or {}
    max_fu = int(fu_cfg.get("max_followups", 5))
    interval = int(fu_cfg.get("interval_days", 3))
    cap = int(cfg.get("daily_send_cap", 20))
    live = live_sending_enabled(settings) and not bool(cfg.get("first_run_draft_only", True))

    due = followups_due(conn, max_followups=max_fu, limit=limit)
    report = {"dry_run": not live, "sent": 0, "skipped": [], "failed": [], "would_send": []}

    if not live:
        report["would_send"] = [{"company": d["company"], "title": d["title"],
                                 "to": d["contact_email"], "n": (d["followup_count"] or 0) + 1}
                                for d in due]
        return report

    already = sent_today_count(conn, "email")
    for d in due:
        if already >= cap:
            report["skipped"].append({"company": d["company"], "reason": f"daily cap {cap} reached"})
            continue
        if not d["contact_email"]:
            report["skipped"].append({"company": d["company"], "reason": "no address"})
            continue
        subject = d["subject"] or f"Product role at {d['company']}"
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        try:
            smtp_send(settings, d["contact_email"], subject, _bump_body(settings, d["company"]))
        except Exception as e:  # noqa: BLE001
            report["failed"].append({"company": d["company"], "error": str(e)})
            continue
        next_fu = datetime.now(timezone.utc) + timedelta(days=interval)
        record_followup(conn, d["id"], next_followup_at=next_fu)
        already += 1
        report["sent"] += 1
    return report
