"""Hunter.io — the specific-email fallback when generic domain resolution fails.

Free tier is ~50 domain-searches + 100 verifications/month, so this is used
sparingly: only for shortlisted companies with no resolvable address, and each
result is stored as a contact (never re-queried). Degrades to nothing if the key
is missing or the quota is spent.
"""

from __future__ import annotations

import os

import requests

from ..db import _load_env

_BASE = "https://api.hunter.io/v2"
TIMEOUT = 25

# Locals that are never a job-application inbox.
_BAD_LOCALS = {"noreply", "no-reply", "press", "legal", "abuse", "privacy", "security",
               "billing", "info", "marketing", "sales", "support", "newsletter",
               "promociones", "hello", "contact", "admin", "webmaster"}
# Hints (in department / position / local-part) that an address is recruiting-relevant.
_RECRUIT_HINTS = ("recruit", "talent", "people", "hr", "human resources", "hiring", "staffing")


def _key() -> str:
    _load_env()
    return os.environ.get("HUNTER_API_KEY", "").strip()


def account() -> dict | None:
    """Remaining Hunter quota, or None if no key / call fails."""
    key = _key()
    if not key:
        return None
    try:
        rs = (requests.get(f"{_BASE}/account", params={"api_key": key}, timeout=15)
              .json().get("data", {}).get("requests", {}))
    except Exception:  # noqa: BLE001
        return None
    s, v = rs.get("searches", {}), rs.get("verifications", {})
    return {"searches_left": (s.get("available", 0) - s.get("used", 0)),
            "verifications_left": (v.get("available", 0) - v.get("used", 0))}


def has_quota(min_searches: int = 1) -> bool:
    a = account()
    return bool(a) and a["searches_left"] >= min_searches


def domain_search(company: str | None = None, domain: str | None = None,
                  limit: int = 10) -> dict | None:
    """Resolve a company/domain to {domain, pattern, emails[]}. Costs 1 search."""
    key = _key()
    if not key or not (company or domain):
        return None
    params = {"api_key": key, "limit": limit}
    params["domain" if domain else "company"] = domain or company
    try:
        d = requests.get(f"{_BASE}/domain-search", params=params, timeout=TIMEOUT).json().get("data", {})
    except Exception:  # noqa: BLE001
        return None
    return {
        "domain": d.get("domain"), "pattern": d.get("pattern"),
        "emails": [{"email": e.get("value"), "type": e.get("type"),
                    "confidence": e.get("confidence") or 0, "dept": (e.get("department") or "").lower(),
                    "position": e.get("position") or "",
                    "name": " ".join(filter(None, [e.get("first_name"), e.get("last_name")]))}
                   for e in (d.get("emails") or []) if e.get("value")],
    }


def best_recruiting_email(result: dict | None) -> dict | None:
    """Pick the most recruiting-relevant address from a domain-search result, or None."""
    if not result:
        return None
    scored: list[tuple[int, dict]] = []
    for e in result.get("emails", []):
        local = (e["email"] or "").split("@", 1)[0].lower()
        if local in _BAD_LOCALS:
            continue
        blob = f"{e['dept']} {e['position'].lower()} {local}"
        if any(h in blob for h in _RECRUIT_HINTS):
            scored.append((200 + e["confidence"], e))          # recruiting inbox/person — best
        elif e["type"] == "personal" and e["position"]:
            scored.append((e["confidence"], e))                # a named person with a role
    scored.sort(key=lambda x: -x[0])
    return scored[0][1] if scored else None


def verify(email: str) -> dict | None:
    """Verify deliverability (costs 1 verification). Returns {status, score} or None."""
    key = _key()
    if not key or not email:
        return None
    try:
        d = requests.get(f"{_BASE}/email-verifier", params={"api_key": key, "email": email},
                         timeout=TIMEOUT).json().get("data", {})
    except Exception:  # noqa: BLE001
        return None
    return {"status": d.get("status"), "score": d.get("score")}
