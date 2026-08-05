"""Hunter.io — paid-overflow finder, used only when the free path (free_finder)
turns up nothing. Domain-first: we only ever ask Hunter about a domain we have
already resolved and trusted (never `company=…`, which is what mapped SAGON onto
faro.com). Keys come from a rotating pool so we ride several free tiers.

Free tier ≈ 50 domain-searches + 100 verifications / key / month; with a pool of
8-10 keys that is plenty. Degrades to nothing when every key is spent.
"""

from __future__ import annotations

import requests

from .keypool import KeyPool

_BASE = "https://api.hunter.io/v2"
TIMEOUT = 25

# Locals that are never a job-application inbox.
_BAD_LOCALS = {"noreply", "no-reply", "press", "legal", "abuse", "privacy", "security",
               "billing", "info", "marketing", "sales", "support", "newsletter",
               "promociones", "hello", "contact", "admin", "webmaster"}
_RECRUIT_HINTS = ("recruit", "talent", "people", "hr", "human resources", "hiring", "staffing")


class _QuotaSpent(Exception):
    """Raised inside a pooled call when the current key is out of quota/rate-limited."""


# ── key pool ─────────────────────────────────────────────────────────

def _searches_left(key: str) -> int:
    try:
        rs = (requests.get(f"{_BASE}/account", params={"api_key": key}, timeout=15)
              .json().get("data", {}).get("requests", {}).get("searches", {}))
    except Exception:  # noqa: BLE001
        return 1                                   # can't tell → let the call try
    return rs.get("available", 0) - rs.get("used", 0)


def _pool() -> KeyPool:
    return KeyPool(["HUNTER_API_KEYS", "HUNTER_API_KEY"],
                   has_quota=lambda k: _searches_left(k) >= 1)


def has_quota(min_searches: int = 1) -> bool:
    """True if any pooled key still has at least `min_searches` of search quota."""
    pool = KeyPool(["HUNTER_API_KEYS", "HUNTER_API_KEY"],
                   has_quota=lambda k: _searches_left(k) >= min_searches)
    return pool.current() is not None


def _is_quota_error(e: Exception) -> bool:
    return isinstance(e, _QuotaSpent)


def _get(key: str, path: str, params: dict) -> dict:
    r = requests.get(f"{_BASE}/{path}", params={**params, "api_key": key}, timeout=TIMEOUT)
    if r.status_code in (429, 402) or (r.status_code == 401):
        raise _QuotaSpent()
    body = r.json()
    if r.status_code >= 400:
        errs = str(body.get("errors") or body)
        if any(w in errs.lower() for w in ("usage", "quota", "limit", "reset")):
            raise _QuotaSpent()
        return {}
    return body.get("data", {}) or {}


# ── search / verify (pooled) ─────────────────────────────────────────

def domain_search(domain: str, limit: int = 10) -> dict | None:
    """People + inferred pattern for a KNOWN domain. Costs 1 search on the live key."""
    if not domain:
        return None
    pool = _pool()

    def call(key: str) -> dict:
        return _get(key, "domain-search", {"domain": domain, "limit": limit})

    d = pool.run(call, _is_quota_error)
    if not d:
        return None
    return {
        "domain": d.get("domain"), "pattern": d.get("pattern"),
        "emails": [{"email": e.get("value"), "type": e.get("type"),
                    "confidence": e.get("confidence") or 0, "dept": (e.get("department") or "").lower(),
                    "position": e.get("position") or "",
                    "name": " ".join(filter(None, [e.get("first_name"), e.get("last_name")]))}
                   for e in (d.get("emails") or []) if e.get("value")],
    }


def verify(email: str) -> dict | None:
    """Deliverability check (costs 1 verification). Returns {status, score} or None."""
    if not email:
        return None
    d = _pool().run(lambda key: _get(key, "email-verifier", {"email": email}), _is_quota_error)
    return {"status": d.get("status"), "score": d.get("score")} if d else None


# ── person selection ─────────────────────────────────────────────────

def _role_hints(role_title: str | None) -> tuple[str, ...]:
    """Department-head hints for THIS role (a PM req → product leadership)."""
    t = (role_title or "").lower()
    if "product" in t:
        return ("head of product", "chief product", "cpo", "vp product",
                "director of product", "product lead", "director, product")
    return ()


def best_recruiting_email(result: dict | None, role_title: str | None = None) -> dict | None:
    """Pick the most hiring-relevant address: a recruiter/HR person first, then the
    department head for this role, then any named person with a title."""
    if not result:
        return None
    role_hints = _role_hints(role_title)
    scored: list[tuple[int, dict]] = []
    for e in result.get("emails", []):
        local = (e["email"] or "").split("@", 1)[0].lower()
        if local in _BAD_LOCALS:
            continue
        blob = f"{e['dept']} {e['position'].lower()} {local}"
        if any(h in blob for h in _RECRUIT_HINTS):
            scored.append((300 + e["confidence"], e))          # recruiter / HR — best
        elif role_hints and any(h in blob for h in role_hints):
            scored.append((250 + e["confidence"], e))          # the role's dept head
        elif e["type"] == "personal" and e["position"]:
            scored.append((e["confidence"], e))                # some named person with a role
    scored.sort(key=lambda x: -x[0])
    return scored[0][1] if scored else None
