"""Phase 3 — recruiter discovery + email outreach.

Typical flow:

    from jobpilot import db
    from jobpilot.outreach import load_settings, compose_for_shortlist, approve_all, send_pending

    conn, settings = db.connect(), load_settings()
    compose_for_shortlist(conn, settings)   # discover contacts + draft emails (no sending)
    approve_all(conn, min_score=80)         # human approval step
    send_pending(conn, settings)            # dry-run by default; sends only if dry_run: false

Nothing outside `sender.send_pending` can transmit mail.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .bounce import Bounce, apply_bounce_feedback, run_bounce_loop, scan_bounces
from .composer import (EmailDraft, compose_email, compose_for_shortlist, master_resume,
                       quality_issues, render_draft)
from .discovery import (AUTO_SENDABLE_TIERS, CONF_HIGH, CONF_REVIEW, CONF_VERIFIED,
                        TIER_HIGH, TIER_REJECT, TIER_REVIEW, TIER_VERIFIED,
                        best_contact_for, can_auto_send, discover_and_verify,
                        infer_pattern, review_queue, tier_for)
from .finder import (ContactCandidate, apollo_people_search, discover_contacts,
                     find_domain, finder_availability, generic_guess,
                     hunter_domain_search, pattern_guess, verify_smtp)
from .free_sources import (crawl_site_for_emails, free_source_availability,
                           github_emails, google_cse_search, linkedin_recruiter_names)
from .sender import (approve, approve_all, live_sending_enabled, pending_summary,
                     send_pending)
from .verify import (VerificationResult, check_mx, check_syntax, detect_catch_all,
                     is_disposable, is_role_address, smtp_probe, verify_email)

SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "settings.yaml"


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    """Read config/settings.yaml (read-only; this module never writes it)."""
    return yaml.safe_load(path.read_text()) or {}


__all__ = [
    "load_settings",
    # finder
    "ContactCandidate", "find_domain", "hunter_domain_search", "apollo_people_search",
    "pattern_guess", "generic_guess", "verify_smtp", "discover_contacts",
    "finder_availability",
    # composer
    "EmailDraft", "compose_email", "compose_for_shortlist", "quality_issues",
    "render_draft", "master_resume",
    # sender
    "send_pending", "approve", "approve_all", "pending_summary", "live_sending_enabled",
    # free discovery sources (no paid API)
    "crawl_site_for_emails", "github_emails", "google_cse_search",
    "linkedin_recruiter_names", "free_source_availability",
    # verification stack
    "VerificationResult", "verify_email", "detect_catch_all", "smtp_probe",
    "check_syntax", "check_mx", "is_disposable", "is_role_address",
    # orchestration + send-gating contract
    "discover_and_verify", "best_contact_for", "review_queue", "tier_for",
    "can_auto_send", "infer_pattern",
    "TIER_VERIFIED", "TIER_HIGH", "TIER_REVIEW", "TIER_REJECT", "AUTO_SENDABLE_TIERS",
    "CONF_VERIFIED", "CONF_HIGH", "CONF_REVIEW",
    # bounce feedback loop
    "Bounce", "scan_bounces", "apply_bounce_feedback", "run_bounce_loop",
]
