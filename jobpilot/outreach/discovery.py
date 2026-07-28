"""Free-first recruiter discovery orchestrator.

Pipeline, cheapest and most trustworthy first:

    1. site crawl      — addresses the company published itself
    2. GitHub          — commit-author addresses on the company's org
    3. pattern learning— infer the domain's naming convention from 1 & 2
    4. LinkedIn names  → apply the learned pattern → SMTP-verify
    5. keyed sources   — Google CSE, then Hunter — ONLY if we still have nothing
    6. role addresses  — careers@/jobs@ … as a floor, always verified before use

Step 3 is what makes this work without a paid API. Engineers leak the corporate
email format in git metadata; recruiters never do. Once we know Figma writes
`ckalmar@figma.com` and Postman writes `devesh.kumar@postman.com`, a recruiter's
name from LinkedIn becomes a *targeted* guess instead of eight blind permutations
— and on a non-catch-all domain the SMTP probe then settles it outright.

Everything is verified through `verify.verify_email` before it is stored, and
confidence is capped by what verification could actually prove. On a catch-all
domain nothing is ever auto-sendable, because on a catch-all domain nothing can
be proven.
"""

from __future__ import annotations

import os
import re
from collections import Counter

from ..db import add_contact, contacts_for_company
from ..llm import _load_env
from . import free_sources as fs
from . import store, verify
from .finder import (ContactCandidate, GENERIC_LOCALPARTS, find_domain,
                     hunter_domain_search, _warn)

# ── confidence bands (the contract callers enforce) ──────────────────
#
# Auto-send is allowed ONLY at TIER_VERIFIED / TIER_HIGH. Everything else is a
# review-queue item. These are deliberately strict: a bounce costs Gmail sending
# reputation, while a review costs one click.

CONF_VERIFIED = 85   # SMTP-proved mailbox on a recipient-validating domain
CONF_HIGH = 70       # strong evidence, not SMTP-proved (e.g. published + MX ok)
CONF_REVIEW = 40     # plausible; a human must look at it
CONF_FLOOR = 20      # below this we don't even store it

TIER_VERIFIED = "verified"
TIER_HIGH = "high"
TIER_REVIEW = "review"
TIER_REJECT = "reject"

AUTO_SENDABLE_TIERS = (TIER_VERIFIED, TIER_HIGH)

# A catch-all domain can never exceed this, whatever the source claimed —
# deliberately one point below CONF_HIGH so it can never become auto-sendable.
CATCH_ALL_CAP = CONF_HIGH - 5      # 65
UNKNOWN_CAP = 55                   # SMTP inconclusive (port 25 blocked, greylist)


# Defaults for the `email_discovery:` block in config/settings.yaml. Every key
# has a safe default here, so the block is optional.
DEFAULT_CFG = {
    "sources": ["site_crawl", "github", "linkedin_names", "cse", "hunter"],
    "min_confidence_to_autosend": CONF_HIGH,
    "review_catch_all": True,
    "bounce_scan_days": 30,
    "max_verify_per_company": 12,
    "smtp_verify": True,
}


def email_discovery_cfg(settings: dict | None) -> dict:
    """Read `email_discovery:` with safe defaults. A malformed value never
    loosens a safety setting — it falls back to the stricter default."""
    cfg = dict(DEFAULT_CFG)
    block = (settings or {}).get("email_discovery") or {}
    if not isinstance(block, dict):
        return cfg
    for key, default in DEFAULT_CFG.items():
        if key not in block or block[key] is None:
            continue
        val = block[key]
        try:
            if isinstance(default, bool):
                cfg[key] = bool(val)
            elif isinstance(default, int):
                cfg[key] = int(val)
            elif isinstance(default, list):
                cfg[key] = [str(v).strip().lower() for v in val] if isinstance(val, list) else default
            else:
                cfg[key] = val
        except (TypeError, ValueError):
            cfg[key] = default
    # Never let config drop the bar below the catch-all ceiling — that would make
    # unprovable addresses auto-sendable, which is the one thing this must prevent.
    cfg["min_confidence_to_autosend"] = max(int(cfg["min_confidence_to_autosend"]),
                                            CATCH_ALL_CAP + 1)
    return cfg


