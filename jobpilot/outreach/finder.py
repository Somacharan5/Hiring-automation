"""Recruiter discovery — resolve a company to a domain, then to real people.

Design rule that everything else here serves: **an address is only ever marked
`verified` if a provider API asserted it, or we pattern-guessed it AND an SMTP
RCPT-TO probe accepted it.** Everything else is persisted at an honest (low)
confidence with verified=0 so the send path refuses it and a human reviews it.

Every provider adapter degrades to "no-op + warning" when its key is missing —
no key in .env is a normal state here, not an error.
"""

from __future__ import annotations

import os
import random
import re
import smtplib
import socket
import string
import subprocess
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from ..db import add_contact, contacts_for_company
from ..llm import _load_env

USER_AGENT = "JobPilot/1.0 (personal job-search assistant)"
HTTP_TIMEOUT = 12

# Title/department keywords that mark someone as worth emailing about a role.
RECRUITING_TERMS = (
    "recruit", "talent", "sourcer", "sourcing", "hiring", "staffing",
    "people ops", "people operations", "peopleops", "human resources",
    "hr ", "head of people", "chief people", "employer brand",
)
# Weaker signal — a hiring manager is a fine second choice.
MANAGER_TERMS = ("head of product", "vp product", "director of product", "product lead")

# Hosts that are job boards / ATS vendors, never the employer's own mail domain.
ATS_HOSTS = {
    "greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io",
    "lever.co", "jobs.lever.co", "ashbyhq.com", "jobs.ashbyhq.com",
    "workable.com", "recruitee.com", "bamboohr.com", "smartrecruiters.com",
    "myworkdayjobs.com", "workday.com", "icims.com", "taleo.net", "jazzhr.com",
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "remoteok.com", "remoteok.io", "remotive.com", "remotive.io", "weworkremotely.com",
    "wellfound.com", "angel.co", "adzuna.com", "google.com", "monster.com",
    "builtin.com", "dice.com", "otta.com", "simplify.jobs", "jobvite.com",
}

# Free-mail domains: never a company's recruiting domain.
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "aol.com", "icloud.com", "proton.me", "protonmail.com", "mail.com",
}

# Role addresses worth trying when we know the domain but no individual names.
GENERIC_LOCALPARTS = (
    ("careers", 45), ("jobs", 45), ("recruiting", 40), ("recruitment", 38),
    ("talent", 38), ("hr", 35), ("hiring", 30), ("people", 28),
)


@dataclass
class ContactCandidate:
    """One discovered (or guessed) way to reach a human at a company."""
    email: str | None = None
    name: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    source: str = "pattern"          # hunter | apollo | pattern | generic | manual
    confidence: int = 30             # 0-100, deliberately pessimistic
    verified: bool = False           # True ONLY if API-asserted or SMTP-accepted
    notes: list[str] = field(default_factory=list)

    def key(self) -> str:
        return (self.email or self.linkedin_url or self.name or "").strip().lower()


# ── helpers ──────────────────────────────────────────────────────────

def _warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def _env(key: str) -> str | None:
    _load_env()
    val = (os.environ.get(key) or "").strip()
    return val or None


def _is_recruiterish(*fields: str | None) -> bool:
    blob = " ".join(f.lower() for f in fields if f) + " "
    return any(t in blob for t in RECRUITING_TERMS)


def _is_hiring_managerish(*fields: str | None) -> bool:
    blob = " ".join(f.lower() for f in fields if f) + " "
    return any(t in blob for t in MANAGER_TERMS)


def _registrable(host: str) -> str:
    """Trim a hostname to something that looks like the registrable domain."""
    host = host.lower().strip().removeprefix("www.")
    parts = host.split(".")
    # Handle two-label public suffixes (co.uk, com.au, co.in …).
    if len(parts) > 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _slug(company: str) -> str:
    """'Acme Corp, Inc.' → 'acmecorp' — a decent last-resort domain stem."""
    cleaned = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|gmbh|plc|pvt|private|co)\b", " ",
                     company.lower())
    return re.sub(r"[^a-z0-9]", "", cleaned)


# ── domain resolution ────────────────────────────────────────────────

