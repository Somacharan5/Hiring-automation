"""Generic company address discovery — careers@/jobs@/hr@ on an MX-valid domain.

The first-pass, no-API strategy: derive the company's email domain, confirm it has
MX records, and register the conventional recruiting inboxes as 'generic' contacts.
Specific-recruiter discovery (Hunter / site-crawl) is a later second pass. Nothing
here sends or SMTP-probes a mailbox (unreliable + rude from a datacenter IP).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from ..db import add_contact, contacts_for_company

ROOT = Path(__file__).resolve().parent.parent.parent
_DOMAIN_MAP_PATH = ROOT / "config" / "company_domains.yaml"


@lru_cache(maxsize=1)
def _domain_map() -> dict[str, str]:
    """Curated company → email-domain overrides (checked before any guessing)."""
    try:
        import yaml
        data = yaml.safe_load(_DOMAIN_MAP_PATH.read_text()) or {}
        return {str(k).lower().strip(): str(v).strip() for k, v in data.items()}
    except Exception:  # noqa: BLE001
        return {}

# Hosts that are the ATS/aggregator, never the employer's own mail domain.
_ATS_HOSTS = (
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee",
    "myworkday", "workday", "indeed", "linkedin", "adzuna", "remotive", "remoteok",
    "google", "glassdoor", "ziprecruiter", "naukri", "bamboohr", "jobvite", "icims",
    "gohire", "breezy", "teamtailor", "wellfound", "angel.co",
)

# prefix → confidence. careers@ clears the 70 auto-send floor; others are alternates.
_GENERIC = [("careers", 75), ("jobs", 68), ("hr", 62), ("talent", 60),
            ("recruiting", 60), ("people", 58)]


def candidate_domains(company: str, job_url: str | None = None) -> list[str]:
    """Ordered guesses for the company's email domain: job-URL host, then slug+TLDs."""
    out: list[str] = []
    if job_url:
        try:
            host = urlparse(job_url).netloc.lower().removeprefix("www.")
        except ValueError:
            host = ""
        if host and not any(a in host for a in _ATS_HOSTS):
            parts = host.split(".")
            out.append(".".join(parts[-2:]) if len(parts) > 2 else host)
    slug = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    if slug:
        out += [f"{slug}.{tld}" for tld in ("com", "ai", "io", "co", "in", "xyz", "org", "net")]
    seen, res = set(), []
    for d in out:
        if d and d not in seen:
            seen.add(d)
            res.append(d)
    return res


@lru_cache(maxsize=512)
def has_mx(domain: str) -> bool:
    """True if the domain publishes MX records (i.e. can receive mail)."""
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=6.0)
        return len(answers) > 0
    except Exception:  # noqa: BLE001 — NXDOMAIN / NoAnswer / timeout all mean "no"
        return False


def resolve_domain(company: str, job_url: str | None = None) -> str | None:
    """Curated override → first MX-valid candidate (URL host, then slug+TLDs). None if nothing.

    The override map is what makes discovery hit sarvam.ai instead of guessing sarvam.com.
    """
    override = _domain_map().get((company or "").lower().strip())
    if override:
        return override
    for d in candidate_domains(company, job_url):
        if has_mx(d):
            return d
    return None


def discover_generic(conn, company: str, job_url: str | None = None,
                     settings: dict | None = None) -> list[dict]:
    """Register careers@/jobs@/hr@ for the company's MX-valid domain. Returns contacts."""
    existing = [c for c in contacts_for_company(conn, company) if c.get("email")]
    if existing:
        return existing                      # already discovered; don't re-hammer DNS

    domain = resolve_domain(company, job_url)
    if not domain:
        return []

    with conn.cursor() as cur:      # record the domain on the company's jobs (schema + dashboard)
        cur.execute("UPDATE jobs SET company_domain = %s WHERE company = %s AND company_domain IS NULL",
                    (domain, company))
    conn.commit()

    prefixes = ((settings or {}).get("outreach", {}) or {}).get("generic_inbox_prefixes")
    generic = ([(p, 70 if p == "careers" else 62) for p in prefixes] if prefixes else _GENERIC)
    for local, conf in generic:
        add_contact(conn, company, email=f"{local}@{domain}", source="pattern",
                    tier="generic", confidence=conf, verified=False)
    return contacts_for_company(conn, company)


def best_sendable_contact(conn, company: str, min_confidence: int = 70) -> dict | None:
    """Highest-confidence contact with an email at/above the floor, or None."""
    for c in contacts_for_company(conn, company):     # already sorted by confidence DESC
        if c.get("email") and (c.get("confidence") or 0) >= min_confidence:
            return c
    return None
