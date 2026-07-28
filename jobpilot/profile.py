"""Master profile: parse the user's resume (PDF/docx) into structured YAML.

The profile at config/profile.yaml is the pipeline's core input — the matcher
scores against it and (Phase 2) the tailor selects/rephrases bullets from it.
Text is extracted locally (pypdf / python-docx), then structured by Qwen.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .llm import structured_call

PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "profile.yaml"


class Experience(BaseModel):
    company: str
    title: str
    location: str | None = None
    start: str | None = Field(default=None, description="e.g. '2022-06'")
    end: str | None = Field(default=None, description="e.g. '2024-01' or 'present'")
    bullets: list[str] = Field(description="Achievement bullets, verbatim from the resume")


class Education(BaseModel):
    institution: str
    degree: str | None = None
    field: str | None = None
    year: str | None = None


class Profile(BaseModel):
    name: str
    headline: str | None = Field(default=None, description="Professional headline/title")
    location: str | None = None
    country: str | None = Field(
        default=None, description="Country of residence — application forms ask for this directly")
    work_authorization: str | None = Field(
        default=None,
        description="Where the candidate may legally work without sponsorship, e.g. "
                    "'India (citizen); requires sponsorship for US/UK/EU'. Drives both "
                    "match scoring and application-form answers.")
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    portfolio: str | None = None
    years_experience: float | None = Field(
        default=None, description="Total professional years of experience, estimated from the work history")
    summary: str | None = None
    skills: list[str] = Field(description="All skills/tools/methodologies mentioned")
    experiences: list[Experience]
    education: list[Education]
    certifications: list[str] = []
    achievements: list[str] = Field(
        default=[], description="Awards, publications, notable metrics not tied to one role")


SYSTEM = (
    "You extract structured data from resumes. Copy bullets verbatim — do not "
    "paraphrase, embellish, or invent anything. Estimate years_experience from "
    "the earliest professional role to today."
)


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif suffix == ".docx":
        import docx  # python-docx

        text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs if p.text.strip())
    else:
        raise ValueError(f"Unsupported resume format: {suffix} (use .pdf or .docx)")

    if len(text.strip()) < 100:
        raise ValueError(
            f"Extracted only {len(text.strip())} chars from {path.name} — the file may be "
            "a scanned image. Export a text-based PDF and retry."
        )
    return text


def parse_resume(path: Path, model: str | None = None) -> Profile:
    text = _extract_text(path)
    return structured_call(SYSTEM, f"RESUME:\n\n{text}", Profile, model=model, max_tokens=16000)


def save_profile(profile: Profile, path: Path = PROFILE_PATH) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(profile.model_dump(), sort_keys=False, allow_unicode=True))


ANSWERS_PATH = Path(__file__).resolve().parent.parent / "config" / "answers.yaml"


def load_answer_overrides(path: Path = ANSWERS_PATH) -> dict:
    """User-supplied answers for questions the profile cannot cover."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {k: v for k, v in data.items() if v not in (None, "")} if isinstance(data, dict) else {}


def load_profile_for_forms(path: Path = PROFILE_PATH) -> dict:
    """Master profile plus answer overrides — what form-fillers should use."""
    profile = yaml.safe_load(load_profile_yaml(path)) or {}
    profile["_answer_overrides"] = load_answer_overrides()
    return profile


def load_profile_yaml(path: Path = PROFILE_PATH) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"No master profile at {path}. Run:  python -m jobpilot init-profile <resume.pdf>"
        )
    return path.read_text()


# Identity fields the matcher has no use for — judging fit needs skills and
# experience, not contact details. Stripped before anything leaves the machine.
PII_FIELDS = ("name", "email", "phone", "linkedin", "portfolio")


def load_profile_for_matching(path: Path = PROFILE_PATH) -> str:
    """Profile YAML with contact/identity fields removed, for sending to an LLM."""
    data = yaml.safe_load(load_profile_yaml(path)) or {}
    redacted = {k: v for k, v in data.items() if k not in PII_FIELDS}
    return yaml.safe_dump(redacted, sort_keys=False, allow_unicode=True)