def find_domain(company: str, job_url: str | None = None) -> str | None:
    """Resolve a company name to its email domain.

    Clearbit's autocomplete endpoint is free and keyless; the job URL host is
    the fallback (skipped when it is an ATS/aggregator, which it usually is).
    """
    company = (company or "").strip()
    if not company:
        return None

    # 1. Clearbit autocomplete (no key required).
    try:
        resp = requests.get(
            "https://autocomplete.clearbit.com/v1/companies/suggest",
            params={"query": company}, timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        if resp.ok:
            suggestions = resp.json() or []
            target = company.lower()
            # Prefer an exact-ish name match over Clearbit's first guess.
            for s in suggestions:
                if (s.get("name") or "").lower() == target and s.get("domain"):
                    return _registrable(s["domain"])
            for s in suggestions:
                dom = s.get("domain")
                if dom and _slug(company) and _slug(company) in _registrable(dom).replace(".", ""):
                    return _registrable(dom)
            if suggestions and suggestions[0].get("domain"):
                return _registrable(suggestions[0]["domain"])
    except (requests.RequestException, ValueError) as e:
        _warn(f"Clearbit lookup failed for {company!r}: {type(e).__name__}")

    # 2. Fall back to the job posting's host, unless it is a job board.
    if job_url:
        host = urlparse(job_url).netloc
        if host:
            dom = _registrable(host)
            if dom not in ATS_HOSTS and dom not in FREEMAIL and host.lower() not in ATS_HOSTS:
                return dom

    return None


# ── provider adapters ────────────────────────────────────────────────

def hunter_domain_search(domain: str, api_key: str | None = None) -> list[ContactCandidate]:
    """Hunter.io domain-search. Free tier is ~25 searches/month — use sparingly."""
    api_key = api_key or _env("HUNTER_API_KEY")
    if not api_key:
        _warn("hunter: HUNTER_API_KEY not set — skipping (add it to .env to enable)")
        return []
    if not domain:
        return []

    try:
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": api_key, "limit": 25},
            timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as e:
        _warn(f"hunter: request failed ({type(e).__name__}) — skipping")
        return []
    if not resp.ok:
        _warn(f"hunter: HTTP {resp.status_code} — {resp.text[:160]}")
        return []

    out: list[ContactCandidate] = []
    for e in (resp.json().get("data", {}) or {}).get("emails", []) or []:
        email = (e.get("value") or "").strip().lower()
        if not email:
            continue
        name = " ".join(p for p in (e.get("first_name"), e.get("last_name")) if p) or None
        title = e.get("position")
        dept = e.get("department")
        hunter_conf = int(e.get("confidence") or 0)          # Hunter's own 0-100
        recruiting = _is_recruiterish(title, dept)
        managerish = _is_hiring_managerish(title)
        if not (recruiting or managerish):
            continue                                          # don't email random engineers
        # Hunter asserts deliverability; trust it but keep our own ceiling.
        conf = min(95, hunter_conf if hunter_conf else 70)
        if recruiting:
            conf = min(97, conf + 5)
        out.append(ContactCandidate(
            email=email, name=name, title=title or dept,
            linkedin_url=e.get("linkedin"), source="hunter",
            confidence=conf,
            verified=(e.get("verification", {}) or {}).get("status") == "valid" or hunter_conf >= 90,
            notes=[f"hunter confidence={hunter_conf}", f"department={dept}"],
        ))
    out.sort(key=lambda c: c.confidence, reverse=True)
    return out


def apollo_people_search(company: str, api_key: str | None = None) -> list[ContactCandidate]:
    """Apollo people-search filtered to recruiting titles at one company."""
    api_key = api_key or _env("APOLLO_API_KEY")
    if not api_key:
        _warn("apollo: APOLLO_API_KEY not set — skipping (add it to .env to enable)")
        return []
    if not company:
        return []

    payload = {
        "q_organization_name": company,
        "person_titles": [
            "recruiter", "technical recruiter", "talent acquisition",
            "talent acquisition partner", "head of talent", "head of people",
            "people operations", "hr manager", "hiring manager",
        ],
        "page": 1, "per_page": 25,
    }
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/mixed_people/search",
            json=payload, timeout=HTTP_TIMEOUT,
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache",
                     "x-api-key": api_key, "User-Agent": USER_AGENT},
        )
    except requests.RequestException as e:
        _warn(f"apollo: request failed ({type(e).__name__}) — skipping")
        return []
    if not resp.ok:
        _warn(f"apollo: HTTP {resp.status_code} — {resp.text[:160]}")
        return []

    out: list[ContactCandidate] = []
    for p in (resp.json() or {}).get("people", []) or []:
        email = (p.get("email") or "").strip().lower()
        # Apollo returns this literal string when the address is paywalled.
        locked = (not email) or email.startswith("email_not_unlocked")
        name = p.get("name") or " ".join(
            x for x in (p.get("first_name"), p.get("last_name")) if x) or None
        title = p.get("title")
        if not _is_recruiterish(title) and not _is_hiring_managerish(title):
            continue
        out.append(ContactCandidate(
            email=None if locked else email,
            name=name, title=title, linkedin_url=p.get("linkedin_url"),
            source="apollo",
            confidence=40 if locked else 85,
            verified=(not locked) and p.get("email_status") == "verified",
            notes=["email locked behind Apollo credits — name usable for pattern guessing"]
            if locked else [f"email_status={p.get('email_status')}"],
        ))
    out.sort(key=lambda c: c.confidence, reverse=True)
    return out


