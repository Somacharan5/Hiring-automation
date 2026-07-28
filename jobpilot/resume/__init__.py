"""Per-job resume generation via the locked kit in `Resume_creation/`.

The kit (resume.json + template.css + build.mjs) owns the exact format. We only
ever produce a tailored `resume.json` and render it through the kit. Editing the
CSS or hand-writing HTML is off-limits — see Resume_creation/CLAUDE.md.
"""

from .kit import load_master, render_html, render_pdf
from .tailor import tailor_and_render, tailor_data

__all__ = ["load_master", "render_html", "render_pdf", "tailor_and_render", "tailor_data"]
