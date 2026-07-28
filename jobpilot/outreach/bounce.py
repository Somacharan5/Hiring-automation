"""Bounce harvesting — the accuracy flywheel no paid API can sell you.

Hunter can tell you an address looked valid last Tuesday. Only your own mailbox
can tell you that the message you actually sent to it came back. This module
reads the Gmail account we send from, finds the delivery-status notifications,
and feeds the failures back into discovery so the same mistake is never repeated:

  * the address itself is marked dead (confidence 0, verified 0) and blacklisted
  * the *pattern* it came from is demoted for that domain, so if `first.last@acme`
    bounced we stop guessing `first.last` at acme.com and try something else

Hard (5.x.x) vs soft (4.x.x) matters: a hard bounce means the mailbox does not
exist and we must never retry; a soft bounce is a full mailbox or a temporary
server problem and says nothing about validity.

Skips cleanly when GMAIL_APP_PASSWORD is unset.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

from ..llm import _load_env
from . import store

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Senders and subjects that mean "this is a delivery failure report".
BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail-daemon", "maildelivery")
BOUNCE_SUBJECTS = (
    "undelivered mail returned to sender", "delivery status notification",
    "returned mail", "delivery failure", "undeliverable", "mail delivery failed",
    "failure notice", "message not delivered", "delivery incomplete",
)

# `Final-Recipient: rfc822; someone@acme.com` in a message/delivery-status part.
FINAL_RECIPIENT_RE = re.compile(
    r"^(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*<?([^\s<>]+@[^\s<>]+?)>?\s*$",
    re.IGNORECASE | re.MULTILINE)
STATUS_RE = re.compile(r"^Status:\s*([245]\.\d{1,3}\.\d{1,3})", re.IGNORECASE | re.MULTILINE)
DIAGNOSTIC_RE = re.compile(r"^Diagnostic-Code:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
ACTION_RE = re.compile(r"^Action:\s*(\w+)", re.IGNORECASE | re.MULTILINE)
# Fallback for providers that describe the failure in prose only.
PROSE_RE = re.compile(
    r"(?:to|address)\s+<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?"
    r"[^.]{0,80}?(?:does not exist|not found|unknown|couldn't be found|rejected)",
    re.IGNORECASE)
SMTP_CODE_RE = re.compile(r"\b([45]\d{2})[\s-]|\b([245]\.\d{1,3}\.\d{1,3})\b")


@dataclass
class Bounce:
    email: str
    smtp_code: str | None = None
    kind: str = "unknown"          # hard | soft | unknown
    subject: str = ""
    detail: str = ""
    seen_at: str = ""

    @property
    def is_hard(self) -> bool:
        return self.kind == "hard"


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return str(value)


PLACEHOLDER_MARKERS = ("paste", "here", "your", "xxxx", "changeme", "todo", "app_password")


def is_placeholder_password(value: str) -> bool:
    """True when .env still holds a template value rather than a real secret.

    A real Gmail App Password is 16 characters with no separators once spaces
    are stripped, so anything shorter is definitionally not one.
    """
    v = (value or "").strip()
    if not v:
        return True
    low = v.lower()
    if any(m in low for m in PLACEHOLDER_MARKERS):
        return True
    return len(v) < 16


def classify_code(code: str | None) -> str:
    """5.x.x → hard (mailbox does not exist). 4.x.x → soft (temporary)."""
    if not code:
        return "unknown"
    c = code.strip()
    if c.startswith("5"):
        return "hard"
    if c.startswith("4"):
        return "soft"
    return "unknown"


def looks_like_bounce(from_hdr: str, subject: str) -> bool:
    f, s = (from_hdr or "").lower(), (subject or "").lower()
    return (any(b in f for b in BOUNCE_SENDERS)
            or any(b in s for b in BOUNCE_SUBJECTS))


def parse_bounce_message(raw: bytes, sent_from: str | None = None) -> Bounce | None:
    """Pull the failed recipient and status out of one DSN. None if it isn't one."""
    try:
        msg = email_lib.message_from_bytes(raw)
    except Exception:  # noqa: BLE001
        return None

    subject = _decode(msg.get("Subject"))
    from_hdr = _decode(msg.get("From"))
    if not looks_like_bounce(from_hdr, subject):
        return None

    recipient = code = diagnostic = None
    action = None

    # Preferred path: the structured message/delivery-status part (RFC 3464).
    for part in msg.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        try:
            body = part.as_string()
        except Exception:  # noqa: BLE001
            continue
        if m := FINAL_RECIPIENT_RE.search(body):
            recipient = m.group(1).strip().lower()
        if m := STATUS_RE.search(body):
            code = m.group(1)
        if m := DIAGNOSTIC_RE.search(body):
            diagnostic = m.group(1).strip()
        if m := ACTION_RE.search(body):
            action = m.group(1).lower()
        if recipient:
            break

    # Fallback: scan the human-readable text.
    if not recipient:
        text = ""
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    text += part.get_payload(decode=True).decode(errors="replace")
                except Exception:  # noqa: BLE001
                    continue
        if m := FINAL_RECIPIENT_RE.search(text):
            recipient = m.group(1).strip().lower()
        elif m := PROSE_RE.search(text):
            recipient = m.group(1).strip().lower()
        if not code:
            if m := STATUS_RE.search(text):
                code = m.group(1)
            elif m := SMTP_CODE_RE.search(text):
                code = m.group(1) or m.group(2)
        if not diagnostic:
            diagnostic = text[:300].replace("\n", " ").strip()

    if not recipient or "@" not in recipient:
        return None
    # Never treat our own address as a bounced recipient.
    if sent_from and recipient == sent_from.lower():
        return None

    kind = classify_code(code)
    if kind == "unknown" and action == "failed":
        kind = "hard"

    return Bounce(email=recipient, smtp_code=code, kind=kind, subject=subject,
                  detail=(diagnostic or "")[:400],
                  seen_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def scan_bounces(mailbox: str | None = None, app_password: str | None = None,
                 since_days: int = 30, folder: str = '"[Gmail]/All Mail"',
                 limit: int = 200) -> list[Bounce]:
    """IMAP into Gmail and return the delivery failures from the last N days.

    Read-only: the mailbox is opened with `readonly=True`, so nothing is marked,
    moved, or deleted. Returns [] (with a warning) when unconfigured.
    """
    _load_env()
    mailbox = mailbox or os.environ.get("OUTREACH_MAILBOX") or "iamsomacharan@gmail.com"
    app_password = app_password or os.environ.get("GMAIL_APP_PASSWORD")
    if not app_password:
        print("  ⚠ bounce: GMAIL_APP_PASSWORD not set — skipping bounce scan")
        return []
    app_password = app_password.replace(" ", "")
    # The shipped .env carries a literal placeholder; attempting a login with it
    # just earns a confusing AUTHENTICATIONFAILED from Gmail.
    if is_placeholder_password(app_password):
        print("  ⚠ bounce: GMAIL_APP_PASSWORD is still the placeholder value — "
              "skipping. Generate a 16-char App Password at "
              "https://myaccount.google.com/apppasswords and put it in .env")
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
    bounces: list[Bounce] = []
    imap = None
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(mailbox, app_password)
        status, _ = imap.select(folder, readonly=True)
        if status != "OK":
            status, _ = imap.select("INBOX", readonly=True)
            if status != "OK":
                print("  ⚠ bounce: could not open a mail folder — skipping")
                return []

        uids: set[bytes] = set()
        queries = [f'(SINCE {since} FROM "mailer-daemon")',
                   f'(SINCE {since} FROM "postmaster")',
                   f'(SINCE {since} SUBJECT "Undelivered Mail Returned")',
                   f'(SINCE {since} SUBJECT "Delivery Status Notification")',
                   f'(SINCE {since} SUBJECT "Address not found")']
        for q in queries:
            try:
                typ, data = imap.search(None, q)
            except imaplib.IMAP4.error:
                continue
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())

        for uid in list(uids)[:limit]:
            try:
                typ, data = imap.fetch(uid, "(RFC822)")
            except imaplib.IMAP4.error:
                continue
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            b = parse_bounce_message(data[0][1], sent_from=mailbox)
            if b:
                bounces.append(b)
    except imaplib.IMAP4.error as e:
        print(f"  ⚠ bounce: IMAP login/search failed ({e}). If you have 2FA, the "
              f"value in GMAIL_APP_PASSWORD must be a 16-char App Password, and "
              f"IMAP must be enabled in Gmail settings.")
        return []
    except OSError as e:
        print(f"  ⚠ bounce: network error ({type(e).__name__}) — skipping")
        return []
    finally:
        if imap is not None:
            try:
                imap.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass

    # De-duplicate: one address may bounce many times.
    seen: dict[str, Bounce] = {}
    for b in bounces:
        if b.email not in seen or (b.is_hard and not seen[b.email].is_hard):
            seen[b.email] = b
    out = list(seen.values())
    print(f"  · bounce: {len(out)} distinct bounced address(es) in the last {since_days}d "
          f"({sum(1 for b in out if b.is_hard)} hard)")
    return out