def pattern_guess(domain: str, first: str, last: str) -> list[ContactCandidate]:
    """Standard corporate local-part permutations, weighted by real-world frequency.

    These are GUESSES: every candidate comes back verified=False. They only
    become sendable after `verify_smtp` returns True.
    """
    if not domain or not first:
        return []
    f = re.sub(r"[^a-z]", "", first.lower())
    l = re.sub(r"[^a-z]", "", (last or "").lower())
    if not f:
        return []

    patterns: list[tuple[str, int]] = []
    if l:
        patterns += [
            (f"{f}.{l}", 70), (f"{f[0]}{l}", 60), (f"{f}{l}", 50),
            (f"{f}_{l}", 40), (f"{f}{l[0]}", 35), (f"{f[0]}.{l}", 35),
            (f"{l}.{f}", 25), (f"{l}{f[0]}", 20),
        ]
    patterns += [(f, 45 if l else 30)]

    seen: set[str] = set()
    out: list[ContactCandidate] = []
    full_name = " ".join(p.title() for p in (first, last) if p) or None
    for local, weight in patterns:
        email = f"{local}@{domain}".lower()
        if email in seen:
            continue
        seen.add(email)
        out.append(ContactCandidate(
            email=email, name=full_name, source="pattern",
            confidence=weight, verified=False,
            notes=["unverified pattern guess"],
        ))
    return out


def generic_guess(domain: str) -> list[ContactCandidate]:
    """Role addresses (careers@, jobs@ …) — the honest fallback when no names."""
    if not domain:
        return []
    return [
        ContactCandidate(email=f"{local}@{domain}", name=None, title="Recruiting (role address)",
                         source="generic", confidence=conf, verified=False,
                         notes=["role address guess"])
        for local, conf in GENERIC_LOCALPARTS
    ]


# ── SMTP verification ────────────────────────────────────────────────

def _mx_hosts(domain: str) -> list[str]:
    """MX records via dnspython if present, else `dig`, else `nslookup`."""
    try:  # dnspython is optional and currently not installed
        import dns.resolver  # type: ignore

        resolver = dns.resolver.Resolver()
        resolver.lifetime = resolver.timeout = 5
        answers = resolver.resolve(domain, "MX")
        return [str(r.exchange).rstrip(".") for r in
                sorted(answers, key=lambda r: r.preference)]
    except ImportError:
        pass
    except Exception:
        return []

    for cmd in (["dig", "+short", "+time=3", "+tries=1", "MX", domain],
                ["nslookup", "-type=MX", domain]):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            continue
        hosts: list[tuple[int, str]] = []
        for line in res.stdout.splitlines():
            m = re.search(r"(?:preference\s*=\s*|^\s*)(\d+)\s*(?:,\s*mail exchanger\s*=\s*|\s+)([A-Za-z0-9.\-]+\.)",
                          line.strip())
            if m:
                hosts.append((int(m.group(1)), m.group(2).rstrip(".")))
        if hosts:
            return [h for _, h in sorted(hosts)]
    return []


def _probe_sender(settings: dict | None = None) -> str:
    cfg = (settings or {}).get("outreach", {}) if settings else {}
    return (cfg.get("from_email") or "").strip() or "no-reply@example.com"


