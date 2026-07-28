"""The guarded send path. `send_pending` is the ONLY function that can send mail.

Guard order, evaluated per application, in this order and no other:
    1. dry-run gate      — settings.outreach.dry_run must be explicitly false
    2. approval gate     — status must be 'approved' (when require_approval)
    3. daily cap         — db.sent_today_count(conn, 'email') < daily_send_cap
    4. duplicate check   — this (job, channel) must not already be 'sent'
    5. address quality   — contact exists, has an email, and is verified
    6. attachments       — every attachment must exist on disk
    7. send → mark_application_sent

Any guard that trips skips that application with a reason. A send that raises
records status 'failed' with the error and the batch continues.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from ..db import (applications_with_status, mark_application_sent, sent_today_count,
                  upsert_application)
from ..llm import _load_env
from .composer import master_resume, render_draft

ROOT = Path(__file__).resolve().parent.parent.parent

SENDABLE_STATUSES = ("approved",)          # when require_approval is on
LOOSE_STATUSES = ("approved", "drafted")   # only when require_approval is off


class SendBlocked(RuntimeError):
    """Raised if the SMTP transport is reached while dry-run is in force."""


# ── gate 1: the dry-run resolver ─────────────────────────────────────

def live_sending_enabled(settings: dict | None) -> bool:
    """True only if outreach.dry_run is *explicitly* false.

    Missing config, a missing section, None, or anything unrecognised all mean
    dry-run. There is deliberately no way to enable sending by omission.
    """
    if not settings:
        return False
    cfg = settings.get("outreach")
    if not isinstance(cfg, dict) or "dry_run" not in cfg:
        return False
    val = cfg["dry_run"]
    if val is False:
        return True
    if isinstance(val, str) and val.strip().lower() in {"false", "no", "off", "0"}:
        return True
    return False


# ── helpers ──────────────────────────────────────────────────────────

def _contact(conn, contact_id: int | None):
    if not contact_id:
        return None
    return conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()


def _application(conn, application_id: int):
    return conn.execute(
        "SELECT a.*, j.company, j.title, j.url, j.match_score FROM applications a "
        "JOIN jobs j ON j.id = a.job_id WHERE a.id = ?", (application_id,)
    ).fetchone()


def _attachments(app_row, settings: dict) -> tuple[list[Path], list[str]]:
    """Resolve (paths, problems). A missing file is a problem, not a silent skip."""
    cfg = settings.get("outreach", {})
    paths: list[Path] = []
    problems: list[str] = []

    resume = app_row["resume_path"]
    resume_path = Path(resume) if resume else None
    if resume_path and not resume_path.is_absolute():
        resume_path = ROOT / resume_path
    if not resume_path or not resume_path.exists():
        fallback = master_resume()
        if fallback:
            resume_path = fallback
        else:
            problems.append("no resume PDF found (neither tailored nor master CV)")
            resume_path = None
    if resume_path:
        paths.append(resume_path)

    if cfg.get("attach_portfolio"):
        p = Path(cfg.get("portfolio_path") or "")
        if p and not p.is_absolute():
            p = ROOT / p
        if p and p.exists():
            paths.append(p)
        else:
            problems.append(f"portfolio attachment missing: {cfg.get('portfolio_path')!r}")
    return paths, problems


def _build_message(settings: dict, to_email: str, to_name: str | None,
                   subject: str, body: str, attachments: list[Path]) -> EmailMessage:
    cfg = settings.get("outreach", {})
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("from_name") or "", cfg.get("from_email") or ""))
    msg["To"] = formataddr((to_name or "", to_email))
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachments:
        msg.add_attachment(path.read_bytes(), maintype="application",
                           subtype="pdf", filename=path.name)
    return msg


def _smtp_send(settings: dict, msg: EmailMessage, *, live: bool) -> None:
    """Transport. Refuses to run unless the caller proved the dry-run gate passed."""
    if not live:
        raise SendBlocked("_smtp_send reached with live=False — dry-run gate bypassed")

    _load_env()
    import os

    cfg = settings.get("outreach", {})
    user = (cfg.get("from_email") or "").strip()
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").strip()
    if not user:
        raise RuntimeError("outreach.from_email is empty in config/settings.yaml")
    if not password:
        raise RuntimeError("GMAIL_APP_PASSWORD not set in .env (use a Gmail App Password)")

    host = cfg.get("smtp_host") or "smtp.gmail.com"
    port = int(cfg.get("smtp_port") or 587)
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(user, password)
        server.send_message(msg)


# ── approval helpers ─────────────────────────────────────────────────

def approve(conn, application_id: int) -> bool:
    """Move one drafted application to 'approved'. Returns False if not eligible."""
    row = _application(conn, application_id)
    if row is None:
        print(f"  ✗ no application with id {application_id}")
        return False
    if row["status"] != "drafted":
        print(f"  ✗ application {application_id} is '{row['status']}', not 'drafted'")
        return False
    conn.execute("UPDATE applications SET status = 'approved' WHERE id = ?", (application_id,))
    conn.commit()
    print(f"  ✔ approved #{application_id}: {row['company']} — {row['title'].strip()}")
    return True


def approve_all(conn, min_score: int = 0) -> int:
    """Approve every drafted email whose job scored >= min_score. Returns the count."""
    rows = conn.execute(
        "SELECT a.id, j.company, j.title, j.match_score FROM applications a "
        "JOIN jobs j ON j.id = a.job_id "
        "WHERE a.channel = 'email' AND a.status = 'drafted' "
        "  AND COALESCE(j.match_score, 0) >= ?", (min_score,),
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE applications SET status = 'approved' WHERE id = ?", (r["id"],))
    conn.commit()
    for r in rows:
        print(f"  ✔ approved #{r['id']}: {r['company']} — {r['title'].strip()} [{r['match_score']}]")
    return len(rows)


def pending_summary(conn) -> dict[str, int]:
    """Counts of email applications by status — for CLI status output."""
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM applications WHERE channel = 'email' GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# ── the one send path ────────────────────────────────────────────────

def send_pending(conn, settings: dict | None = None, limit: int | None = None) -> dict:
    """Send approved outreach emails, subject to every guard rail.

    Returns a report dict:
        {dry_run, eligible, sent, would_send, skipped[], failed[], cap, cap_remaining}
    In dry-run it prints each draft and sends nothing.
    """
    settings = settings or {}
    cfg = settings.get("outreach", {}) if isinstance(settings.get("outreach"), dict) else {}

    live = live_sending_enabled(settings)                      # ── GATE 1
    require_approval = cfg.get("require_approval", True) is not False
    cap = int(cfg.get("daily_send_cap", 20) or 0)

    statuses = SENDABLE_STATUSES if require_approval else LOOSE_STATUSES  # ── GATE 2
    queue: list = []
    for status in statuses:
        queue += [r for r in applications_with_status(conn, status, channel="email")]
    if not require_approval:
        print("  ⚠ require_approval is off — drafted emails are eligible to send")

    report: dict = {
        "dry_run": not live, "eligible": len(queue), "sent": 0, "would_send": 0,
        "skipped": [], "failed": [], "cap": cap,
        "cap_remaining": max(0, cap - sent_today_count(conn, "email")),
    }
    if not queue:
        print("Nothing to send: no email applications in status "
              f"{'/'.join(statuses)}. Draft some first, then approve them.")
        return report

    if not live:
        print(f"\n{'═' * 68}\n DRY RUN — nothing will be sent "
              f"(set outreach.dry_run: false to go live)\n{'═' * 68}")

    processed = 0
    for row in queue:
        if limit is not None and processed >= limit:
            report["skipped"].append({"id": row["id"], "reason": f"batch limit {limit} reached"})
            continue
        processed += 1
        label = f"{row['company']} — {row['title'].strip()}"

        # ── GATE 3: daily cap, re-checked from the DB before every single send.
        # cap <= 0 means "send nothing today" — never "unlimited".
        used = sent_today_count(conn, "email")
        if used >= cap:
            report["skipped"].append({"id": row["id"], "company": row["company"],
                                      "reason": f"daily cap reached ({used}/{cap})"})
            print(f"  ⛔ {label}: daily cap reached ({used}/{cap}) — stopping")
            break
        report["cap_remaining"] = max(0, cap - used)

        # ── GATE 4: duplicate — never a second email for the same (job, channel)
        fresh = _application(conn, row["id"])
        if fresh is None or fresh["status"] == "sent":
            report["skipped"].append({"id": row["id"], "company": row["company"],
                                      "reason": "already sent (duplicate blocked)"})
            print(f"  ⛔ {label}: already sent — skipping")
            continue

        # ── GATE 5: a real, verified address
        contact = _contact(conn, fresh["contact_id"])
        if contact is None or not contact["email"]:
            report["skipped"].append({"id": row["id"], "company": row["company"],
                                      "reason": "no email address on the linked contact"})
            print(f"  ⛔ {label}: no contact email — left as a draft for manual review")
            continue
        if not contact["verified"]:
            report["skipped"].append({
                "id": row["id"], "company": row["company"],
                "reason": f"address {contact['email']} is an unverified guess "
                          f"(confidence {contact['confidence']}) — manual review required"})
            print(f"  ⛔ {label}: {contact['email']} unverified — manual review required")
            continue

        # ── GATE 6: attachments must actually exist
        attachments, problems = _attachments(fresh, settings)
        if problems:
            report["skipped"].append({"id": row["id"], "company": row["company"],
                                      "reason": "; ".join(problems)})
            print(f"  ⛔ {label}: {'; '.join(problems)}")
            continue

        if not live:
            report["would_send"] += 1
            print(f"\n▸ [#{fresh['id']}] {label}  (status={fresh['status']})")
            print(render_draft(fresh["subject"] or "(no subject)", fresh["body"] or "",
                               to=contact["email"], attachments=[str(p) for p in attachments]))
            continue

        # ── GATE 7: send, then record
        try:
            msg = _build_message(settings, contact["email"], contact["name"],
                                 fresh["subject"] or "", fresh["body"] or "", attachments)
            _smtp_send(settings, msg, live=live)
        except Exception as e:  # noqa: BLE001 — one bad send never kills the batch
            err = f"{type(e).__name__}: {e}"
            upsert_application(conn, job_id=fresh["job_id"], channel="email", status="failed",
                               contact_id=fresh["contact_id"], resume_path=fresh["resume_path"],
                               subject=fresh["subject"], body=fresh["body"], error=err)
            report["failed"].append({"id": fresh["id"], "company": row["company"], "error": err})
            print(f"  ✗ {label}: {err}")
            continue

        mark_application_sent(conn, fresh["id"])
        report["sent"] += 1
        report["cap_remaining"] = max(0, cap - sent_today_count(conn, "email"))
        print(f"  ✔ sent to {contact['email']} — {label}")

    verb = "would send" if not live else "sent"
    print(f"\n{verb}: {report['would_send'] if not live else report['sent']} · "
          f"skipped: {len(report['skipped'])} · failed: {len(report['failed'])} · "
          f"cap remaining today: {report['cap_remaining']}/{cap}")
    return report
