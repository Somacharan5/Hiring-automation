"""Adapter for the locked resume kit in `Resume_creation/`.

The kit is the source of truth for the exact CV format: `resume.json` is the
content, `template.css` is the measured geometry, and `build.mjs` (Node) renders
them to HTML. We honour its contract — we only ever produce a `resume.json`, let
build.mjs render the HTML, and use our own headless Chromium (Playwright) to turn
that HTML into a PDF. Nothing here hand-writes HTML or edits the CSS.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
KIT_DIR = ROOT / "Resume_creation"
MASTER_PATH = KIT_DIR / "resume.json"
BUILD_MJS = KIT_DIR / "build.mjs"
TEMPLATE_CSS = KIT_DIR / "template.css"


def load_master() -> dict:
    """The master resume content, in the kit's schema."""
    return json.loads(MASTER_PATH.read_text(encoding="utf-8"))


def render_html(data: dict) -> str:
    """Run the kit's build.mjs on `data` and return the HTML it produces."""
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required to render the resume kit — install it "
                           "(the Oracle VM guide covers `apt install nodejs`).")
    if not BUILD_MJS.exists():
        raise RuntimeError(f"resume kit not found at {BUILD_MJS}")

    with tempfile.TemporaryDirectory() as td:
        data_path = Path(td) / "data.json"
        out_path = Path(td) / "out.html"
        data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [node, str(BUILD_MJS), "--data", str(data_path), "--out", str(out_path),
             "--css", str(TEMPLATE_CSS)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"build.mjs failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return out_path.read_text(encoding="utf-8")


def render_pdf(data: dict, out_pdf: Path | str) -> Path:
    """Render `data` to a PDF: build.mjs → HTML → headless Chromium print (A4, no margins)."""
    from playwright.sync_api import sync_playwright

    html = render_html(data)
    out = Path(out_pdf)
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="networkidle")
            page.pdf(path=str(out), format="A4", print_background=True,
                     margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                     prefer_css_page_size=True)
        finally:
            browser.close()
    return out
