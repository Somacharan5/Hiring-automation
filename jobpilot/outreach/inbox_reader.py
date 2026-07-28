"""Inbound reply reader — IMAP → replies + notifications (the bell).

Scans the sending inbox for mail from domains we've emailed, matches each to its
application, classifies sentiment (interested / rejected / other), stores it
deduped on Message-ID, rings the bell, and stops follow-ups for that thread.
Read-only against the mailbox; it never deletes or moves anything.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from ..db import _load_env, add_notification, add_reply, match_application_for_reply

_REJECT = re.compile(
    r"unfortunately|not moving forward|will not be moving|decided not to|won'?t be proceeding"
    r"|not a fit|position has been filled|role has been filled|regret to inform"
    r"|other candidates|not to proceed|no longer (?:considering|available)", re.I)
_INTERESTED = re.compile(
    r"interview|schedule a call|set up a call|phone screen|happy to (?:chat|connect)"
    r"|would love to|next steps|move forward|available for|book a time|assessment|assignment"
    r"|calendar|when are you free|hop on a call", re.I)


def classify(subject: str, body: str) -> str:
    text = f"{subject}\n{body}"
    if _REJECT.search(text):
        return "rejected"
    if _INTERESTED.search(text):
        return "interested"
    return "other"


def _plain_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                    part.get("Content-Disposition") or ""):
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:  # noqa: BLE001
        return str(msg.get_payload())


def _contacted_domains(conn) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT LOWER(c.email) AS email FROM contacts c "
        "JOIN applications a ON a.contact_id = c.id "
        "WHERE a.sent_at IS NOT NULL AND c.email IS NOT NULL").fetchall()
    return {r["email"].split("@", 1)[1] for r in rows if r["email"] and "@" in r["email"]}


def _clear_followups(conn, application_id: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE applications SET status = %s, next_followup_at = NULL WHERE id = %s",
                    (status, application_id))
    conn.commit()


def scan_replies(conn, settings: dict, since_days: int | None = None,
                 max_scan: int = 400) -> dict:
    """Fetch recent inbound mail, match to applications, store replies + bell. Returns a report."""
    _load_env()
    inbox = settings.get("inbox", {}) or {}
    user = settings["outreach"]["from_email"]
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not pw:
        raise RuntimeError("GMAIL_APP_PASSWORD not set in .env")
    since_days = since_days or int(inbox.get("scan_days", 14))

    domains = _contacted_domains(conn)
    report = {"scanned": 0, "matched": 0, "new_replies": 0, "domains_watched": len(domains)}
    if not domains:
        return report                          # nothing sent yet → no replies to match

    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL(inbox.get("imap_host", "imap.gmail.com"), int(inbox.get("imap_port", 993)))
    try:
        M.login(user, pw)
        M.select(inbox.get("mailbox", "INBOX"), readonly=True)
        typ, data = M.search(None, "SINCE", since)
        ids = data[0].split() if data and data[0] else []
        ids = ids[-max_scan:]                   # most recent window only
        report["scanned"] = len(ids)

        for num in ids:
            typ, hd = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")
            if typ != "OK" or not hd or not hd[0]:
                continue
            hdr = email.message_from_bytes(hd[0][1])
            from_email = parseaddr(hdr.get("From", ""))[1].lower()
            if "@" not in from_email or from_email.split("@", 1)[1] not in domains:
                continue
            report["matched"] += 1
            app = match_application_for_reply(conn, from_email)
            if not app:
                continue
            msg_id = (hdr.get("Message-ID") or f"{from_email}:{num.decode()}").strip()
            subject = str(hdr.get("Subject") or "")
            typ, full = M.fetch(num, "(RFC822)")
            body = _plain_body(email.message_from_bytes(full[0][1])) if full and full[0] else ""
            sentiment = classify(subject, body)

            reply_id = add_reply(conn, message_id=msg_id, from_email=from_email, subject=subject,
                                 body=body[:8000], company=app["company"],
                                 application_id=app["id"], job_id=app["job_id"], sentiment=sentiment)
            if reply_id is None:
                continue                        # already recorded
            report["new_replies"] += 1
            _clear_followups(conn, app["id"], "denied" if sentiment == "rejected" else "replied")
            add_notification(conn, "reply", f"{app['company']} replied ({sentiment})",
                             body=subject[:140], link="/inbox")
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass
    return report
