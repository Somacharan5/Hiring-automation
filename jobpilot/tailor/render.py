"""Document rendering — ATS-safe PDF (reportlab) and DOCX (python-docx).

Every choice here exists to survive an applicant tracking system's parser:

  * one single column, top to bottom — no tables, no text boxes, no frames
  * no headers, footers, images, logos, icons, charts or rules
  * standard fonts only (Helvetica in PDF, Arial in DOCX)
  * real "•" bullet characters, as selectable text
  * standard section headings: SUMMARY / SKILLS / EXPERIENCE / EDUCATION
  * contact details as plain text at the top of the body

Contact details ARE included — the PII redaction in jobpilot.profile applies to
what gets sent to an LLM, not to the document the candidate sends an employer.

Fitting to `max_pages`: the renderer first tries the roomiest layout, then walks
a ladder of progressively tighter font/spacing settings, and only after that
starts dropping trailing (least-relevant) bullets and then trailing roles. The
LLM orders both most-relevant-first, so what gets cut is what matters least.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.fonts import addMapping
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

BULLET = "•"
DASH = "-"

# Characters that don't survive PDF text extraction cleanly, mapped to ASCII.
# ATS parsers prefer plain ASCII anyway — "INR 39L+" indexes better than "₹39L+".
TRANSLIT = {
    "₹": "INR ", "€": "EUR ", "£": "GBP ", "’": "'", "‘": "'",
    "“": '"', "”": '"', "–": "-", "—": "-", "−": "-",
    "…": "...", " ": " ", "​": "", "•": "-", "﻿": "",
}

# Standard, universally-installed sans fonts. A real TrueType font gives the PDF
# a ToUnicode CMap, so "•" extracts back as U+2022; reportlab's built-in Type1
# cores put the bullet at codepoint 127, which extracts as garbage (\x7f) and
# can pollute the first word of every bullet in an ATS parse.
_FONT_CANDIDATES = (
    ("JPSans", "/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("JPSans", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("JPSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("JPSans", r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
)


@lru_cache(maxsize=1)
def _fonts() -> tuple[str, str, str]:
    """(regular, bold, bullet_char). Falls back to core Helvetica + '-' bullets."""
    for family, regular, bold in _FONT_CANDIDATES:
        if not (Path(regular).exists() and Path(bold).exists()):
            continue
        try:
            pdfmetrics.registerFont(TTFont(family, regular))
            pdfmetrics.registerFont(TTFont(f"{family}-Bold", bold))
            for italic in (0, 1):                       # no italics used; map to upright
                addMapping(family, 0, italic, family)
                addMapping(family, 1, italic, f"{family}-Bold")
            return family, f"{family}-Bold", BULLET
        except Exception:
            continue
    # Helvetica is ATS-standard; the hyphen bullet extracts cleanly everywhere.
    return "Helvetica", "Helvetica-Bold", "-"

MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
          "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}


@dataclass(frozen=True)
class Density:
    """One rung on the shrink ladder. Points unless noted."""
    body: float
    leading: float
    name: float
    heading: float
    gap_section: float          # space above a section heading
    gap_role: float             # space above a role
    gap_bullet: float
    margin: float               # inches


LADDER = (
    Density(10.5, 13.4, 19, 11.0, 11, 7.0, 2.4, 0.72),
    Density(10.0, 12.6, 18, 10.5, 9, 6.0, 2.0, 0.65),
    Density(9.5, 11.8, 17, 10.0, 8, 5.0, 1.6, 0.58),
    Density(9.0, 11.0, 16, 9.5, 6.5, 4.0, 1.2, 0.52),
    Density(8.5, 10.3, 15, 9.0, 5.5, 3.0, 1.0, 0.46),
)


# ── Text helpers ------------------------------------------------------

def clean(text) -> str:
    """ASCII-safe text: transliterate symbols an ATS parser mangles, collapse space."""
    out = str(text or "")
    for src, dst in TRANSLIT.items():
        out = out.replace(src, dst)
    out = "".join(c for c in out if c == "\n" or ord(c) >= 32)
    return re.sub(r"[ \t]+", " ", out).strip()


def _esc(text) -> str:
    return escape(clean(text))


def _fmt_date(value: str | None) -> str:
    """'2025-11' → 'Nov 2025'; 'present' → 'Present'; anything else passes through."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.lower() in ("present", "current", "now", "ongoing"):
        return "Present"
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", raw)
    if m:
        year, month = m.group(1), m.group(2).zfill(2)
        return f"{MONTHS.get(month, '')} {year}".strip()
    return raw


def _date_range(start: str | None, end: str | None) -> str:
    a, b = _fmt_date(start), _fmt_date(end)
    if a and b:
        return f"{a} {DASH} {b}"
    return a or b or ""


def contact_lines(profile) -> list[str]:
    """[name, headline?, 'loc | email | phone | linkedin | portfolio']."""
    lines = [clean(profile.name)]
    if profile.headline:
        lines.append(clean(profile.headline))
    bits = [profile.location, profile.email, profile.phone, profile.linkedin, profile.portfolio]
    contact = "  |  ".join(clean(b) for b in bits if b and str(b).strip())
    if contact:
        lines.append(contact)
    return [line for line in lines if line]


