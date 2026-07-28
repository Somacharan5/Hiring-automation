"""Email verification stack, cheapest check first.

    syntax → disposable/role → MX → **catch-all probe** → RCPT-TO probe

The one idea this module exists to enforce
------------------------------------------
An SMTP `RCPT TO` that returns 250 does **not** mean the mailbox exists. Most
Google Workspace tenants accept every recipient and bounce it internally later.
Measured on real domains from this project's own job list:

    stripe.com      RCPT zq7x2mk9nonexist… → 250   ← catch-all, proves nothing
    databricks.com  RCPT zq7x2mk9nonexist… → 250   ← catch-all, proves nothing
    atlan.com       RCPT zq7x2mk9nonexist… → 250   ← catch-all, proves nothing
    figma.com       RCPT zq7x2mk9nonexist… → 550   ← strict, probes are meaningful
    sarvam.ai       RCPT zq7x2mk9nonexist… → 550   ← strict, probes are meaningful

So: **before trusting any positive RCPT result we probe a random local part on
the same domain.** If the random address is accepted, the domain is catch-all,
SMTP tells us nothing there, and every address on it is capped at review-only
confidence no matter how it was discovered. The result is cached per domain
(`store.domain_intel`) because each probe is a real connection to someone
else's mail server and we are a polite client.

Nothing here ever issues `DATA`. The probe connects, says hello, declares a
sender, asks about a recipient, and quits. No mail is transmitted.
"""

from __future__ import annotations

import random
import re
import smtplib
import socket
import string
import subprocess
import threading
import time
from dataclasses import dataclass, field

# ── status vocabulary ────────────────────────────────────────────────

VERIFIED = "verified"    # SMTP proved the mailbox exists on a strict domain
LIKELY = "likely"        # strong non-SMTP evidence, SMTP unavailable/inconclusive
CATCH_ALL = "catch_all"  # domain accepts everything — unprovable, human review
INVALID = "invalid"      # syntactically bad, no MX, disposable, or RCPT rejected
UNKNOWN = "unknown"      # we could not find out (port 25 blocked, greylisted…)

ALL_STATUSES = (VERIFIED, LIKELY, CATCH_ALL, INVALID, UNKNOWN)

# Deliverability confidence produced by this module alone (0-100). The
# discovery layer combines these with the *source* confidence.
CONF = {
    VERIFIED: 92,
    LIKELY: 60,
    CATCH_ALL: 50,
    UNKNOWN: 45,
    INVALID: 0,
}

# ── tunables ─────────────────────────────────────────────────────────

SMTP_TIMEOUT = 12.0
SMTP_PORT = 25
MAX_MX_TRIED = 2
# Be a polite client: never hammer the same MX host back-to-back.
MIN_SECONDS_BETWEEN_CONNECTS = 1.5
# HELO name and MAIL FROM used for probes. Deliberately not the user's real
# Gmail address by default — probing shouldn't attach their sending identity to
# other people's spam heuristics.
DEFAULT_HELO = "jobpilot.local"
DEFAULT_PROBE_SENDER = "verify-probe@example.com"

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)

# Local parts that are real mailboxes but never worth emailing a human at.
UNDELIVERABLE_ROLES = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "notifications",
    "postmaster", "mailer-daemon", "mailerdaemon", "abuse", "bounce", "bounces",
    "unsubscribe", "automated", "alerts", "notification",
}
# Role addresses that ARE legitimate recruiting targets — lower value than a
# named person, but genuinely deliverable and genuinely read.
RECRUITING_ROLES = {
    "careers", "career", "jobs", "job", "recruiting", "recruitment", "recruiter",
    "hiring", "talent", "hr", "people", "work", "workwithus", "joinus", "apply",
}
# Deliverable but generic — a last resort.
GENERIC_ROLES = {
    "info", "hello", "contact", "admin", "support", "help", "sales", "team",
    "office", "enquiries", "inquiries", "mail", "general",
}
# Real, monitored mailboxes that are simply the WRONG AUDIENCE for a job
# application. These verify perfectly and will not bounce — which is exactly why
# they need calling out separately. Deliverability is not appropriateness, and
# mailing a resume to press@ or security@ is a reputational own-goal, not a
# technical failure.
MISDIRECTED_ROLES = {
    "press", "media", "pr", "mediarelations", "legal", "security", "privacy",
    "abuse-report", "billing", "invoices", "accounts", "investors", "ir",
    "partnerships", "partner", "bd", "marketing", "compliance", "vendor",
    "procurement", "dpo", "gdpr",
}

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
    "getnada.com", "sharklasers.com", "dispostable.com", "maildrop.cc",
    "fakeinbox.com", "mailnesia.com", "tempinbox.com", "spamgourmet.com",
    "mohmal.com", "emailondeck.com", "burnermail.io", "moakt.com",
}

