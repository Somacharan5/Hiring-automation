"""Email outreach — the only channel. Everything here is gated by outreach.dry_run.

Pipeline:
    prepare_outreach  discover a company address → tailor the resume → compose the email
    send_pending      send prepared emails (guards: dry_run, first-run-draft, cap, dedup)
    run_followups     3-day "Re: …" bumps, up to the cap
    scan_replies      read the inbox → replies + notifications (dashboard bell)

`pipeline.run_outreach_cycle` runs the whole pass in order.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .composer import EmailDraft, compose_email, master_resume, quality_issues, render_draft
from .find_addresses import best_sendable_contact, discover_generic, resolve_domain
from .followups import run_followups
from .inbox_reader import classify, scan_replies
from .pipeline import prepare_outreach, run_outreach_cycle
from .send import live_sending_enabled, send_pending

SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "settings.yaml"


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    """Read config/settings.yaml (read-only)."""
    return yaml.safe_load(path.read_text()) or {}


__all__ = [
    "load_settings",
    "EmailDraft", "compose_email", "quality_issues", "render_draft", "master_resume",
    "discover_generic", "best_sendable_contact", "resolve_domain",
    "send_pending", "live_sending_enabled",
    "run_followups", "scan_replies", "classify",
    "prepare_outreach", "run_outreach_cycle",
]