def _role_heading(exp) -> str:
    """'<b>Title</b> — Company, Location  |  Nov 2025 – Mar 2026'"""
    head = f"<b>{_esc(exp.title)}</b> {DASH} {_esc(exp.company)}"
    if getattr(exp, "location", None):
        head += f", {_esc(exp.location)}"
    dates = _date_range(getattr(exp, "start", None), getattr(exp, "end", None))
    if dates:
        head += f"  |  {_esc(dates)}"
    return head


def _education_line(edu) -> str:
    left = ", ".join(x for x in (edu.degree, edu.field) if x) or edu.institution
    line = f"<b>{_esc(left)}</b>"
    if edu.degree or edu.field:
        line += f" {DASH} {_esc(edu.institution)}"
    if edu.year:
        line += f"  |  {_esc(edu.year)}"
    return line


def _trim(tailored, max_bullets: int | None, max_roles: int | None):
    """Content plan: cap bullets per role and drop trailing roles."""
    exps = list(tailored.experiences)
    if max_roles is not None:
        exps = exps[:max_roles]
    out = []
    for i, exp in enumerate(exps):
        bullets = list(exp.bullets or [])
        if max_bullets is not None:
            # The first (most relevant) role keeps one extra bullet.
            cap = max_bullets + (1 if i == 0 else 0)
            bullets = bullets[:cap]
        out.append((exp, bullets))
    return out


# ── PDF ---------------------------------------------------------------

def _styles(d: Density) -> dict[str, ParagraphStyle]:
    regular, bold, _bullet = _fonts()
    return {
        "name": ParagraphStyle("name", fontName=bold, fontSize=d.name,
                               leading=d.name * 1.15, alignment=TA_LEFT, spaceAfter=1.5),
        "headline": ParagraphStyle("headline", fontName=regular, fontSize=d.body + 0.5,
                                   leading=(d.body + 0.5) * 1.2, spaceAfter=1.5),
        "contact": ParagraphStyle("contact", fontName=regular, fontSize=d.body - 0.5,
                                  leading=(d.body - 0.5) * 1.25, spaceAfter=0),
        "heading": ParagraphStyle("heading", fontName=bold, fontSize=d.heading,
                                  leading=d.heading * 1.2, spaceBefore=d.gap_section,
                                  spaceAfter=2.5),
        "body": ParagraphStyle("body", fontName=regular, fontSize=d.body,
                               leading=d.leading, alignment=TA_JUSTIFY, spaceAfter=0),
        "role": ParagraphStyle("role", fontName=regular, fontSize=d.body,
                               leading=d.leading, spaceBefore=d.gap_role, spaceAfter=1.5),
        "bullet": ParagraphStyle("bullet", fontName=regular, bulletFontName=regular,
                                 bulletFontSize=d.body, fontSize=d.body,
                                 leading=d.leading, leftIndent=11, bulletIndent=1.5,
                                 spaceAfter=d.gap_bullet, alignment=TA_LEFT),
    }


def _story(tailored, profile, d: Density, max_bullets, max_roles) -> list:
    st = _styles(d)
    bullet_char = _fonts()[2]
    flow: list = []

    lines = contact_lines(profile)
    flow.append(Paragraph(_esc(lines[0]), st["name"]))
    for line in lines[1:]:
        style = st["headline"] if line == profile.headline else st["contact"]
        flow.append(Paragraph(_esc(line), style))

    if tailored.summary:
        flow.append(Paragraph("SUMMARY", st["heading"]))
        flow.append(Paragraph(_esc(tailored.summary), st["body"]))

    if tailored.selected_skills:
        flow.append(Paragraph("SKILLS", st["heading"]))
        flow.append(Paragraph(_esc("  |  ".join(tailored.selected_skills)), st["body"]))

    plan = _trim(tailored, max_bullets, max_roles)
    if plan:
        flow.append(Paragraph("EXPERIENCE", st["heading"]))
        for exp, bullets in plan:
            block = [Paragraph(_role_heading(exp), st["role"])]
            block += [Paragraph(_esc(b), st["bullet"], bulletText=bullet_char) for b in bullets]
            # Keep the role header with its first bullet, never orphaned.
            flow.append(KeepTogether(block[:2]))
            flow.extend(block[2:])

    if profile.education:
        flow.append(Paragraph("EDUCATION", st["heading"]))
        for edu in profile.education:
            flow.append(Paragraph(_education_line(edu), st["role"]))

    if profile.certifications:
        flow.append(Paragraph("CERTIFICATIONS", st["heading"]))
        for cert in profile.certifications:
            flow.append(Paragraph(_esc(cert), st["bullet"], bulletText=bullet_char))

    flow.append(Spacer(1, 1))
    return flow


