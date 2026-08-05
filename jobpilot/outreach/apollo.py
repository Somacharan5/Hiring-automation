"""Apollo.io — paid-overflow people finder, complementary to Hunter. Apollo is
strong at locating a person by TITLE at a domain (the recruiter, or the head of the
hiring department), which is exactly "the person who owns the req".

Domain-first and defensive: we only search a domain we already trust, and Apollo's
free tier often masks the email ('email_not_unlocked@…') — when it does, we return
nothing and let the caller fall back to Hunter, then to careers@. Keys rotate through
a pool so we ride several free tiers.
"""

from __future__ import annotations

import requests

from .keypool import KeyPool

_BASE = "https://api.apollo.io/v1"
TIMEOUT = 25


class _QuotaSpent(Exception):
    """Current Apollo key is out of credits / rate-limited — rotate to the next."""


def _pool() -> KeyPool:
    # No cheap free quota endpoint, so we rotate reactively on credit/rate errors.
    return KeyPool(["APOLLO_API_KEYS", "APOLLO_API_KEY"])


def has_keys() -> bool:
    return bool(_pool())


def _is_quota_error(e: Exception) -> bool:
    return isinstance(e, _QuotaSpent)


def _masked(email: str | None) -> bool:
    return not email or "not_unlocked" in email or "email_not_found" in email


def _post(key: str, path: str, payload: dict) -> dict:
    r = requests.post(f"{_BASE}/{path}",
                      headers={"X-Api-Key": key, "Content-Type": "application/json",
                               "Cache-Control": "no-cache"},
                      json=payload, timeout=TIMEOUT)
    if r.status_code in (401, 402, 403, 429):
        raise _QuotaSpent()
    try:
        body = r.json()
    except ValueError:
        return {}
    if r.status_code >= 400:
        if any(w in str(body).lower() for w in ("credit", "quota", "limit", "upgrade")):
            raise _QuotaSpent()
        return {}
    return body or {}


def _titles(role_title: str | None) -> list[str]:
    """Who to look for: recruiter/HR first, then the hiring department's head."""
    titles = ["recruiter", "technical recruiter", "talent acquisition",
              "head of talent", "hr manager", "people operations", "hiring manager"]
    if "product" in (role_title or "").lower():
        titles += ["head of product", "vp product", "director of product",
                   "chief product officer", "product lead", "group product manager"]
    return titles


def find_person(domain: str, role_title: str | None = None) -> dict | None:
    """Best hiring-owner at `domain` with a REVEALED email, or None. Returns
    {email, name, title, confidence, source='apollo'}."""
    if not domain:
        return None
    pool = _pool()
    if not pool:
        return None

    search = pool.run(lambda key: _post(key, "mixed_people/search", {
        "q_organization_domains": domain, "person_titles": _titles(role_title),
        "page": 1, "per_page": 10}), _is_quota_error)
    for person in (search or {}).get("people", []):
        email = person.get("email")
        if _masked(email):                              # try to unlock via people/match
            m = pool.run(lambda key, p=person: _post(key, "people/match", {
                "first_name": p.get("first_name"), "last_name": p.get("last_name"),
                "domain": domain, "reveal_personal_emails": False}), _is_quota_error)
            email = ((m or {}).get("person") or {}).get("email")
        if _masked(email):
            continue
        if (email.split("@", 1)[-1].lower() != domain.lower()):
            continue                                    # only accept the trusted domain
        return {"email": email,
                "name": person.get("name") or " ".join(
                    filter(None, [person.get("first_name"), person.get("last_name")])),
                "title": person.get("title") or "", "confidence": 90, "source": "apollo"}
    return None
