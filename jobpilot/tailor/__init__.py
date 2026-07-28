"""Phase 2 — resume tailoring.

    from jobpilot.tailor import tailor_and_render
    result = tailor_and_render(conn, job_id, settings)   # PDF/DOCX + ATS score, recorded in db

Lower-level pieces are exported too: `tailor_for_job` (LLM step only),
`render_pdf` / `render_docx`, and `score_ats` / `verify_pdf_extractable`.
"""

from .ats_check import extract_keywords, score_ats, verify_pdf_extractable
from .engine import (
    FabricationError,
    TailoredExperience,
    TailoredResume,
    TailorResult,
    load_settings,
    tailor_and_render,
    tailor_for_job,
)
from .render import render_docx, render_pdf

__all__ = [
    "tailor_and_render",
    "tailor_for_job",
    "TailoredResume",
    "TailoredExperience",
    "TailorResult",
    "FabricationError",
    "render_pdf",
    "render_docx",
    "score_ats",
    "extract_keywords",
    "verify_pdf_extractable",
    "load_settings",
]
