"""Render the exact-format resume: data.json + template.html → PDF (via Playwright).

    python -m jobpilot.resume                      # data.json → output/resumes/resume.pdf
    python -m jobpilot.resume path/to/data.json out.pdf

The layout is fixed in template.html; only the JSON content changes. Inline
markup in any text field: **bold**, *italic*, [label](url). Company/institution
links use the `href` field. Chromium keeps the links clickable in the PDF.
"""

from __future__ import annotations

import html as _html
import json
import re
import sys
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DATA_PATH = HERE / "data.json"
DEFAULT_OUT = ROOT / "output" / "resumes" / "resume.pdf"

_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITAL = re.compile(r"\*(.+?)\*", re.S)


def rich(text: Any) -> Markup:
    """Convert lightweight markup to safe HTML: [label](url), **bold**, *italic*.

    HTML-escapes first, so content is never injected; then applies markup. Links
    are resolved before bold/italic so a link may sit inside a **bold** span.
    """
    if not text:
        return Markup("")
    s = _html.escape(str(text))
    s = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _ITAL.sub(r"<em>\1</em>", s)
    return Markup(s)


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(HERE)), autoescape=True)
    env.filters["rich"] = rich
    return env


def render_html(data: dict) -> str:
    return _env().get_template("template.html").render(**data)


def render_pdf(data: dict, out_path: Path | str = DEFAULT_OUT) -> Path:
    """Render `data` to a PDF at `out_path` using headless Chromium. Returns the path."""
    from playwright.sync_api import sync_playwright

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(data)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="networkidle")
            page.pdf(
                path=str(out), format="A4", print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                prefer_css_page_size=True,
            )
        finally:
            browser.close()
    return out


def load_data(path: Path | str = DATA_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    data_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_PATH
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    data = load_data(data_path)
    out = render_pdf(data, out_path)
    print(f"✔ Rendered {data.get('name', 'resume')} → {out}")


if __name__ == "__main__":
    main()
