"""Resume tailoring engine — the brain of Phase 2.

Given a job row and the master profile, ask the LLM to re-order, re-emphasise
and re-word the candidate's *existing* achievements for that specific posting,
then prove in code that nothing was invented.

THE HARD RULE
-------------
The tailor may:  reorder roles and bullets, rewrite wording, surface skills that
                 already exist somewhere in the master profile, and omit things.
The tailor may NOT: invent an employer, a job title, a date, a metric, a
                 technology or a credential the candidate does not have.

`_validate_no_fabrication()` enforces this after every generation:
  * unknown company or title  → FabricationError (hard stop)
  * wrong dates/location      → flagged, then force-corrected from the profile
  * a number in a bullet that
    isn't in that role's
    original bullets          → flagged, then the bullet is reverted verbatim
  * a skill not present
    anywhere in the profile   → flagged and dropped

A fabricated resume gets the user blacklisted, so every repair path degrades
towards the master profile's own wording rather than towards the LLM's.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..db import add_tailored_resume, get_job
from ..llm import structured_call
from ..profile import Profile, load_profile_for_matching, load_profile_yaml
from . import ats_check
from .render import render_docx, render_pdf

ROOT = Path(__file__).resolve().parent.parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"

MAX_JD_CHARS = 14_000


class FabricationError(RuntimeError):
    """Raised when the tailored resume claims something the profile doesn't."""


# ── Output schema -----------------------------------------------------

class TailoredExperience(BaseModel):
    company: str = Field(description="EXACT company name copied from the master profile")
    title: str = Field(description="EXACT job title copied from the master profile")
    location: str | None = Field(default=None, description="Copied from the master profile")
    start: str | None = Field(default=None, description="Copied from the master profile, verbatim")
    end: str | None = Field(default=None, description="Copied from the master profile, verbatim")
    bullets: list[str] = Field(
        description="Achievement bullets rewritten for this job's keywords, most relevant first. "
                    "Every fact and every number must come from that role's original bullets.")


class TailoredResume(BaseModel):
    summary: str = Field(description="2-3 line professional summary targeted at this exact role")
    selected_skills: list[str] = Field(
        description="Skills from the master profile, filtered and reordered by relevance to this JD")
    experiences: list[TailoredExperience] = Field(
        description="Roles from the master profile, most relevant to this JD first")
    keywords_targeted: list[str] = Field(
        description="Job-description keywords deliberately incorporated into the resume")


@dataclass
class TailorResult:
    """Everything one tailoring run produced."""
    job_id: str
    company: str
    title: str
    tailored: TailoredResume
    pdf_path: str | None = None
    docx_path: str | None = None
    ats_score: int = 0
    keywords_matched: list[str] = field(default_factory=list)
    keywords_missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages: int | None = None
    pdf_extractable: bool | None = None
    resume_id: int | None = None

    @property
    def meets_threshold(self) -> bool:
        return self.ats_score >= self._min_score

    _min_score: int = 75


# ── Prompt ------------------------------------------------------------

