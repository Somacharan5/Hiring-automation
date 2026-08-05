"""Zero-cost email finder — the PRIMARY path before any paid API.

Signals, all free and from public data:
  1. JD parse     — a recruiter's address printed in the job description.
  2. Site crawl   — sample emails + name↔email pairs on careers/team/contact pages.
  3. GitHub       — commit emails at the domain (only if GITHUB_TOKEN is set).
  4. Google CSE   — a recruiter/dept-head NAME to build an address for (only if keyed).
  5. Pattern      — infer the company's email format from a confirmed sample, then
                    apply it to the hiring person's name.

Accuracy rule (never email a guessed human): a constructed person address is returned
ONLY when the company's pattern was confirmed by a real sample at the domain, or the
person's address was found published outright. Otherwise we return None and the caller
falls back to careers@.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter

import requests

from ..db import _load_env

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
NAME_RE = re.compile(r"\b([A-Z][a-z]{1,15})\s+([A-Z][a-z]{1,15})\b")
_UA = {"User-Agent": "Mozilla/5.0 (compatible; JobPilot/1.0; +https://example.com/bot)"}
_TIMEOUT = 8

# Locals that are a role inbox, never a specific person.
_ROLE_LOCALS = {"careers", "career", "jobs", "job", "hr", "recruiting", "recruitment",
                "talent", "people", "hiring", "apply", "work", "info", "hello", "contact",
                "support", "sales", "marketing", "admin", "noreply", "no-reply", "team",
                "office", "hi", "help", "press", "media", "legal", "privacy", "billing"}
_CAREER_PATHS = ("", "careers", "careers/jobs", "jobs", "team", "about", "about-us",
                 "company/team", "our-team", "people", "contact", "contact-us")


# ── name + pattern primitives (pure, unit-tested) ────────────────────

def _norm(s: str) -> str:
    """Lowercase ASCII, letters only: 'José Änder' -> 'joseander' pieces via split first."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def split_name(full: str) -> tuple[str, str]:
    """('Priya Sharma') -> ('priya','sharma'); single token -> (token, '')."""
    parts = [_norm(p) for p in re.split(r"\s+", (full or "").strip()) if _norm(p)]
    if not parts:
        return "", ""
    return (parts[0], parts[-1]) if len(parts) >= 2 else (parts[0], "")