def tier_for(confidence: int, verified: bool = False) -> str:
    """Map a contact onto a structural quality band (independent of config)."""
    if verified and confidence >= CONF_VERIFIED:
        return TIER_VERIFIED
    if confidence >= CONF_HIGH:
        return TIER_HIGH
    if confidence >= CONF_REVIEW:
        return TIER_REVIEW
    return TIER_REJECT


def can_auto_send(contact, settings: dict | None = None,
                  min_confidence: int | None = None) -> bool:
    """True only for addresses we genuinely proved AND that clear the configured
    bar (`email_discovery.min_confidence_to_autosend`). Accepts dict or sqlite Row."""
    get = contact.get if isinstance(contact, dict) else lambda k, d=None: contact[k]
    try:
        conf = int(get("confidence") or 0)
        ver = bool(get("verified") or False)
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    if min_confidence is None:
        min_confidence = email_discovery_cfg(settings)["min_confidence_to_autosend"]
    return tier_for(conf, ver) in AUTO_SENDABLE_TIERS and conf >= min_confidence


# ── pattern learning ─────────────────────────────────────────────────

PATTERN_TEMPLATES = {
    "first.last": lambda f, l: f"{f}.{l}",
    "flast": lambda f, l: f"{f[0]}{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "first": lambda f, l: f,
    "firstl": lambda f, l: f"{f}{l[0]}",
    "f.last": lambda f, l: f"{f[0]}.{l}",
    "last.first": lambda f, l: f"{l}.{f}",
    "lastf": lambda f, l: f"{l}{f[0]}",
}


def infer_pattern(known: list[tuple[str, str]]) -> tuple[str | None, float]:
    """Given [(local_part, "First Last"), …], guess the domain's convention.

    Returns (template_name, agreement_ratio). Two agreeing examples is a usable
    signal; one is a coincidence waiting to happen, so the caller should weight
    by the ratio and by how many samples backed it.
    """
    votes: Counter[str] = Counter()
    usable = 0
    for local, name in known:
        parts = [re.sub(r"[^a-z]", "", p.lower()) for p in (name or "").split()]
        parts = [p for p in parts if p]
        if len(parts) < 2:
            continue
        first, last = parts[0], parts[-1]
        local = local.lower()
        usable += 1
        for tmpl, fn in PATTERN_TEMPLATES.items():
            try:
                if fn(first, last) == local:
                    votes[tmpl] += 1
            except IndexError:
                continue
    if not usable or not votes:
        return None, 0.0
    tmpl, n = votes.most_common(1)[0]
    return tmpl, n / usable


# Blind-guess order, most common corporate convention first.
PATTERN_FREQUENCY = ("first.last", "flast", "firstlast", "first_last",
                     "firstl", "f.last", "first", "last.first", "lastf")


def blind_pattern_candidates(domain: str, first: str, last: str,
                             burned: set[str] | None = None,
                             limit: int = 3) -> list[tuple[str, str]]:
    """[(email, template), …] in frequency order, skipping burned conventions.

    Template-aware on purpose: knowing that `flast` bounced at this domain is
    only useful if we can tell which guesses `flast` would produce. A plain list
    of addresses cannot express that, so `pattern_guess`'s output is not enough
    once the bounce loop has taught us something.
    """
    burned = burned or set()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tmpl in PATTERN_FREQUENCY:
        if f"__pattern__{tmpl}" in burned:
            continue
        email = apply_pattern(tmpl, domain, first, last)
        if not email or email in seen:
            continue
        if email.split("@", 1)[0] in burned:
            continue
        seen.add(email)
        out.append((email, tmpl))
        if len(out) >= limit:
            break
    return out