SYSTEM = """You are a meticulous resume tailor working for ONE candidate. You are given
that candidate's MASTER PROFILE and a single job description. You produce a tailored
version of their resume for that job.

WHAT YOU MAY DO
- Reorder roles so the most relevant to this job comes first.
- Reorder, merge, split, drop and REWRITE bullets so the candidate's real achievements
  are phrased in this job description's vocabulary.
- Choose which of the candidate's existing skills to surface, and in what order.
- Write a fresh 2-3 line summary aimed squarely at this role.

WHAT YOU MUST NEVER DO — this is absolute
- Never invent an employer, job title, date, location, degree or certification.
  Copy company, title, location, start and end VERBATIM from the master profile.
- Never invent a number. Every metric (%, revenue, users, reach, headcount, time saved)
  in a bullet must already appear in THAT SAME ROLE's original bullets. If the original
  says "reducing manual work by 20%", you may say "cut manual effort 20%" — you may NOT
  say 25%, and you may NOT add a metric to a bullet that had none.
- Never claim a technology, tool, framework or domain the master profile does not
  mention. If the job wants Kubernetes and the candidate has never used it, leave it out.
  A missing keyword is fine; a false one ends the candidate's application.
- Never change what a role actually was. Do not upgrade "Business Trainer" into
  "Product Manager".

STYLE
- Bullets: start with a strong past-tense verb, one achievement each, ~10-24 words,
  quantified only where the original was quantified. No pronouns, no filler.
- Mirror the job description's exact terminology where the candidate genuinely matches it
  (e.g. if the JD says "experimentation" and the profile says "A/B tests", use both).
- Include every role from the master profile unless it is clearly irrelevant; keep 2-4
  bullets for the most relevant roles and 1-2 for older ones.
- selected_skills: 12-20 items, most job-relevant first, each traceable to the profile.
"""

USER_TEMPLATE = """== CANDIDATE MASTER PROFILE (the ONLY facts you may use) ==
{profile}

== TARGET JOB ==
Company: {company}
Title: {title}
Location: {location}

Job description:
{jd}
{hints}
Produce the tailored resume now. Company names, titles and dates must be byte-identical
to the master profile."""


# ── Normalisation helpers --------------------------------------------