def verify_smtp(email: str, settings: dict | None = None, timeout: float = 8.0) -> bool | None:
    """Best-effort RCPT-TO probe. Never sends: we quit before DATA.

    Returns True (accepted), False (rejected), or None (inconclusive — no MX,
    port 25 blocked, greylisting, or a catch-all domain that accepts anything).
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1]
    hosts = _mx_hosts(domain)
    if not hosts:
        return None

    random_local = "".join(random.choices(string.ascii_lowercase, k=16))
    sender = _probe_sender(settings)

    for host in hosts[:2]:
        server = None
        try:
            server = smtplib.SMTP(timeout=timeout)
            server.connect(host, 25)
            server.ehlo_or_helo_if_needed()
            server.mail(sender)
            code, _ = server.rcpt(email)
            if code not in (250, 251):
                # 550/553 → no such mailbox. 4xx → greylisted, inconclusive.
                return False if 500 <= code < 600 else None
            # Accepted — but is this a catch-all that accepts literally anything?
            catch_code, _ = server.rcpt(f"{random_local}@{domain}")
            if catch_code in (250, 251):
                return None                      # catch-all: proves nothing
            return True
        except (smtplib.SMTPException, socket.error, OSError):
            continue                             # try the next MX
        finally:
            if server is not None:
                try:
                    server.quit()                # QUIT — DATA is never issued
                except Exception:
                    pass
    return None


# ── availability reporting ───────────────────────────────────────────

def finder_availability(settings: dict | None = None) -> dict[str, bool]:
    """Which configured finders can actually run right now (key present?)."""
    cfg = (settings or {}).get("outreach", {}) if settings else {}
    configured = cfg.get("finders") or ["hunter", "apollo", "pattern"]
    needs_key = {"hunter": "HUNTER_API_KEY", "apollo": "APOLLO_API_KEY",
                 "snov": "SNOV_CLIENT_ID"}
    return {name: (True if name not in needs_key else bool(_env(needs_key[name])))
            for name in configured}


# ── orchestration ────────────────────────────────────────────────────

def discover_contacts(conn, company: str, job_url: str | None = None,
                      settings: dict | None = None, verify: bool = True) -> list:
    """Find (or recall) recruiter contacts for a company and persist them.

    Returns the company's contact rows, best confidence first. Contacts already
    in the DB short-circuit the whole thing — API quota is scarce.
    """
    cfg = (settings or {}).get("outreach", {}) if settings else {}
    finders = cfg.get("finders") or ["hunter", "apollo", "pattern"]

    existing = contacts_for_company(conn, company)
    if any(r["email"] for r in existing):
        print(f"  ↺ {company}: {len(existing)} contact(s) already known — skipping discovery")
        return existing

    domain = find_domain(company, job_url)
    if domain:
        print(f"  · {company}: domain → {domain}")
    else:
        _warn(f"{company}: could not resolve a domain — no email discovery possible")

    candidates: list[ContactCandidate] = []
    named: list[ContactCandidate] = []

    if "hunter" in finders and domain:
        found = hunter_domain_search(domain)
        candidates += found
        named += [c for c in found if c.name]
    if "apollo" in finders:
        found = apollo_people_search(company)
        candidates += [c for c in found if c.email]
        named += [c for c in found if c.name]

    if "pattern" in finders and domain:
        # Names we know but have no address for → permute them.
        for person in named:
            if person.email:
                continue
            parts = (person.name or "").split()
            if parts:
                guesses = pattern_guess(domain, parts[0], parts[-1] if len(parts) > 1 else "")
                for g in guesses:
                    g.title = person.title
                    g.linkedin_url = person.linkedin_url
                candidates += guesses
        if not any(c.email for c in candidates):
            candidates += generic_guess(domain)   # last resort: careers@ etc.

    # De-duplicate, keeping the highest-confidence version of each address.
    best: dict[str, ContactCandidate] = {}
    for c in candidates:
        k = c.key()
        if k and (k not in best or c.confidence > best[k].confidence):
            best[k] = c
    ranked = sorted(best.values(), key=lambda c: c.confidence, reverse=True)

    # SMTP-probe only the unverified guesses that could plausibly be used.
    if verify:
        for c in ranked[:6]:
            if c.verified or not c.email:
                continue
            result = verify_smtp(c.email, settings)
            if result is True:
                c.verified = True
                c.confidence = min(90, c.confidence + 20)
                c.notes.append("SMTP RCPT-TO accepted")
            elif result is False:
                c.confidence = max(1, c.confidence - 25)
                c.notes.append("SMTP RCPT-TO rejected — mailbox does not exist")
            else:
                c.notes.append("SMTP inconclusive (catch-all/blocked) — needs manual review")

    for c in ranked:
        if not (c.email or c.linkedin_url):
            continue
        add_contact(conn, company=company, email=c.email, name=c.name, title=c.title,
                    linkedin_url=c.linkedin_url, source=c.source,
                    confidence=c.confidence, verified=c.verified)

    rows = contacts_for_company(conn, company)
    verified_n = sum(1 for r in rows if r["verified"])
    print(f"  · {company}: {len(rows)} contact(s) stored, {verified_n} verified "
          f"({'sendable' if verified_n else 'manual review required'})")
    return rows