# ── module caches (process-lifetime; the DB cache is the durable one) ─

_mx_cache: dict[str, list[str]] = {}
_catch_all_cache: dict[str, bool | None] = {}
_last_connect: dict[str, float] = {}
_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"    {msg}")


def _pace(host: str) -> None:
    with _lock:
        last = _last_connect.get(host, 0.0)
        wait = MIN_SECONDS_BETWEEN_CONNECTS - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _last_connect[host] = time.monotonic()


def random_local_part(n: int = 18) -> str:
    """A local part no real person could plausibly own."""
    return "zq7x2mk9" + "".join(random.choices(string.ascii_lowercase + string.digits, k=n - 8))


# ── 1. syntax ────────────────────────────────────────────────────────

def check_syntax(email: str) -> bool:
    """RFC-ish. Deliberately stricter than the RFC — we want mailable, not legal."""
    email = (email or "").strip()
    if not email or len(email) > 254 or email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    if not local or len(local) > 64 or not domain:
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    if ".." in domain or domain.startswith("-") or domain.endswith("-"):
        return False
    return bool(EMAIL_RE.match(email))


def split_email(email: str) -> tuple[str, str]:
    local, _, domain = (email or "").strip().lower().partition("@")
    return local, domain


# ── 2. cheap classification ──────────────────────────────────────────

def is_disposable(domain: str) -> bool:
    return (domain or "").strip().lower() in DISPOSABLE_DOMAINS


def is_role_address(local: str) -> bool:
    """True for any non-personal mailbox (role, generic, misdirected, undeliverable)."""
    return role_kind(local) is not None


def role_kind(local: str) -> str | None:
    """'recruiting' | 'generic' | 'misdirected' | 'undeliverable' | None (personal)."""
    low = (local or "").strip().lower()
    base = re.split(r"[+.\-]", low)[0]
    for names, kind in ((UNDELIVERABLE_ROLES, "undeliverable"),
                        (RECRUITING_ROLES, "recruiting"),
                        (MISDIRECTED_ROLES, "misdirected"),
                        (GENERIC_ROLES, "generic")):
        if low in names or base in names:
            return kind
    return None


# ── 3. MX ────────────────────────────────────────────────────────────

