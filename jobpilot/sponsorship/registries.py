"""Government visa-sponsor registries — company-name lookups.

Loads registry CSVs from `data/registries/` (drop the official gov files there;
they're large, so they're gitignored). Each becomes a normalized company-name list
the classifier fuzzy-matches against. Works with zero CSVs present — the classifier
then falls back to the JD-keyword and Gulf-automatic signals.

Official sources (download once, refresh ~weekly):
  UK  — gov.uk "Register of licensed sponsors: workers" (CSV)
  US  — USCIS H-1B Employer Data Hub (CSV per FY)
  CA  — open.canada.ca positive LMIA employers (CSV)
  AU  — Home Affairs approved sponsor list
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "registries"

# filename in data/registries/ → (registry_key, candidate company-name column headers)
REGISTRY_FILES: dict[str, tuple[str, list[str]]] = {
    "uk_sponsors.csv": ("UK_sponsor_register",
                        ["Organisation Name", "Organisation name", "Company", "company"]),
    "us_h1b.csv":      ("US_H1B",
                        ["Employer", "Employer (Petitioner) Name", "employer_name", "EMPLOYER_NAME"]),
    "ca_lmia.csv":     ("CA_LMIA", ["Employer", "Employer name", "Business Operating Name"]),
    "au_sponsors.csv": ("AU_sponsor", ["Business Name", "Legal Name", "Company", "company"]),
}

_SUFFIX = re.compile(
    r"\b(inc|ltd|llc|llp|limited|pvt|private|corp|corporation|co|gmbh|ag|plc|bv|nv|sarl|srl"
    r"|technologies|technology|labs|solutions|services|systems|group|holdings|global|international)\b",
    re.I)


def norm_company(name: str | None) -> str:
    """Normalise a company name for matching: drop legal suffixes + punctuation, lowercase."""
    s = _SUFFIX.sub(" ", (name or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


@lru_cache(maxsize=1)
def load() -> dict[str, list[str]]:
    """Registry key → sorted list of normalised sponsor company names. Cached in memory."""
    out: dict[str, list[str]] = {}
    if not DATA_DIR.exists():
        return out
    for fname, (key, cols) in REGISTRY_FILES.items():
        path = DATA_DIR / fname
        if not path.exists():
            continue
        names: set[str] = set()
        try:
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                fields = reader.fieldnames or []
                col = next((c for c in cols if c in fields), fields[0] if fields else None)
                if not col:
                    continue
                for row in reader:
                    n = norm_company(row.get(col, ""))
                    if len(n) >= 3:
                        names.add(n)
        except Exception:  # noqa: BLE001 — a malformed CSV must not break scoring
            continue
        if names:
            out[key] = sorted(names)
    return out


def available() -> dict[str, int]:
    """Which registries are loaded, and how many companies each holds."""
    return {k: len(v) for k, v in load().items()}
