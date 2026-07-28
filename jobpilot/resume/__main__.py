"""Render the master resume through the locked kit (Resume_creation/).

    python -m jobpilot.resume            # → output/resumes/resume.pdf
"""

from . import kit

out = kit.ROOT / "output" / "resumes" / "resume.pdf"
kit.render_pdf(kit.load_master(), out)
print(f"✔ Rendered master resume via kit → {out}")
