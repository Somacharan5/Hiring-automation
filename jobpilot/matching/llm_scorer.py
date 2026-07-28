"""LLM match scorer — Qwen judges two-way fit between the master profile and a JD."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..llm import structured_call

SYSTEM_TEMPLATE = """You are a rigorous recruiting-fit evaluator working for a job seeker.
You will be shown one job posting at a time. Judge the two-way fit between the
candidate's master profile below and the posting:
1. Is the candidate credibly qualified (would a recruiter shortlist them)?
2. Is the role right for the candidate (level, domain, trajectory)?

Scoring guide:
- 85-100: near-perfect fit — apply immediately, strong story on both sides
- 70-84:  solid fit — worth applying with a tailored resume
- 50-69:  stretch — missing some stated requirements but a story exists
- 0-49:   poor fit — wrong level, wrong domain, or hard requirement missed

Be honest about gaps; never invent candidate experience. A hard requirement the
candidate clearly lacks (e.g. a mandatory language, degree, or visa status)
caps the score below 50.

== CANDIDATE MASTER PROFILE ==
{profile}"""


class MatchVerdict(BaseModel):
    score: int = Field(description="Overall two-way fit score, 0-100")
    verdict: Literal["strong_match", "solid_match", "stretch", "no_match"]
    matched_strengths: list[str] = Field(
        description="Candidate strengths that directly map to stated requirements")
    gaps: list[str] = Field(
        description="Stated requirements the candidate does not clearly meet")
    hard_blockers: list[str] = Field(
        description="Mandatory requirements clearly missed (visa, language, degree, YoE). Empty if none.")
    tailoring_hints: list[str] = Field(
        description="Concrete angles to emphasize when tailoring the resume for this role")
    reasoning: str = Field(description="2-3 sentence justification of the score")


class Scorer:
    def __init__(self, profile_yaml: str, model: str | None = None):
        self.model = model
        self.system = SYSTEM_TEMPLATE.format(profile=profile_yaml)

    def score(self, company: str, title: str, location: str | None,
              description: str | None) -> MatchVerdict:
        jd = (description or "").strip()
        if len(jd) > 20_000:
            jd = jd[:20_000] + "\n[...truncated]"
        posting = (
            f"Company: {company}\nTitle: {title}\nLocation: {location or 'unspecified'}\n\n"
            f"Job description:\n{jd if jd else '(no description available — judge from title/company only, be conservative)'}"
        )
        return structured_call(self.system, posting, MatchVerdict, model=self.model)