def apply_pattern(template: str, domain: str, first: str, last: str) -> str | None:
    fn = PATTERN_TEMPLATES.get(template)
    f = re.sub(r"[^a-z]", "", (first or "").lower())
    l = re.sub(r"[^a-z]", "", (last or "").lower())
    if not fn or not f or not l:
        return None
    try:
        return f"{fn(f, l)}@{domain}".lower()
    except IndexError:
        return None


def learn_domain_pattern(candidates: list[ContactCandidate]) -> tuple[str | None, float, int]:
    """Infer the naming convention from candidates that carry both email and name."""
    known = [(c.email.split("@", 1)[0], c.name)
             for c in candidates if c.email and c.name and "@" in c.email
             and verify.role_kind(c.email.split("@", 1)[0]) is None]
    tmpl, ratio = infer_pattern(known)
    return tmpl, ratio, len(known)


# ── verification + scoring ───────────────────────────────────────────

def score_candidate(c: ContactCandidate, vr: verify.VerificationResult) -> ContactCandidate:
    """Fold a verification verdict into a candidate's confidence, honestly.

    The verification result is authoritative in both directions: it can promote a
    guess to sendable, and it can (and often does) demote a confident-looking
    find to zero.
    """
    c.notes.append(f"verify: {vr.status} — {vr.reason}")

    if vr.status == verify.INVALID:
        c.confidence, c.verified = 0, False
        return c

    if vr.status == verify.VERIFIED:
        c.verified = True
        # A proved mailbox is a proved mailbox; source only breaks ties above the bar.
        c.confidence = min(95, max(CONF_VERIFIED, vr.confidence, c.confidence))
        # …except that "it will arrive" is not "it should be sent". A press@ or
        # billing@ inbox verifies perfectly and is still the wrong audience, so
        # the verifier's own cap wins and the address goes to a human.
        if vr.role in ("misdirected", "generic"):
            c.confidence = min(c.confidence, vr.confidence)
            c.notes.append(f"{vr.role} inbox → review queue despite being deliverable")
        return c

    c.verified = False
    if vr.status == verify.CATCH_ALL:
        # THE important cap. We cannot prove anything on this domain, so no
        # amount of source confidence may make it auto-sendable.
        c.confidence = min(c.confidence, CATCH_ALL_CAP)
        c.notes.append("catch-all domain → review queue (SMTP cannot prove this address)")
    elif vr.status == verify.UNKNOWN:
        c.confidence = min(c.confidence, UNKNOWN_CAP)
    else:  # LIKELY
        c.confidence = min(c.confidence, CONF_HIGH)
    return c


def _dedupe(candidates: list[ContactCandidate]) -> list[ContactCandidate]:
    best: dict[str, ContactCandidate] = {}
    for c in candidates:
        if not c.email:
            continue
        k = c.email.strip().lower()
        if k not in best or c.confidence > best[k].confidence:
            if k in best and best[k].name and not c.name:
                c.name = best[k].name
            best[k] = c
    return sorted(best.values(), key=lambda c: c.confidence, reverse=True)


# ── the orchestrator ─────────────────────────────────────────────────

