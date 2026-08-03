"""Tailor the master resume (kit schema) for one job — re-emphasis only.

Operates on the locked kit in Resume_creation/: the master resume.json is the
source of truth. The LLM returns DELTAS — a reworded tagline and reordered,
reworded bullets per role — which are merged into a copy of the master. Identity
fields (the heading with the company name/link, the role subheading, dates,
location) are always taken from the master, so they cannot be fabricated. Numbers
in a tailored bullet are verified against that role's master bullets; an
unverifiable one reverts to the closest original. Rendering goes through the kit
(build.mjs → template.css → Chromium), so the exact format is preserved.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from ..llm import structured_call
from . import kit

ROOT = Path(__file__).resolve().parent.parent.parent

# ── markup + metric helpers ──────────────────────────────────────────

_METRIC_RE = re.compile(
    r"(?<![\w.])(\d[\d,\s]*(?:\.\d+)?)\s*"
    r"(%|percent|k\b|m\b|mn\b|b\b|bn\b|cr\b|crore|l\b|lakh|lac|x\b)?", re.I)
_UNIT_ALIASES = {"percent": "%", "mn": "m", "bn": "b", "crore": "cr", "lakh": "l", "lac": "l"}
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def _strip_markup(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")   # [label](url) → label
    return text.replace("**", "").replace("_", "")


def _metrics(text: str | None) -> set[str]:
    found: set[str] = set()
    for raw_num, raw_unit in _METRIC_RE.findall(_strip_markup(text or "")):
        num = re.sub(r"[,\s]", "", raw_num).rstrip(".")
        if not num:
            continue
        num = num.rstrip("0").rstrip(".") if "." in num else num
        unit = _UNIT_ALIASES.get((raw_unit or "").strip().lower(), (raw_unit or "").strip().lower())
        found.add(f"{num}{unit}")
    return found


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("’", "'").replace("–", "-")).strip().casefold()


def _company_of(heading: str) -> str:
    """The company/institution name — the first bold run in a row heading."""
    m = _BOLD.search(heading or "")
    return _strip_markup(m.group(1) if m else (heading or "")).strip()


def _closest(bullet: str, originals: list[str], used: set[int]) -> int | None:
    best, best_ratio = None, -1.0
    for i, cand in enumerate(originals):
        if i in used:
            continue
        ratio = difflib.SequenceMatcher(None, _norm(_strip_markup(bullet)),
                                        _norm(_strip_markup(cand))).ratio()
        if ratio > best_ratio:
            best, best_ratio = i, ratio
    return best


# ── LLM output schema ────────────────────────────────────────────────

class RoleBullets(BaseModel):
    company: str = Field(description="EXACT company name copied from the master resume")
    bullets: list[str] = Field(
        description="This role's bullets, reworded for the job's keywords and reordered "
                    "most-relevant-first. Every fact and number must already appear in this "
                    "role's master bullets. Kit markup: **bold the achievement/metric**, "
                    "_italic_, [label](url).")


class TailoredDelta(BaseModel):
    tagline: str = Field(description="A one-line tagline targeted at this role. Real facts only; no new numbers.")
    experience: list[RoleBullets] = Field(description="Every role from the master, most relevant to this job first.")


SYSTEM = """You tailor an EXISTING resume for one job. You get the candidate's master resume
(as company → bullets) and a job description. You return only re-emphasis, never new facts.

YOU MAY: reorder roles so the most relevant to this job comes first; reorder, merge, reword
and drop bullets so real achievements are phrased in the job's vocabulary; write a fresh
one-line tagline aimed at this role.

YOU MUST NEVER: invent or alter a company, title, date or number — every metric in a bullet
must already appear in THAT role's master bullets (you may rephrase "reduced by 20%" but
never change it to 25% or add a % to a bullet that had none); claim a tool or domain the
master does not contain.