def check_mx(domain: str, conn=None) -> list[str]:
    """MX hosts in preference order. `[]` means the domain cannot receive mail.

    dnspython when available (it is, in this venv), else `dig`, else `nslookup`.
    Cached in-process and — when a DB connection is supplied — in `domain_intel`.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return []
    if domain in _mx_cache:
        return _mx_cache[domain]

    if conn is not None:
        from . import store
        intel = store.get_domain_intel(conn, domain)
        if intel and intel.get("mx_hosts"):
            _mx_cache[domain] = intel["mx_hosts"]
            return intel["mx_hosts"]

    hosts = _resolve_mx(domain)
    _mx_cache[domain] = hosts
    if conn is not None and hosts:
        from . import store
        store.save_domain_intel(conn, domain, mx_hosts=hosts)
    return hosts


def _resolve_mx(domain: str) -> list[str]:
    try:
        import dns.resolver  # type: ignore

        resolver = dns.resolver.Resolver()
        resolver.timeout = 5
        resolver.lifetime = 8
        answers = resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
        return [h for h in hosts if h and h != "."]
    except ImportError:
        pass  # fall through to the CLI tools
    except Exception:  # NXDOMAIN, no answer, timeout — all mean "no usable MX"
        return []

    for cmd in (["dig", "+short", "+time=3", "+tries=1", "MX", domain],
                ["nslookup", "-type=MX", domain]):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        found: list[tuple[int, str]] = []
        for line in res.stdout.splitlines():
            m = re.search(
                r"(?:preference\s*=\s*|^\s*)(\d+)\s*(?:,\s*mail exchanger\s*=\s*|\s+)"
                r"([A-Za-z0-9.\-]+)", line.strip())
            if m:
                found.append((int(m.group(1)), m.group(2).rstrip(".")))
        if found:
            return [h for _, h in sorted(found)]
    return []


# ── 4. the SMTP conversation ─────────────────────────────────────────

@dataclass
class RcptResult:
    """Raw outcome of one RCPT TO."""
    code: int | None
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.code in (250, 251)

    @property
    def rejected(self) -> bool:
        # 5xx = permanent refusal. 552 (mailbox full) means the box EXISTS.
        return self.code is not None and 500 <= self.code < 600 and self.code != 552

    @property
    def inconclusive(self) -> bool:
        return self.code is None or (400 <= self.code < 500) or self.code == 552


def _rcpt_batch(domain: str, addresses: list[str], sender: str = DEFAULT_PROBE_SENDER,
                helo: str = DEFAULT_HELO, timeout: float = SMTP_TIMEOUT,
                mx_hosts: list[str] | None = None) -> dict[str, RcptResult]:
    """Ask one MX about several recipients on a single connection.

    Batching matters: the catch-all probe and the real probe should ideally hit
    the same server in the same session, and one connection is far politer than
    three. Returns {address: RcptResult}; missing keys mean the conversation died.
    """
    hosts = mx_hosts if mx_hosts is not None else check_mx(domain)
    if not hosts:
        return {}

    out: dict[str, RcptResult] = {}
    for host in hosts[:MAX_MX_TRIED]:
        server = None
        try:
            _pace(host)
            server = smtplib.SMTP(timeout=timeout)
            server.connect(host, SMTP_PORT)
            server.ehlo(helo)
            if server.esmtp_features == {} and not server.does_esmtp:
                server.helo(helo)
            code, _ = server.mail(sender)
            if code >= 400:
                continue  # this MX won't talk to us; try the next one
            for addr in addresses:
                try:
                    c, m = server.rcpt(addr)
                    out[addr] = RcptResult(c, m.decode(errors="replace") if isinstance(m, bytes) else str(m))
                except smtplib.SMTPServerDisconnected:
                    break  # server hung up mid-batch; keep what we have
                except smtplib.SMTPException as e:
                    out[addr] = RcptResult(None, f"{type(e).__name__}: {e}")
            if out:
                return out
        except (smtplib.SMTPException, socket.error, OSError):
            continue  # blocked port, refused connection, TLS weirdness → next MX
        finally:
            if server is not None:
                try:
                    server.quit()  # QUIT. DATA is never issued anywhere in this module.
                except Exception:  # noqa: BLE001
                    pass
    return out


# ── 5. catch-all detection — THE critical piece ──────────────────────

def detect_catch_all(domain: str, conn=None, force: bool = False,
                     sender: str = DEFAULT_PROBE_SENDER,
                     ttl_days: int = 30) -> bool | None:
    """Does this domain accept mail for *any* local part?

    Returns True (catch-all — SMTP verification is worthless here), False (strict
    — a 250 on a real address is meaningful), or None (couldn't tell: no MX,
    port 25 blocked, greylisted).

    Two independent random local parts must BOTH be accepted before we call a
    domain catch-all, so one greylist/tarpit fluke can't mislabel a strict
    domain. Cached in `domain_intel` for `ttl_days`.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return None

    if not force:
        if domain in _catch_all_cache:
            return _catch_all_cache[domain]
        if conn is not None:
            from . import store
            intel = store.get_domain_intel(conn, domain)
            if store.catch_all_is_fresh(intel, ttl_days):
                _catch_all_cache[domain] = intel["catch_all"]
                return intel["catch_all"]

    hosts = check_mx(domain, conn=conn)
    if not hosts:
        _catch_all_cache[domain] = None
        return None

    probes = [f"{random_local_part()}@{domain}", f"{random_local_part()}@{domain}"]
    results = _rcpt_batch(domain, probes, sender=sender, mx_hosts=hosts)
    got = [results[p] for p in probes if p in results]

    verdict: bool | None
    if not got:
        verdict, note = None, "no SMTP conversation possible (port 25 blocked or MX refused)"
    elif all(r.accepted for r in got):
        verdict, note = True, f"accepted {len(got)} random local parts → catch-all"
    elif any(r.rejected for r in got):
        codes = ",".join(str(r.code) for r in got)
        verdict, note = False, f"rejected random local parts ({codes}) → strict recipient validation"
    else:
        codes = ",".join(str(r.code) for r in got)
        verdict, note = None, f"inconclusive random-probe result ({codes}) — greylisting?"

    _catch_all_cache[domain] = verdict
    if conn is not None:
        from . import store
        store.save_domain_intel(conn, domain, mx_hosts=hosts, catch_all=verdict, probe_note=note)
    return verdict