def discover_and_verify(conn, company: str, domain: str | None = None,
                        job_url: str | None = None, settings: dict | None = None,
                        use_smtp: bool = True, use_linkedin: bool = False,
                        max_verify: int | None = None) -> list[dict]:
    """Find, verify, persist and rank contacts for one company.

    Free sources run first and keyed sources only run if the free ones came up
    empty, so a user with no API keys at all gets the same pipeline minus the
    last resort. Returns ranked dicts (email, confidence, tier, verified, …).
    """
    _load_env()
    store.ensure_tables(conn)
    company = (company or "").strip()
    if not company:
        return []

    cfg = email_discovery_cfg(settings)
    enabled = set(cfg["sources"])
    min_autosend = cfg["min_confidence_to_autosend"]
    if max_verify is None:
        max_verify = cfg["max_verify_per_company"]
    if not cfg["smtp_verify"]:
        use_smtp = False

    print(f"\n▸ {company}")
    domain = domain or find_domain(company, job_url)
    if not domain:
        _warn(f"{company}: no domain resolved — cannot discover email")
        return []
    print(f"    domain → {domain}")

    # Domain must be able to receive mail at all before we spend anything else.
    mx = verify.check_mx(domain, conn=conn)
    if not mx:
        _warn(f"{domain} has no MX records — it cannot receive email at all")
        return []

    candidates: list[ContactCandidate] = []
    named: list[ContactCandidate] = []

    # 1 + 2. Free, keyless sources.
    if "site_crawl" in enabled:
        candidates += fs.crawl_site_for_emails(domain)
    if "github" in enabled:
        candidates += fs.github_emails(company, domain,
                                       token=os.environ.get("GITHUB_TOKEN") or None)

    # 3. Learn the naming convention from whatever we just found.
    template, ratio, samples = learn_domain_pattern(candidates)
    if template and samples:
        print(f"    pattern: {template} ({samples} sample(s), {ratio:.0%} agreement)")

    # Patterns that have already produced a hard bounce on this domain are not
    # allowed to be used again, however confidently we inferred them.
    burned = store.dead_patterns_for(conn, domain)
    if template and f"__pattern__{template}" in burned:
        print(f"    pattern: '{template}' previously bounced on {domain} — discarding it")
        template, ratio = None, 0.0

    # 4. LinkedIn names → targeted guesses using the learned pattern.
    if use_linkedin and "linkedin_names" in enabled:
        named += fs.linkedin_recruiter_names(company, settings=settings)
    for person in named:
        parts = (person.name or "").split()
        if len(parts) < 2:
            continue
        first, last = parts[0], parts[-1]
        guessed: list[ContactCandidate] = []
        if template and ratio >= 0.5:
            email = apply_pattern(template, domain, first, last)
            if email:
                guessed.append(ContactCandidate(
                    email=email, name=person.name, title=person.title,
                    linkedin_url=person.linkedin_url, source="pattern",
                    confidence=60,   # learned pattern, still only a guess until probed
                    notes=[f"learned pattern '{template}' from {samples} known address(es)"]))
        else:
            # No usable learned convention → blind permutations in frequency
            # order, skipping any convention that has already bounced here.
            for email, tmpl in blind_pattern_candidates(domain, first, last, burned):
                guessed.append(ContactCandidate(
                    email=email, name=person.name, title=person.title,
                    linkedin_url=person.linkedin_url, source="pattern",
                    confidence=40,   # blind guess — much weaker than a learned one
                    notes=[f"blind pattern guess '{tmpl}' (no known address to learn from)"]))
        candidates += guessed

    # 5. Keyed sources — last, and only if free discovery found nothing usable.
    if "cse" in enabled and not any(c.email for c in candidates):
        key, cx = os.environ.get("GOOGLE_CSE_KEY"), os.environ.get("GOOGLE_CSE_CX")
        candidates += fs.google_cse_search(company, domain, key, cx)
    if ("hunter" in enabled and not any(c.email for c in candidates)
            and os.environ.get("HUNTER_API_KEY")):
        print("    falling back to Hunter (paid-tier quota: 25/month)")
        candidates += hunter_domain_search(domain)

    # 6. Role addresses as a floor. Cheap, and on a strict domain the probe
    #    settles whether they exist (careers@sarvam.ai does; careers@figma.com doesn't).
    have = {c.email.lower() for c in candidates if c.email}
    for local, conf in GENERIC_LOCALPARTS:
        addr = f"{local}@{domain}"
        if addr not in have:
            candidates.append(ContactCandidate(
                email=addr, title="Recruiting (role address)", source="pattern",
                confidence=conf, notes=["role-address guess"]))

    # Drop anything we have already watched bounce.
    dead = store.bounced_emails(conn)
    dead_locals = burned
    filtered = []
    for c in candidates:
        if not c.email:
            continue
        local = c.email.split("@", 1)[0].lower()
        if c.email.lower() in dead:
            print(f"    skip {c.email} — previously bounced")
            continue
        if local in dead_locals:
            print(f"    skip {c.email} — pattern previously bounced on this domain")
            continue
        filtered.append(c)

    ranked = _dedupe(filtered)

    # Verify. The per-domain catch-all probe happens once and is then cached,
    # so this is one extra RCPT per address, not one full handshake per address.
    print(f"    verifying {min(len(ranked), max_verify)} of {len(ranked)} candidate(s)…")
    for c in ranked[:max_verify]:
        vr = verify.verify_email(c.email, conn=conn, use_smtp=use_smtp)
        score_candidate(c, vr)
    for c in ranked[max_verify:]:
        c.confidence = min(c.confidence, CONF_REVIEW)
        c.notes.append("not verified (past this run's verification budget)")

    final = sorted((c for c in ranked if c.confidence >= CONF_FLOOR),
                   key=lambda c: c.confidence, reverse=True)

    out: list[dict] = []
    for c in final:
        add_contact(conn, company=company, email=c.email, name=c.name, title=c.title,
                    linkedin_url=c.linkedin_url, source=c.source,
                    confidence=c.confidence, verified=c.verified)
        row = {"confidence": c.confidence, "verified": c.verified}
        out.append({
            "email": c.email, "name": c.name, "title": c.title,
            "linkedin_url": c.linkedin_url, "source": c.source,
            "confidence": c.confidence, "verified": c.verified,
            "tier": tier_for(c.confidence, c.verified),
            "auto_sendable": can_auto_send(row, min_confidence=min_autosend),
            "notes": c.notes,
        })

    sendable = sum(1 for r in out if r["auto_sendable"])
    print(f"    → {len(out)} stored, {sendable} auto-sendable, "
          f"{len(out) - sendable} queued for review")
    return out


