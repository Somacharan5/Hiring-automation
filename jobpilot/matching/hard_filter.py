"""Cheap, code-only pre-filter. Kills obvious non-fits before any LLM spend.

Retargeted 2026-07-27 for a 2027-grad chasing fresher / internship→PPO / new-grad
Product roles: internships are now allowed (only seniority is rejected), and roles
that hard-require existing local work authorization are dropped for free instead
of burning an LLM call to reach the same verdict.
"""

from __future__ import annotations

import re

_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I)

# Phrases that signal a *mandatory* existing right to work with no sponsorship.
# Kept deliberately strict — a role merely mentioning "visa" is not a blocker;
# it must read as a hard requirement the candidate cannot meet.
_WORK_AUTH_RE = re.compile(
    r"(no\s+(?:visa\s+)?sponsorship)"
    r"|(sponsorship\s+is\s+not\s+(?:available|provided|offered))"
    r"|(not\s+able\s+to\s+sponsor)"
    r"|(unable\s+to\s+sponsor)"
    r"|(we\s+(?:do\s+not|don't|cannot|can't)\s+sponsor)"
    r"|(must\s+(?:already\s+)?(?:have|possess)\s+(?:the\s+)?(?:legal\s+)?"
    r"(?:right|authorization|authorisation)\s+to\s+work)"
    r"|(must\s+be\s+(?:legally\s+)?authori[sz]ed\s+to\s+work)"
    r"|(without\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+)?sponsorship)"
    r"|(requires?\s+(?:an?\s+)?(?:active\s+)?security\s+clearance)",
    re.I,
)

# Programs that inherently sponsor / relocate freshers — never let a stray
# "sponsorship" mention in these kill an otherwise perfect grad-program fit.
_SPONSOR_FRIENDLY_RE = re.compile(
    r"(graduate\s+program)|(new\s+grad)|(rotational\s+program)|(will\s+sponsor)"
    r"|(sponsorship\s+(?:available|provided|offered))|(relocation\s+(?:support|provided|assistance))",
    re.I,
)


def requires_local_work_auth(description: str | None) -> bool:
    """True when the JD makes existing local work authorization a hard requirement.

    Grad-program / sponsorship-friendly language wins the tie: those roles are
    exactly the ones a 2027 grad should be applying to, so a boilerplate legal
    line never disqualifies them.
    """
    if not description:
        return False
    if _SPONSOR_FRIENDLY_RE.search(description):
        return False
    return bool(_WORK_AUTH_RE.search(description))


def check(title: str, description: str | None, cfg: dict) -> str | None:
    """Return a rejection reason, or None if the job passes.

    `cfg` is the `hard_filter` block of settings.yaml. If it carries
    `skip_hard_work_auth: true`, roles demanding existing work authorization are
    rejected here (the pipeline mirrors `matching.skip_hard_work_auth` into it).
    """
    t = title.lower()

    if not any(k in t for k in cfg.get("title_must_contain_any", [])):
        return "title lacks required keyword"

    for bad in cfg.get("title_reject_any", []):
        if bad in t:
            return f"title contains '{bad.strip()}'"

    max_years = cfg.get("max_years_required")
    if max_years and description:
        # Look at the lowest years figure demanded in the JD (best-effort)
        years = [int(m) for m in _YEARS_RE.findall(description) if int(m) <= 30]
        if years and min(years) > max_years:
            return f"requires {min(years)}+ years (cap {max_years})"

    if cfg.get("skip_hard_work_auth") and requires_local_work_auth(description):
        return "requires existing local work authorization (no sponsorship)"

    return None