def infer_pattern_name(local: str, name: str | None) -> str | None:
    """Which naming template produced this local part, if we know the person's name."""
    from .discovery import PATTERN_TEMPLATES

    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in (name or "").split()]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    for tmpl, fn in PATTERN_TEMPLATES.items():
        try:
            if fn(first, last) == local.lower():
                return tmpl
        except IndexError:
            continue
    return None


def apply_bounce_feedback(conn, bounces: list[Bounce]) -> dict:
    """Mark bounced contacts dead and demote the pattern that produced them.

    Only HARD bounces poison a pattern — a soft bounce (full mailbox, greylist)
    is not evidence that the address is wrong.
    """
    store.ensure_tables(conn)
    stats = {"contacts_killed": 0, "patterns_demoted": 0, "soft": 0, "logged": 0}

    for b in bounces:
        store.log_bounce(conn, b.email, b.smtp_code, b.kind, b.detail or b.subject, b.seen_at)
        stats["logged"] += 1
        if not b.is_hard:
            stats["soft"] += 1
            continue

        row = conn.execute(
            "SELECT id, name, company FROM contacts WHERE lower(email) = ?",
            (b.email.lower(),)).fetchone()

        cur = conn.execute(
            "UPDATE contacts SET confidence = 0, verified = 0 WHERE lower(email) = ?",
            (b.email.lower(),))
        conn.commit()
        stats["contacts_killed"] += cur.rowcount

        local, _, domain = b.email.lower().partition("@")
        if not domain:
            continue
        # Kill the exact local part for this domain…
        store.mark_pattern_dead(conn, domain, local, f"hard bounce {b.smtp_code or ''}".strip())
        # …and, if we can tell which convention generated it, kill that too.
        tmpl = infer_pattern_name(local, row["name"] if row else None)
        if tmpl:
            store.mark_pattern_dead(conn, domain, f"__pattern__{tmpl}",
                                    f"hard bounce of {b.email}")
            stats["patterns_demoted"] += 1

    print(f"  · bounce feedback: {stats['contacts_killed']} contact(s) marked dead, "
          f"{stats['patterns_demoted']} pattern(s) demoted, {stats['soft']} soft bounce(s) ignored")
    return stats


def run_bounce_loop(conn, since_days: int = 30, mailbox: str | None = None,
                    app_password: str | None = None) -> dict:
    """scan + apply. The one call a scheduler or CLI should make."""
    bounces = scan_bounces(mailbox=mailbox, app_password=app_password, since_days=since_days)
    if not bounces:
        return {"contacts_killed": 0, "patterns_demoted": 0, "soft": 0, "logged": 0}
    return apply_bounce_feedback(conn, bounces)