def best_contact_for(conn, company: str, require_auto_sendable: bool = True,
                     settings: dict | None = None) -> dict | None:
    """The single best address for a company, or None if nothing clears the bar."""
    store.ensure_tables(conn)
    min_autosend = email_discovery_cfg(settings)["min_confidence_to_autosend"]
    dead = store.bounced_emails(conn)
    rows = contacts_for_company(conn, company)
    best = None
    for r in rows:
        if not r["email"] or r["email"].lower() in dead:
            continue
        tier = tier_for(int(r["confidence"] or 0), bool(r["verified"]))
        if require_auto_sendable and not can_auto_send(r, min_confidence=min_autosend):
            continue
        cand = {"email": r["email"], "name": r["name"], "title": r["title"],
                "confidence": int(r["confidence"] or 0), "verified": bool(r["verified"]),
                "source": r["source"], "tier": tier}
        if best is None or cand["confidence"] > best["confidence"]:
            best = cand
    return best


def review_queue(conn, company: str | None = None,
                 settings: dict | None = None) -> list[dict]:
    """Everything plausible that a human must approve before it can be used.

    Anything not auto-sendable but above the storage floor lands here — including
    contacts that would be sendable at a looser threshold, so raising
    `min_confidence_to_autosend` moves addresses into review rather than losing them.
    """
    store.ensure_tables(conn)
    min_autosend = email_discovery_cfg(settings)["min_confidence_to_autosend"]
    dead = store.bounced_emails(conn)
    sql = "SELECT * FROM contacts WHERE email IS NOT NULL"
    params: list = []
    if company:
        sql += " AND company = ?"
        params.append(company)
    rows = conn.execute(sql + " ORDER BY confidence DESC", params).fetchall()
    out = []
    for r in rows:
        conf, ver = int(r["confidence"] or 0), bool(r["verified"])
        if (r["email"] or "").lower() in dead:
            continue
        if can_auto_send(r, min_confidence=min_autosend):
            continue
        if tier_for(conf, ver) == TIER_REJECT:
            continue
        out.append({"id": r["id"], "company": r["company"], "email": r["email"],
                    "name": r["name"], "title": r["title"], "source": r["source"],
                    "confidence": conf, "verified": ver, "tier": tier_for(conf, ver)})
    return out