# ── 6. single-address probe ──────────────────────────────────────────

def smtp_probe(email: str, sender: str = DEFAULT_PROBE_SENDER,
               timeout: float = SMTP_TIMEOUT, mx_hosts: list[str] | None = None) -> bool | None:
    """RCPT-TO probe for one address. True / False / None (inconclusive).

    NOTE: a True here is only meaningful on a **non-catch-all** domain. Call
    `detect_catch_all` first — `verify_email` does this for you. Never sends.
    """
    email = (email or "").strip().lower()
    if not check_syntax(email):
        return False
    _, domain = split_email(email)
    results = _rcpt_batch(domain, [email], sender=sender, timeout=timeout, mx_hosts=mx_hosts)
    r = results.get(email)
    if r is None:
        return None
    if r.accepted:
        return True
    if r.rejected:
        return False
    return None  # 4xx greylisting, 552 full mailbox, or a protocol error


# ── 7. orchestration ─────────────────────────────────────────────────

@dataclass
class VerificationResult:
    email: str
    status: str = UNKNOWN
    confidence: int = 0
    reason: str = ""
    domain: str = ""
    mx_hosts: list[str] = field(default_factory=list)
    catch_all: bool | None = None
    role: str | None = None          # recruiting | generic | undeliverable | None
    smtp_code: int | None = None
    checks: list[str] = field(default_factory=list)

    @property
    def is_verified(self) -> bool:
        return self.status == VERIFIED

    @property
    def sendable_without_review(self) -> bool:
        """Only a genuinely proven mailbox clears this bar."""
        return self.status == VERIFIED

    def to_dict(self) -> dict:
        return {
            "email": self.email, "status": self.status, "confidence": self.confidence,
            "reason": self.reason, "domain": self.domain, "catch_all": self.catch_all,
            "role": self.role, "smtp_code": self.smtp_code, "checks": list(self.checks),
        }