# local-part template → builder(first, last) -> local
_PATTERNS: dict[str, "callable"] = {
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast":  lambda f, l: f"{f}{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "flast":      lambda f, l: f"{f[:1]}{l}",
    "f.last":     lambda f, l: f"{f[:1]}.{l}",
    "first.l":    lambda f, l: f"{f}.{l[:1]}",
    "firstl":     lambda f, l: f"{f}{l[:1]}",
    "lastfirst":  lambda f, l: f"{l}{f}",
    "last.first": lambda f, l: f"{l}.{f}",
    "first":      lambda f, l: f,
    "last":       lambda f, l: l,
    "fl":         lambda f, l: f"{f[:1]}{l[:1]}",
}


def detect_pattern(local: str, first: str, last: str) -> str | None:
    """Which template turns (first,last) into this local part? None if no match.
    The local keeps its separators ('.'/'_'); only the name is letter-normalized."""
    local = (local or "").strip().lower()
    first, last = _norm(first), _norm(last)
    if not local or not first:
        return None
    for name, build in _PATTERNS.items():
        if (name in ("first", "fl") or last) and build(first, last) == local:
            return name
    return None


def build_email(pattern: str, first: str, last: str, domain: str) -> str | None:
    b = _PATTERNS.get(pattern)
    if not b or not first or (not last and pattern not in ("first",)):
        return None
    return f"{b(first, last)}@{domain}"


def infer_pattern(pairs: list[tuple[str, str]], domain: str) -> str | None:
    """Majority pattern across (name, email) samples that sit on `domain`."""
    votes: Counter[str] = Counter()
    for name, email in pairs:
        if not email.lower().endswith("@" + domain.lower()):
            continue
        first, last = split_name(name)
        p = detect_pattern(email.split("@", 1)[0], first, last)
        if p:
            votes[p] += 1
    return votes.most_common(1)[0][0] if votes else None


# ── free public-data sources (best-effort, network) ──────────────────

def _fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers=_UA, timeout=_TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except Exception:  # noqa: BLE001 — a dead page must not kill discovery
        return None
    return None


def _emails_on_domain(text: str, domain: str) -> list[str]:
    out, seen = [], set()
    for m in EMAIL_RE.findall(text or ""):
        e = m.lower()
        if e.endswith("@" + domain.lower()) and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _name_near(text: str, email: str) -> str | None:
    """A 'First Last' appearing just before the email (team-page 'Jane Doe jane@…')."""
    i = text.find(email)
    if i == -1:
        return None
    m = None
    for m in NAME_RE.finditer(text, max(0, i - 140), i):
        pass                                    # keep the last (closest) match
    return f"{m.group(1)} {m.group(2)}" if m else None


def harvest_domain(domain: str, max_pages: int = 6) -> dict:
    """Crawl a few employer pages → {emails: [...], pairs: [(name,email)]}."""
    emails: list[str] = []
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in _CAREER_PATHS[:max_pages]:
        html = _fetch(f"https://{domain}/{path}".rstrip("/"))
        if not html:
            continue
        for e in _emails_on_domain(html, domain):
            if e not in seen:
                seen.add(e)
                emails.append(e)
                nm = _name_near(html, e)
                if nm and e.split("@", 1)[0] not in _ROLE_LOCALS:
                    pairs.append((nm, e))
        if pairs and len(emails) >= 2:          # enough to infer a pattern — stop early
            break
    return {"emails": emails, "pairs": pairs}


def _github_pairs(domain: str) -> list[tuple[str, str]]:
    """Commit-author (name,email) at the domain — only if GITHUB_TOKEN is set."""
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if not tok:
        return []
    try:
        r = requests.get("https://api.github.com/search/commits",
                         params={"q": f"author-email:@{domain}", "per_page": 20},
                         headers={"Authorization": f"Bearer {tok}",
                                  "Accept": "application/vnd.github.cloak-preview+json"},
                         timeout=_TIMEOUT)
        items = r.json().get("items", []) if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        return []
    pairs = []
    for it in items:
        au = (it.get("commit") or {}).get("author") or {}
        nm, em = au.get("name"), (au.get("email") or "").lower()
        if nm and em.endswith("@" + domain.lower()) and " " in nm:
            pairs.append((nm, em))
    return pairs


def _cse_person_name(company: str, role_title: str | None) -> str | None:
    """A recruiter/dept-head NAME via Google CSE (only if keyed). Parses it from the
    result title, e.g. 'Priya Sharma - Talent Acquisition at Acme | LinkedIn'."""
    key, cx = os.environ.get("GOOGLE_CSE_KEY"), os.environ.get("GOOGLE_CSE_CX")
    if not key or not cx:
        return None
    who = "head of product" if "product" in (role_title or "").lower() else "recruiter"
    try:
        r = requests.get("https://www.googleapis.com/customsearch/v1",
                         params={"key": key, "cx": cx,
                                 "q": f'"{company}" {who} site:linkedin.com/in', "num": 5},
                         timeout=_TIMEOUT)
        items = r.json().get("items", []) if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        return None
    for it in items:
        head = re.split(r"[-|–]", it.get("title", ""), 1)[0].strip()
        m = NAME_RE.fullmatch(head) or NAME_RE.match(head)
        if m:
            return f"{m.group(1)} {m.group(2)}"
    return None


# ── public API ───────────────────────────────────────────────────────

def _is_personal_local(local: str) -> bool:
    l = local.lower()
    return l not in _ROLE_LOCALS and (("." in l) or bool(re.fullmatch(r"[a-z]{3,20}", l)))


def find_person(company: str, domain: str, role_title: str | None,
                job_description: str | None, settings: dict | None = None) -> dict | None:
    """Best hiring-person address from FREE public data, or None. Never a bare guess:
    the address is either published outright or built from a domain-confirmed pattern."""
    _load_env()
    if not domain:
        return None

    # 1. A personal address printed in the JD (highest signal, zero fetches).
    for e in _emails_on_domain(job_description or "", domain):
        if _is_personal_local(e.split("@", 1)[0]):
            return {"email": e, "name": None, "title": None,
                    "confidence": 88, "source": "jd"}

    # 2. Crawl the employer site for sample emails + name↔email pairs.
    site = harvest_domain(domain)
    for name, e in site["pairs"]:               # a named person published on the site
        if _is_personal_local(e.split("@", 1)[0]):
            return {"email": e, "name": name, "title": None,
                    "confidence": 86, "source": "site"}

    # 3. Confirm the company's email pattern from any (name,email) sample.
    pairs = site["pairs"] + _github_pairs(domain)
    pattern = infer_pattern(pairs, domain)
    if not pattern:
        return None                              # no confirmed pattern → don't construct

    # 4. Find the hiring person's NAME (site pairs give one; else CSE) and build it.
    target = None
    if site["pairs"]:
        target = site["pairs"][0][0]
    target = target or _cse_person_name(company, role_title)
    if not target:
        return None
    first, last = split_name(target)
    email = build_email(pattern, first, last, domain)
    if not email:
        return None
    return {"email": email, "name": target, "title": None,
            "confidence": 80, "source": f"pattern:{pattern}"}
