"""Visa-sponsorship classifier — the spec's "actual unlock".

For each job, returns (signal, registry_match):
  registry_confirmed — company is on a government sponsor register, OR the role is in
                       the Gulf (UAE/Qatar/… where employment sponsorship is automatic)
  jd_mentioned       — the JD explicitly offers sponsorship/relocation (weaker signal)
  likely_no          — the JD hard-requires existing local work authorization
  unknown            — no signal either way

Registry matching is fuzzy (rapidfuzz) against the cached gov CSVs; it degrades to the
JD-keyword + Gulf rules when no CSV is present.
"""

from __future__ import annotations

import re

from ..matching.hard_filter import requires_local_work_auth
from . import registries

_SPONSOR_POS = re.compile(
    r"visa sponsorship(?:\s+is)?(?:\s+available|\s+provided|\s+offered)?"
    r"|will sponsor|we sponsor|sponsorship (?:available|provided|offered)"
    r"|relocation (?:support|assistance|package|provided)"
    r"|work permit (?:assistance|support|provided)"
    r"|(?:eu )?blue card|tier 2|skilled worker visa|h-?1b sponsorship", re.I)

# Substrings that mean automatic employer sponsorship (Gulf employment contracts).
_GULF = ("united arab emirates", "uae", "dubai", "abu dhabi", "sharjah", "qatar", "doha",
         "saudi arabia", "riyadh", "jeddah", "bahrain", "manama", "kuwait", "oman", "muscat")

_FUZZY_CUTOFF = 92


def classify(company: str | None, description: str | None,
             location: str | None = None) -> tuple[str, str]:
    """Return (sponsorship_signal, sponsorship_registry_match) for one job."""
    loc = (location or "").lower()
    if any(g in loc for g in _GULF):
        return "registry_confirmed", "Gulf_automatic"

    reg = registries.load()
    if reg and company:
        norm = registries.norm_company(company)
        if len(norm) >= 3:
            from rapidfuzz import fuzz, process
            for key, names in reg.items():
                if process.extractOne(norm, names, scorer=fuzz.WRatio,
                                      score_cutoff=_FUZZY_CUTOFF):
                    return "registry_confirmed", key

    # Negative first: a "no sponsorship / must already have the right to work" line
    # dominates. requires_local_work_auth() already yields to genuine sponsor-friendly
    # language (relocation support, will sponsor, grad programs), so real offers survive.
    if requires_local_work_auth(description):
        return "likely_no", "none"
    if description and _SPONSOR_POS.search(description):
        return "jd_mentioned", "none"
    return "unknown", "none"
