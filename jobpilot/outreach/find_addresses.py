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


_LEGAL = re.compile(r"\b(pvt\.?\s*ltd\.?|private\s+limited|ltd\.?|limited|inc\.?|llc|llp|"
                    r"corp\.?|corporation|gmbh|plc|pte\.?\s*ltd\.?|co\.?|sa|ag|bv|srl)\b\.?", re.I)


def clean_company(name: str | None) -> str:
    """Strip legal suffixes + descriptive tails so the slug/Hunter see the core brand.
    'TerraTern Pvt Ltd' -> 'TerraTern'; \"McDonald's Global Office in India\" -> \"McDonald's\"."""
    s = re.sub(r"\b(global\s+)?office\b.*$", "", name or "", flags=re.I)   # "... Global Office in India"
    s = re.sub(r"[,(].*$", "", s)                                          # ", ..." / "(...)" tails
    s = _LEGAL.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


# Multi-label public suffixes we must look *past* to find the registrable name
# (so 'foo.co.in' -> 'foo', not 'co').
_TWO_LABEL_TLDS = {"co", "com", "org", "net", "gov", "ac", "edu"}


def _domain_root(domain: str) -> str:
    """The registrable brand label of a domain: 'careers.acme.co.uk' -> 'acme'."""
    host = (domain or "").lower().strip().removeprefix("www.")
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in _TWO_LABEL_TLDS:   # foo.co.in / foo.com.au
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def domain_matches_company(domain: str, company: str) -> bool:
    """True if `domain` plausibly belongs to `company` — the guard that stops
    Hunter/guesses mapping 'SAGON Technologies' onto faro.com or 'Perfios' onto
    perisoftware.com. Compares the domain's brand label to the cleaned company slug.
    """
    root = _domain_root(domain)
    slug = re.sub(r"[^a-z0-9]", "", clean_company(company).lower())
    if not root or not slug:
        return False
    if root == slug:
        return True
    if len(root) >= 4 and len(slug) >= 4 and (root in slug or slug in root):
        return True                                       # 'slice' ⊂ 'sliceit', etc.
    from rapidfuzz import fuzz
    return max(fuzz.token_set_ratio(root, slug), fuzz.partial_ratio(root, slug)) >= 85


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
    slug = re.sub(r"[^a-z0-9]", "", clean_company(company).lower())
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
        return override                       # curated = human-verified, trusted as-is
    for d in candidate_domains(company, job_url):
        if has_mx(d) and domain_matches_company(d, company):
            return d                          # MX-valid AND consistent with the company name
    return None


def discover_generic(conn, company: str, job_url: str | None = None,
                     settings: dict | None = None) -> list[dict]:
    """Register careers@/jobs@/hr@ for the company's trusted, MX-valid domain. Returns
    the contacts, or [] when no company-consistent domain resolves (left for review —
    we never invent an address on an unverified domain). Person discovery is separate,
    in resolve_recipients()."""
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


# ── Dual-recipient resolution: careers@ + the hiring person ──────────

# Sources we already trust without spending a paid verification: an address that was
# published outright (JD/site) or built from a domain-confirmed pattern.
_FREE_SOURCES = ("jd", "site", "github", "pattern")


def _person_gate_ok(email: str, confidence: int, source: str, settings: dict | None) -> tuple[bool, bool]:
    """(accept?, separately_verified?). Free/published/pattern-confirmed addresses pass
    as-is; a paid guess below the confidence floor must clear a (free-key) verification."""
    if source.split(":", 1)[0] in _FREE_SOURCES:
        return True, False
    ed = (settings or {}).get("email_discovery", {}) or {}
    if confidence >= int(ed.get("min_person_confidence", 90)):
        return True, False
    if not ed.get("verify_before_send", True):
        return False, False
    from . import hunter
    v = hunter.verify(email)
    ok = bool(v) and (v.get("status") == "valid"
                      or (v.get("status") == "accept_all" and (v.get("score") or 0) >= 80))
    return ok, ok


def _find_person(company: str, domain: str, role: str | None,
                 jd: str | None, settings: dict | None) -> dict | None:
    """The hiring owner at `domain`: FREE public data first, then Apollo, then Hunter.
    Every candidate must sit on the trusted domain and clear `_person_gate_ok`."""
    ed = (settings or {}).get("email_discovery", {}) or {}
    dl = domain.lower()

    def _on_domain(e: str | None) -> bool:
        return bool(e) and e.split("@", 1)[-1].lower() == dl

    from . import free_finder                                   # 1. free (zero cost)
    p = free_finder.find_person(company, domain, role, jd, settings)
    if p and _on_domain(p.get("email")):
        p["verified"] = False
        return p

    if ed.get("use_apollo", True):                             # 2. Apollo (paid overflow)
        try:
            from . import apollo
            if apollo.has_keys():
                ap = apollo.find_person(domain, role)
                if ap and _on_domain(ap.get("email")):
                    ok, ver = _person_gate_ok(ap["email"], ap.get("confidence", 90), "apollo", settings)
                    if ok:
                        ap["verified"] = ver
                        return ap
        except Exception:  # noqa: BLE001 — overflow must never break discovery
            pass

    if ed.get("use_hunter", True):                            # 3. Hunter (paid overflow)
        try:
            from . import hunter
            if hunter.has_quota():
                hp = hunter.best_recruiting_email(hunter.domain_search(domain), role)
                if hp and _on_domain(hp.get("email")):
                    ok, ver = _person_gate_ok(hp["email"], hp.get("confidence", 0), "hunter", settings)
                    if ok:
                        return {"email": hp["email"], "name": hp.get("name") or None,
                                "title": hp.get("position") or None,
                                "confidence": hp.get("confidence", 0), "source": "hunter",
                                "verified": ver}
        except Exception:  # noqa: BLE001
            pass
    return None


def resolve_recipients(conn, job, settings: dict | None = None) -> dict:
    """Build the To: list for a job — [hiring person, careers@] on a company-consistent
    domain. If no trusted domain resolves, returns an empty list (left for review, never
    a wrong send). Registers both as contacts; returns the person contact for the greeting.
    """
    company, job_url = job["company"], job.get("url")
    role, jd = job.get("title"), job.get("description")

    domain = resolve_domain(company, job_url)
    if not domain:
        return {"domain": None, "to": [], "person_contact": None, "generic": None, "person": None}

    generics = discover_generic(conn, company, job_url=job_url, settings=settings)
    generic_email = next((c["email"] for c in generics
                          if (c.get("email") or "").lower().startswith("careers@")), None)
    if not generic_email and generics:
        generic_email = generics[0].get("email")

    person = _find_person(company, domain, role, jd, settings)
    person_contact, to = None, []
    if person and person.get("email"):
        cid = add_contact(conn, company, email=person["email"], name=person.get("name"),
                          title=person.get("title"),
                          source=(person.get("source") or "free").split(":", 1)[0],
                          tier="specific", confidence=int(person.get("confidence") or 80),
                          verified=bool(person.get("verified")))
        person_contact = {"id": cid, "name": person.get("name"),
                          "title": person.get("title"), "email": person["email"]}
        to.append(person["email"])
    if generic_email:
        to.append(generic_email)

    seen: set[str] = set()
    to = [e for e in to if e and not (e in seen or seen.add(e))]
    return {"domain": domain, "to": to, "person_contact": person_contact,
            "generic": generic_email, "person": person}