STYLE: lead each bullet with a strong verb; **bold the achievement or the metric**; keep
connective prose regular; use _italic_ sparingly. Keep 2-4 bullets on the most relevant roles.
Return every role, keyed by its exact company name."""


def _prompt(job: dict, job_rows: list[dict]) -> str:
    jd = (job.get("description") or "").strip()
    if len(jd) > 12_000:
        jd = jd[:12_000] + "\n[…truncated]"
    hints = ""
    mj = job.get("match_json")
    if isinstance(mj, dict):
        if mj.get("tailoring_hints"):
            hints += "\nAngles to emphasise: " + "; ".join(mj["tailoring_hints"][:4])
        if mj.get("gaps"):
            hints += "\nGaps — do NOT paper over with invented experience: " + "; ".join(mj["gaps"][:4])
    roles = "\n\n".join(
        f"Company: {_company_of(r['heading'])}\nRole: {_strip_markup(r.get('subheading',''))}\n"
        + "\n".join(f"- {b}" for b in r.get("bullets", []))
        for r in job_rows)
    return (f"== MASTER ROLES (the ONLY facts you may use) ==\n{roles}\n\n"
            f"== TARGET JOB ==\nCompany: {job['company']}\nTitle: {job['title']}\n"
            f"Location: {job.get('location') or 'unspecified'}\n\nJob description:\n"
            f"{jd or '(none — tailor conservatively from the title)'}{hints}\n\n"
            f"Return the tailored delta now, keyed by the exact company names above.")


def _work_section(data: dict) -> dict | None:
    return next((s for s in data.get("sections", [])
                 if (s.get("title") or "").strip().lower() == "work experience"), None)


def tailor_data(job: dict, master: dict | None = None, model: str | None = None) -> tuple[dict, list[str]]:
    """Return (tailored master-copy, warnings) for one job, without rendering."""
    master = master if master is not None else kit.load_master()
    out = copy.deepcopy(master)
    warnings: list[str] = []

    section = _work_section(out)
    if not section:
        return out, ["no Work Experience section found — resume unchanged"]

    rows = section.get("rows", [])
    job_rows = [r for r in rows if r.get("subheading")]
    other_rows = [r for r in rows if not r.get("subheading")]      # Skills labelled row, etc.
    if not job_rows:
        return out, ["no dated roles found — resume unchanged"]

    delta = structured_call(SYSTEM, _prompt(job, job_rows), TailoredDelta, model=model, max_tokens=6000)

    by_company = {_norm(_company_of(r["heading"])): r for r in job_rows}
    all_metrics: set[str] = set()
    for r in job_rows:
        for b in r.get("bullets", []):
            all_metrics |= _metrics(b)

    ordered: list[dict] = []
    seen: set[str] = set()
    for d in delta.experience:
        key = _norm(d.company)
        row = by_company.get(key)
        if row is None:                          # fuzzy fallback on company name
            cand = difflib.get_close_matches(key, list(by_company), n=1, cutoff=0.6)
            row = by_company.get(cand[0]) if cand else None
            key = cand[0] if cand else key
        if row is None or key in seen:
            continue
        seen.add(key)
        new_row = copy.deepcopy(row)
        allowed: set[str] = set()
        for b in row.get("bullets", []):
            allowed |= _metrics(b)
        clean, used = [], set()
        for b in (d.bullets or []):
            if _metrics(b) - allowed:
                idx = _closest(b, row.get("bullets", []), used)
                if idx is None:
                    warnings.append(f"dropped an unverifiable bullet at {_company_of(row['heading'])}")
                    continue
                used.add(idx)
                clean.append(row["bullets"][idx])
                warnings.append(f"reverted a bullet at {_company_of(row['heading'])} (invented number)")
            else:
                clean.append(b.strip())
        new_row["bullets"] = clean or row.get("bullets", [])
        ordered.append(new_row)

    for key, row in by_company.items():          # keep any role the LLM dropped
        if key not in seen:
            ordered.append(copy.deepcopy(row))

    section["rows"] = ordered + other_rows

    if _metrics(delta.tagline) - all_metrics:
        warnings.append("tailored tagline introduced an unverifiable number — kept master tagline")
    elif delta.tagline.strip():
        out.setdefault("header", {})["tagline"] = delta.tagline.strip()

    return out, warnings


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w]+", "-", (text or "")).strip("-").lower()
    return s[:limit].strip("-") or "role"


def _fname(text: str, limit: int = 50) -> str:
    """Filename-friendly but case-preserved (e.g. 'Product-Manager-Growth')."""
    s = re.sub(r"[^\w\s-]", "", (text or "")).strip()
    s = re.sub(r"[\s_]+", "-", s)
    return s[:limit].strip("-") or "x"


def tailor_and_render(conn, job_id: str, settings: dict | None = None,
                      out_dir: Path | None = None, model: str | None = None) -> dict:
    """Tailor + render a per-job PDF via the kit; record it. Returns a summary."""
    from ..db import add_tailored_resume, get_job

    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"No job with id {job_id!r}")
    model = model or ((settings or {}).get("matching") or {}).get("model")

    master = kit.load_master()
    tailored, warnings = tailor_data(dict(job), master, model=model)

    out_dir = out_dir or (ROOT / "output" / "resumes")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = master.get("header", {}).get("name", "resume")
    # {Company}_{Role}_{DateCreated}_{Name} — e.g. Sarvam_Product-Manager-Growth_2026-08-03_Soma-Charan
    stem = f"{_fname(job['company'])}_{_fname(job['title'])}_{datetime.now():%Y-%m-%d}_{_fname(name)}"
    pdf_path = out_dir / f"{stem}.pdf"
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(tailored, ensure_ascii=False, indent=2), encoding="utf-8")
    kit.render_pdf(tailored, pdf_path)

    resume_id = add_tailored_resume(conn, job_id, str(pdf_path), None, None, json_path=str(json_path))
    return {"resume_id": resume_id, "pdf_path": str(pdf_path), "json_path": str(json_path),
            "warnings": warnings}