def _norm(text: str | None) -> str:
    """Casefold + unify quotes/dashes/whitespace, for comparing identity strings."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"[^\w\s'&/+-]", " ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


_METRIC_RE = re.compile(
    r"(?<![\w.])(\d[\d,\s]*(?:\.\d+)?)\s*"
    r"(%|percent|k\b|m\b|mn\b|b\b|bn\b|cr\b|crore|l\b|lakh|lac|x\b)?",
    re.IGNORECASE,
)

_UNIT_ALIASES = {
    "percent": "%", "mn": "m", "bn": "b", "crore": "cr", "lakh": "l", "lac": "l",
}


def _metrics(text: str | None) -> set[str]:
    """Canonical numeric claims in a piece of text.

    '₹ 1  Cr+' → '1cr',  '2,000+' → '2000',  'by 20%' → '20%',  '16M+' → '16m'.
    Digits glued to letters (B2B, n8n, GPT-4) are not metrics and are skipped by
    the lookbehind.
    """
    found: set[str] = set()
    for raw_num, raw_unit in _METRIC_RE.findall(text or ""):
        num = re.sub(r"[,\s]", "", raw_num).rstrip(".")
        if not num:
            continue
        num = num.rstrip("0").rstrip(".") if "." in num else num
        unit = (raw_unit or "").strip().lower()
        unit = _UNIT_ALIASES.get(unit, unit)
        found.add(f"{num}{unit}")
    return found


def _profile_corpus(profile: Profile) -> str:
    """Every word the candidate can legitimately claim, as one lowercase blob."""
    parts = [profile.headline or "", profile.summary or "", " ".join(profile.skills or []),
             " ".join(profile.certifications or []), " ".join(profile.achievements or [])]
    for exp in profile.experiences:
        parts += [exp.company, exp.title, exp.location or "", " ".join(exp.bullets or [])]
    for edu in profile.education:
        parts += [edu.institution, edu.degree or "", edu.field or ""]
    return _norm(" ".join(parts))


_SKILL_STOP = {"and", "the", "for", "with", "via", "using", "based", "etc"}


_STEM_PREFIX = 5


def _corpus_stems(corpus: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", corpus) if t}


def _tok_supported(tok: str, corpus_toks: set[str]) -> bool:
    """Does the profile evidence this word, allowing morphological variants?

    Suffix stemming is unreliable here ('optimization'→'optim' but
    'optimized'→'optimiz'), so compare on a shared leading prefix instead:
    analysis≈analytics, acquisition≈acquiring, optimization≈optimized.
    Short tokens (SQL, Java, Rust) require an exact match so that a genuinely
    absent technology can never slip through on a prefix collision.
    """
    if tok in corpus_toks:
        return True
    if len(tok) < _STEM_PREFIX:
        # Short tokens (rate, ads, api) — only a plural/singular variant counts,
        # so a 4-letter word can't prefix-match its way into an unrelated term.
        variants = {tok + "s", tok + "es"}
        if tok.endswith("s"):
            variants.add(tok[:-1])
        return bool(variants & corpus_toks)
    # A shared 5-char prefix AND a similar length means a morphological variant
    # (optimization≈optimized, analysis≈analytics). The length guard is what
    # stops 'salesforce' from matching 'sales' — a different word entirely.
    pre = tok[:_STEM_PREFIX]
    return any(c.startswith(pre) and abs(len(c) - len(tok)) <= 3 for c in corpus_toks)


def _skill_supported(skill: str, corpus: str, corpus_stems: set[str]) -> bool:
    """Is this skill genuinely evidenced anywhere in the master profile?

    Exact-phrase matching is too strict to be correct: the profile says
    "A/B tests on formats", so claiming the skill "A/B Testing" is honest even
    though that literal string never appears. Requiring every *significant*
    word of the skill to be present (stemmed) keeps invented skills out — "SQL"
    has no supporting token and is still rejected — while letting real,
    evidenced ones through.
    """
    norm = _norm(skill)
    if not norm:
        return False
    if norm in corpus:                      # fast path: literal match
        return True
    toks = [t for t in re.split(r"[^a-z0-9]+", norm)
            if len(t) > 2 and t not in _SKILL_STOP]
    if not toks:
        return False
    return all(_tok_supported(t, corpus_stems) for t in toks)


# ── The hard rule -----------------------------------------------------

def _validate_no_fabrication(tailored: TailoredResume, profile: Profile) -> list[str]:
    """Enforce the no-fabrication rule.

    RAISES `FabricationError` if a company or a job title appears that is not in
    the master profile (identity fabrication — unrecoverable).

    RETURNS a list of soft flags for everything that is fixable: wrong dates or
    location, numeric metrics in a tailored bullet that don't appear in that
    role's original bullets, metrics in the summary that appear nowhere in the
    profile, and skills the profile never mentions. The caller repairs these.
    """
    flags: list[str] = []

    by_company: dict[str, list] = {}
    for exp in profile.experiences:
        by_company.setdefault(_norm(exp.company), []).append(exp)

    hard: list[str] = []
    for exp in tailored.experiences:
        key = _norm(exp.company)
        if key not in by_company:
            hard.append(f"unknown employer {exp.company!r} — not in the master profile")
            continue
        titles = {_norm(o.title): o for o in by_company[key]}
        if _norm(exp.title) not in titles:
            hard.append(
                f"invented title {exp.title!r} at {exp.company!r} — profile has "
                f"{[o.title for o in by_company[key]]}"
            )
            continue

        original = titles[_norm(exp.title)]
        if _norm(exp.start) != _norm(original.start) or _norm(exp.end) != _norm(original.end):
            flags.append(
                f"dates for {exp.company}/{exp.title} were altered "
                f"({exp.start}–{exp.end} vs {original.start}–{original.end}) — reverted"
            )
        if exp.location and _norm(exp.location) != _norm(original.location):
            flags.append(f"location for {exp.company} was altered ({exp.location}) — reverted")

        allowed = set()
        for bullet in original.bullets or []:
            allowed |= _metrics(bullet)
        for bullet in exp.bullets or []:
            invented = _metrics(bullet) - allowed
            if invented:
                flags.append(
                    f"fabricated metric(s) {sorted(invented)} in {exp.company} bullet: {bullet!r}"
                )

    if hard:
        raise FabricationError("; ".join(hard))

    all_metrics: set[str] = set()
    for exp in profile.experiences:
        for bullet in exp.bullets or []:
            all_metrics |= _metrics(bullet)
    for extra in (profile.achievements or []) + ([profile.summary] if profile.summary else []):
        all_metrics |= _metrics(extra)
    invented_summary = _metrics(tailored.summary) - all_metrics - _metrics(
        f"{profile.years_experience or ''}")
    if invented_summary:
        flags.append(f"fabricated metric(s) {sorted(invented_summary)} in the summary")

    corpus = _profile_corpus(profile)
    stems = _corpus_stems(corpus)
    for skill in tailored.selected_skills:
        if _norm(skill) and not _skill_supported(skill, corpus, stems):
            flags.append(f"skill {skill!r} does not appear anywhere in the master profile — dropped")

    return flags


def _repair(tailored: TailoredResume, profile: Profile) -> tuple[TailoredResume, list[str]]:
    """Deterministically force the tailored resume back onto profile facts.

    Identity fields are overwritten with the profile's exact strings, bullets
    with invented numbers are reverted to their closest original wording, and
    unsupported skills are dropped. Always safe to run; idempotent.
    """
    notes: list[str] = []
    originals = {(_norm(e.company), _norm(e.title)): e for e in profile.experiences}

    fixed_exps: list[TailoredExperience] = []
    for exp in tailored.experiences:
        original = originals.get((_norm(exp.company), _norm(exp.title)))
        if original is None:
            notes.append(f"dropped unverifiable role {exp.company}/{exp.title}")
            continue

        allowed: set[str] = set()
        for bullet in original.bullets or []:
            allowed |= _metrics(bullet)

        used: set[int] = set()
        clean_bullets: list[str] = []
        for bullet in exp.bullets or []:
            if _metrics(bullet) - allowed:
                idx = _closest_original(bullet, original.bullets or [], used)
                if idx is None:
                    notes.append(f"dropped un-substantiated bullet at {original.company}: {bullet!r}")
                    continue
                used.add(idx)
                clean_bullets.append(original.bullets[idx])
                notes.append(
                    f"reverted a bullet at {original.company} to its original wording "
                    f"(invented metric)"
                )
            else:
                clean_bullets.append(bullet.strip())

        fixed_exps.append(TailoredExperience(
            company=original.company,          # verbatim from the profile
            title=original.title,
            location=original.location,
            start=original.start,
            end=original.end,
            bullets=[b for b in clean_bullets if b],
        ))

    corpus = _profile_corpus(profile)
    skills, seen = [], set()
    for skill in tailored.selected_skills:
        key = _norm(skill)
        if not key or key in seen:
            continue
        if key not in corpus:
            continue
        seen.add(key)
        skills.append(skill.strip())

    all_metrics: set[str] = set()
    for exp in profile.experiences:
        for bullet in exp.bullets or []:
            all_metrics |= _metrics(bullet)
    for extra in (profile.achievements or []) + ([profile.summary] if profile.summary else []):
        all_metrics |= _metrics(extra)
    all_metrics |= _metrics(f"{profile.years_experience or ''}")

    summary_parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", tailored.summary or "") if s.strip()]
    kept = [s for s in summary_parts if not (_metrics(s) - all_metrics)]
    if len(kept) != len(summary_parts):
        notes.append("removed a summary sentence containing an unverifiable number")
    summary = " ".join(kept) or (profile.summary or profile.headline or "")

    return TailoredResume(
        summary=summary,
        selected_skills=skills,
        experiences=fixed_exps,
        keywords_targeted=tailored.keywords_targeted,
    ), notes


def _closest_original(bullet: str, originals: list[str], used: set[int]) -> int | None:
    """Index of the unused original bullet the tailored one was most likely derived from."""
    best, best_ratio = None, -1.0
    for i, cand in enumerate(originals):
        if i in used:
            continue
        ratio = difflib.SequenceMatcher(None, _norm(bullet), _norm(cand)).ratio()
        if ratio > best_ratio:
            best, best_ratio = i, ratio
    return best


# ── Loading -----------------------------------------------------------

def load_settings(path: Path = SETTINGS_PATH) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def load_profile() -> Profile:
    return Profile.model_validate(yaml.safe_load(load_profile_yaml()) or {})


def _hints_block(match_json: str | None) -> str:
    """Tailoring hints from the match verdict, if scoring has run. Optional."""
    if not match_json:
        return ""
    try:
        verdict = json.loads(match_json)
    except (ValueError, TypeError):
        return ""
    lines = []
    if verdict.get("tailoring_hints"):
        lines.append("Angles to emphasise (from the match analysis):")
        lines += [f"- {h}" for h in verdict["tailoring_hints"]]
    if verdict.get("matched_strengths"):
        lines.append("Confirmed strengths to lead with:")
        lines += [f"- {s}" for s in verdict["matched_strengths"]]
    if verdict.get("gaps"):
        lines.append("Known gaps — do NOT paper over these with invented experience:")
        lines += [f"- {g}" for g in verdict["gaps"]]
    return "\n== MATCH ANALYSIS ==\n" + "\n".join(lines) + "\n" if lines else ""


# ── Public API --------------------------------------------------------

def tailor_for_job(conn: sqlite3.Connection, job_id: str, settings: dict | None = None,
                   warnings: list[str] | None = None) -> TailoredResume:
    """Tailor the master profile for one job. Returns a validated TailoredResume.

    `settings` is the parsed config/settings.yaml (loaded from disk if omitted).
    Pass a list as `warnings` to receive the non-fatal repair notes.
    Handles `match_json` being NULL — falls back to the job description alone.
    Raises FabricationError if the model invents an employer or a title twice.
    """
    settings = settings if settings is not None else load_settings()
    cfg = (settings.get("tailoring") or {})
    warnings = warnings if warnings is not None else []

    row = get_job(conn, job_id)
    if row is None:
        raise ValueError(f"No job with id {job_id!r}")

    profile = load_profile()
    jd = (row["description"] or "").strip()
    if len(jd) > MAX_JD_CHARS:
        jd = jd[:MAX_JD_CHARS] + "\n[...truncated]"
    if not jd:
        warnings.append("job has no description — tailoring from title/company only")

    # Close the loop: tell the model which terms the ATS actually ranks this req
    # on, instead of letting it guess and only scoring it afterwards.
    ats_terms = [k for k, _ in ats_check.extract_keywords(
        jd, row["title"], row["company"], row["location"] or "")][:20]

    user = USER_TEMPLATE.format(
        profile=load_profile_for_matching(),      # PII stripped before it leaves the machine
        company=row["company"], title=row["title"],
        location=row["location"] or "unspecified",
        jd=jd or "(no description available — tailor conservatively from the title)",
        hints=_hints_block(row["match_json"]),
    )
    if ats_terms:
        user += (
            "\n\nATS KEYWORDS FOR THIS REQ (ranked):\n"
            + ", ".join(ats_terms)
            + "\n\nWork as many of these as you honestly can into the summary, skills, and "
              "bullets — but ONLY where the candidate's real experience already supports the "
              "term. Prefer the job's exact wording when describing something they genuinely "
              "did (e.g. write 'A/B testing' if they ran A/B tests). Never claim a keyword "
              "the profile cannot back up; leaving one out is always better than inventing it."
        )
    model = cfg.get("model") or (settings.get("matching") or {}).get("model")

    system = SYSTEM
    last_error: str | None = None
    tailored: TailoredResume | None = None
    for attempt in range(2):
        candidate = structured_call(
            system if not last_error else f"{system}\n\nYour previous attempt was REJECTED:\n"
                                          f"{last_error}\nFix it. Copy facts verbatim.",
            user, TailoredResume, model=model, max_tokens=12_000)
        try:
            flags = _validate_no_fabrication(candidate, profile)
        except FabricationError as exc:
            last_error = str(exc)
            if attempt == 1:
                raise FabricationError(
                    f"model fabricated employment history twice for job {job_id}: {exc}") from exc
            continue

        if flags and not cfg.get("allow_fabrication", False) and attempt == 0:
            last_error = "; ".join(flags)      # one shot at fixing it properly
            tailored = candidate
            continue
        tailored = candidate
        warnings.extend(flags)
        break

    assert tailored is not None
    tailored, notes = _repair(tailored, profile)
    warnings.extend(notes)

    # The repaired document must now be clean; anything left is a bug, not a warning.
    residual = _validate_no_fabrication(tailored, profile)
    residual = [r for r in residual if "does not appear anywhere" not in r]
    if residual:
        raise FabricationError(f"unrepairable fabrication for job {job_id}: {residual}")
    return tailored


def tailor_and_render(conn: sqlite3.Connection, job_id: str, settings: dict | None = None,
                      save_to_db: bool = True) -> TailorResult:
    """Full Phase-2 pipeline for one job: tailor → render → ATS score → record.

    Honours settings['tailoring']: output_dir, format (pdf|docx|both), max_pages,
    min_ats_score. Returns a TailorResult with paths, score and warnings.
    """
    settings = settings if settings is not None else load_settings()
    cfg = (settings.get("tailoring") or {})
    fmt = (cfg.get("format") or "pdf").lower()
    max_pages = int(cfg.get("max_pages") or 1)
    out_dir = ROOT / (cfg.get("output_dir") or "output/resumes")
    out_dir.mkdir(parents=True, exist_ok=True)

    row = get_job(conn, job_id)
    if row is None:
        raise ValueError(f"No job with id {job_id!r}")

    warnings: list[str] = []
    tailored = tailor_for_job(conn, job_id, settings, warnings)
    profile = load_profile()

    stem = f"{_slug(profile.name)}_{_slug(row['company'])}_{_slug(row['title'])}_{job_id[:8]}"
    result = TailorResult(job_id=job_id, company=row["company"], title=row["title"],
                          tailored=tailored, warnings=warnings)
    result._min_score = int(cfg.get("min_ats_score") or 75)

    if fmt in ("pdf", "both"):
        result.pdf_path = render_pdf(tailored, profile, out_dir / f"{stem}.pdf",
                                     max_pages=max_pages)
        result.pdf_extractable = ats_check.verify_pdf_extractable(result.pdf_path)
        result.pages = ats_check.pdf_page_count(result.pdf_path)
        if not result.pdf_extractable:
            warnings.append("rendered PDF is not text-extractable — real ATS would score it zero")
        if result.pages > max_pages:
            warnings.append(f"PDF is {result.pages} pages (max_pages={max_pages})")
    if fmt in ("docx", "both"):
        result.docx_path = render_docx(tailored, profile, out_dir / f"{stem}.docx",
                                       max_pages=max_pages)

    score, matched, missing = score_resume(tailored, row["description"] or "",
                                           row["title"], row["company"], row["location"] or "")
    result.ats_score, result.keywords_matched, result.keywords_missing = score, matched, missing
    if score < result._min_score:
        warnings.append(f"ATS score {score} is below min_ats_score {result._min_score}")

    if save_to_db:
        result.resume_id = add_tailored_resume(
            conn, job_id, result.pdf_path, result.docx_path, score, matched, missing)
    return result


def score_resume(tailored: TailoredResume, job_description: str, job_title: str = "",
                 company: str = "", location: str = "") -> tuple[int, list[str], list[str]]:
    """Thin re-export so callers don't need to import ats_check directly."""
    return ats_check.score_ats(tailored, job_description, job_title, company, location)


def _slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^\w]+", "-", unicodedata.normalize("NFKD", text or "")).strip("-").lower()
    return (slug[:limit].strip("-") or "untitled")