def verify_email(email: str, conn=None, sender: str = DEFAULT_PROBE_SENDER,
                 use_smtp: bool = True, timeout: float = SMTP_TIMEOUT) -> VerificationResult:
    """Run the full stack and return an honest verdict.

    Order is strictly cheapest-first, and each stage can end the pipeline:
      1. syntax          → INVALID
      2. disposable      → INVALID
      3. undeliverable role (noreply@ …) → INVALID
      4. MX lookup       → INVALID if the domain can't receive mail at all
      5. catch-all probe → CATCH_ALL (capped, review-only) if the domain accepts anything
      6. RCPT probe      → VERIFIED / INVALID / UNKNOWN
    """
    email = (email or "").strip().lower()
    res = VerificationResult(email=email)

    if not check_syntax(email):
        res.status, res.confidence = INVALID, 0
        res.reason = "not a syntactically valid email address"
        return res
    res.checks.append("syntax ok")

    local, domain = split_email(email)
    res.domain = domain

    if is_disposable(domain):
        res.status, res.confidence = INVALID, 0
        res.reason = f"{domain} is a disposable-mail provider"
        return res
    res.checks.append("not disposable")

    res.role = role_kind(local)
    if res.role == "undeliverable":
        res.status, res.confidence = INVALID, 0
        res.reason = f"{local}@ is an unattended/no-reply mailbox — nobody reads it"
        return res

    res.mx_hosts = check_mx(domain, conn=conn)
    if not res.mx_hosts:
        res.status, res.confidence = INVALID, 0
        res.reason = f"{domain} publishes no MX records — it cannot receive mail"
        return res
    res.checks.append(f"MX ok ({res.mx_hosts[0]})")

    if not use_smtp:
        res.status = LIKELY
        res.confidence = CONF[LIKELY]
        res.reason = "syntax + MX ok; SMTP probing disabled for this run"
        return res

    # --- the decisive step -------------------------------------------------
    res.catch_all = detect_catch_all(domain, conn=conn, sender=sender)

    if res.catch_all is True:
        res.status = CATCH_ALL
        res.confidence = CONF[CATCH_ALL]
        res.reason = (f"{domain} is a catch-all: it accepts mail for randomly generated "
                      f"local parts, so an SMTP probe cannot prove this mailbox exists. "
                      f"Route to human review — do not auto-send.")
        res.checks.append("catch-all detected")
        return res

    if res.catch_all is None:
        res.status = UNKNOWN
        res.confidence = CONF[UNKNOWN]
        res.reason = (f"could not establish whether {domain} is catch-all (no SMTP "
                      f"conversation — port 25 blocked, or the MX refused us), so no "
                      f"SMTP evidence is trustworthy here")
        res.checks.append("catch-all undetermined")
        return res

    res.checks.append("domain validates recipients (not catch-all)")

    # Strict domain → a RCPT result is real evidence.
    probe = _rcpt_batch(domain, [email], sender=sender, timeout=timeout, mx_hosts=res.mx_hosts)
    r = probe.get(email)
    if r is None:
        res.status = UNKNOWN
        res.confidence = CONF[UNKNOWN]
        res.reason = f"{domain} validates recipients but the probe connection failed"
        return res

    res.smtp_code = r.code
    if r.accepted:
        res.status = VERIFIED
        res.confidence = CONF[VERIFIED]
        res.reason = (f"{domain} rejects unknown recipients but accepted this one "
                      f"(SMTP {r.code}) — the mailbox exists")
        if res.role == "recruiting":
            res.confidence = 88  # real mailbox, but a shared inbox, not a person
        elif res.role in ("misdirected", "generic"):
            # Deliverable, and the wrong place to send a job application.
            res.confidence = 60
            res.reason += (f" — but {local}@ is a {res.role} inbox, not a hiring "
                           f"contact, so it is NOT auto-sendable")
        res.checks.append(f"RCPT accepted ({r.code})")
    elif r.rejected:
        res.status = INVALID
        res.confidence = 0
        res.reason = f"mailbox does not exist — {domain} rejected it with SMTP {r.code}"
        res.checks.append(f"RCPT rejected ({r.code})")
    else:
        res.status = UNKNOWN
        res.confidence = CONF[UNKNOWN]
        res.reason = f"inconclusive SMTP response {r.code} (greylisting or full mailbox)"
    return res


def verify_many(emails: list[str], conn=None, sender: str = DEFAULT_PROBE_SENDER,
                use_smtp: bool = True) -> list[VerificationResult]:
    """Verify a batch. The per-domain catch-all cache makes this cheap after the first."""
    return [verify_email(e, conn=conn, sender=sender, use_smtp=use_smtp) for e in emails]


def clear_caches() -> None:
    """Drop the in-process caches (tests / forced re-probes)."""
    _mx_cache.clear()
    _catch_all_cache.clear()
    _last_connect.clear()
