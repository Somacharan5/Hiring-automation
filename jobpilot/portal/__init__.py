"""ATS portal auto-apply — Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
Recruitee.

Entry points for CLI wiring:

    from jobpilot.portal import apply_batch, apply_to_job
    apply_batch(conn, settings, limit=None, log=print) -> dict
    apply_to_job(context, conn, job_row, settings, profile=None, log=print) -> dict

Everything defaults to a dry run: `portal.dry_run` must be explicitly set to
false in config/settings.yaml before anything is ever submitted. See NOTES.md.
"""

from .apply import (CHANNEL, apply_batch, apply_to_job, find_master_cv,
                    load_profile_dict, resolve_resume_path, select_candidates,
                    verify_submitted)
from .forms import (ADAPTERS, BlockerDetected, FillResult, PortalAnswerer,
                    adapter_for, application_url, detect_adapter, detect_blocker,
                    generic_fill, is_dry_run, is_known_ats_url, portal_cfg,
                    provider_from_url)

__all__ = [
    "CHANNEL",
    "apply_batch",
    "apply_to_job",
    "select_candidates",
    "resolve_resume_path",
    "find_master_cv",
    "load_profile_dict",
    "verify_submitted",
    "ADAPTERS",
    "adapter_for",
    "detect_adapter",
    "application_url",
    "provider_from_url",
    "is_known_ats_url",
    "generic_fill",
    "FillResult",
    "PortalAnswerer",
    "portal_cfg",
    "is_dry_run",
    "detect_blocker",
    "BlockerDetected",
]
