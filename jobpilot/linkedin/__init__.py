"""JobPilot LinkedIn automation (Phase 4).

Assisted personal job-seeking on the user's own account. Conservative by
construction: dry-run default, visible browser, randomised human pacing, a daily
cap, manual-login-only auth, and a hard refusal to invent answers to screening
questions.

Typical wiring from the CLI::

    from jobpilot.linkedin import search_and_store, apply_batch

    search_and_store(conn, settings)                  # discover Easy Apply jobs
    summary = apply_batch(conn, settings, limit=5)    # apply (dry run by default)

Lower level::

    from jobpilot.linkedin import launch, ensure_logged_in, search_jobs, apply_to_job
"""

from __future__ import annotations

import sqlite3

from ..db import Job, upsert_jobs
from .answers import Answer, FieldSpec, ProfileAnswerer
from .apply import (apply_batch, apply_to_job, remaining_quota, resolve_resume_path,
                    select_candidates)
from .search import LinkedInResult, search_jobs, search_jobs_detailed
from .session import (BlockerDetected, LoginRequired, detect_blocker, ensure_logged_in,
                      human_delay, is_dry_run, launch, linkedin_cfg, save_session)

__all__ = [
    "Job", "Answer", "FieldSpec", "ProfileAnswerer", "LinkedInResult",
    "BlockerDetected", "LoginRequired",
    "launch", "ensure_logged_in", "save_session", "human_delay", "detect_blocker",
    "linkedin_cfg", "is_dry_run",
    "search_jobs", "search_jobs_detailed", "search_and_store",
    "apply_to_job", "apply_batch", "select_candidates", "remaining_quota",
    "resolve_resume_path",
]


def search_and_store(conn: sqlite3.Connection, settings: dict | None,
                     terms: list[str] | None = None, locations: list[str] | None = None,
                     limit_per_search: int = 25, log=print) -> int:
    """Run a LinkedIn UI search and upsert the results. Returns # new jobs.

    Falls back to `settings['search']['terms'] / ['locations']` so the CLI can
    call it with nothing but the settings dict.
    """
    from playwright.sync_api import sync_playwright

    search_cfg = (settings or {}).get("search") or {}
    terms = terms or search_cfg.get("terms") or []
    locations = locations or search_cfg.get("locations") or []
    hours_old = search_cfg.get("hours_old")

    with sync_playwright() as pw:
        browser, context = launch(settings, pw)
        try:
            ensure_logged_in(context, settings, log=log)
            jobs = search_jobs(context, terms, locations, settings,
                               limit_per_search=limit_per_search,
                               hours_old=hours_old, log=log)
        finally:
            try:
                context.close()
            finally:
                browser.close()

    added = upsert_jobs(conn, jobs)
    log(f"  [linkedin] {len(jobs)} jobs found, {added} new")
    return added