def _build_pdf(tailored, profile, d: Density, max_bullets, max_roles) -> tuple[bytes, int]:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=d.margin * inch, rightMargin=d.margin * inch,
        topMargin=d.margin * inch, bottomMargin=max(d.margin - 0.1, 0.35) * inch,
        title="Resume", author=profile.name or "", subject="Resume", creator="JobPilot",
    )
    doc.build(_story(tailored, profile, d, max_bullets, max_roles))
    return buf.getvalue(), doc.page


def _fit_plan(tailored, profile, max_pages: int):
    """Walk the shrink ladder until the document fits. Returns (bytes, density, plan, pages)."""
    n_roles = len(tailored.experiences)
    steps: list[tuple[int, int | None, int | None]] = []
    for i in range(len(LADDER)):                    # 1. shrink type and spacing
        steps.append((i, None, None))
    last = len(LADDER) - 1
    for cap in (4, 3, 2):                           # 2. drop trailing bullets
        steps.append((last, cap, None))
    for roles in range(n_roles - 1, 2, -1):         # 3. drop trailing roles
        steps.append((last, 2, roles))
    steps.append((last, 1, min(4, n_roles)))

    fallback = None
    for density_idx, cap, roles in steps:
        data, pages = _build_pdf(tailored, profile, LADDER[density_idx], cap, roles)
        fallback = (data, LADDER[density_idx], (cap, roles), pages)
        if pages <= max_pages:
            return fallback
    return fallback


def render_pdf(tailored, profile, out_path, max_pages: int = 1) -> str:
    """Render an ATS-safe single-column PDF. Returns the path written."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    data, _density, _plan, _pages = _fit_plan(tailored, profile, max_pages)
    out.write_bytes(data)
    return str(out)


# ── DOCX --------------------------------------------------------------

def render_docx(tailored, profile, out_path, max_pages: int = 1) -> str:
    """Render the same content as a single-column .docx. Returns the path written.

    No tables, no text boxes, no headers/footers, no images — just styled
    paragraphs, which is what Workday-class parsers handle best.
    """
    import docx
    from docx.shared import Inches, Pt

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Reuse the PDF pagination trial so both formats carry identical content.
    _data, density, (max_bullets, max_roles), _pages = _fit_plan(tailored, profile, max_pages)

    doc = docx.Document()
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Inches(density.margin)
        section.left_margin = section.right_margin = Inches(density.margin)

    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(density.body)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.line_spacing = density.leading / density.body

    def para(text: str = "", *, bold=False, size=None, space_before=0.0, space_after=0.0,
             indent=None, hanging=False):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before = Pt(space_before)
        pf.space_after = Pt(space_after)
        if indent is not None:
            pf.left_indent = Inches(indent)
            if hanging:
                pf.first_line_indent = Inches(-indent)
        if text:
            run = p.add_run(clean(text))
            run.bold = bold
            if size:
                run.font.size = Pt(size)
        return p

    def bullet_para(text: str):
        # A literal "•" run — Word list numbering doesn't always survive parsing.
        p = para(space_after=density.gap_bullet, indent=0.18, hanging=True)
        p.add_run(f"{BULLET}  ")
        p.add_run(clean(text))
        return p

    def heading(text: str):
        para(text, bold=True, size=density.heading, space_before=density.gap_section,
             space_after=2.0)

    def mixed(bold_text: str, rest: str, space_before=0.0, space_after=1.5):
        p = para(space_before=space_before, space_after=space_after)
        p.add_run(clean(bold_text)).bold = True
        if rest and clean(rest):
            p.add_run(" " + clean(rest))     # clean() strips, so re-add the separator
        return p

    lines = contact_lines(profile)
    para(lines[0], bold=True, size=density.name, space_after=1.5)
    for line in lines[1:]:
        para(line, size=density.body if line == profile.headline else density.body - 0.5,
             space_after=1.5)

    if tailored.summary:
        heading("SUMMARY")
        para(tailored.summary)

    if tailored.selected_skills:
        heading("SKILLS")
        para("  |  ".join(tailored.selected_skills))

    plan = _trim(tailored, max_bullets, max_roles)
    if plan:
        heading("EXPERIENCE")
        for exp, bullets in plan:
            rest = f" {DASH} {exp.company}"
            if getattr(exp, "location", None):
                rest += f", {exp.location}"
            dates = _date_range(getattr(exp, "start", None), getattr(exp, "end", None))
            if dates:
                rest += f"  |  {dates}"
            mixed(exp.title, rest, space_before=density.gap_role)
            for bullet in bullets:
                bullet_para(bullet)

    if profile.education:
        heading("EDUCATION")
        for edu in profile.education:
            left = ", ".join(x for x in (edu.degree, edu.field) if x) or edu.institution
            rest = f" {DASH} {edu.institution}" if (edu.degree or edu.field) else ""
            if edu.year:
                rest += f"  |  {edu.year}"
            mixed(left, rest, space_before=density.gap_role)

    if profile.certifications:
        heading("CERTIFICATIONS")
        for cert in profile.certifications:
            bullet_para(cert)

    doc.core_properties.title = "Resume"
    doc.core_properties.author = profile.name or ""
    doc.save(str(out))
    return str(out)
