"""Auto-apply through company ATS portals (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Recruitee).

Safety posture, in order of importance:

1. **dry_run is the default and the gate is structural.** The submit button is
   reached through exactly one call — `_submit_decision()` — which returns
   `"submit"` only when `portal.dry_run` parses cleanly to false. Every other
   outcome (key missing, key malformed, whole settings block absent) yields
   `"dry_run"`, and the page is closed without pressing anything.
2. **We refuse rather than invent.** Any *required* question the master profile
   cannot answer aborts that application, which is recorded as
   `status='failed', error='needs manual answer: <question>'` for the dashboard.
   The single exception is EEO/demographics, where "Decline to self-identify" is
   itself an honest answer — see `forms.PortalAnswerer`.
3. **Daily cap** from `db.sent_today_count(conn, 'portal')`.
4. **Idempotent** — `select_candidates` excludes any job that already has a
   `portal` application row, and `applications` has UNIQUE(job_id, channel).
5. **Pacing** between every action; **bot-check guard** after every navigation.

Live-verification status of each adapter is recorded in NOTES.md. Read it before
flipping `dry_run` to false.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..db import (latest_resume_for_job, mark_application_sent, sent_today_count,
                  upsert_application)
from ..linkedin.session import human_delay
from . import forms
from .forms import (ADAPTERS, BlockerDetected, FillResult, adapter_for,
                    detect_adapter, is_known_ats_url, portal_cfg, remaining_quota)

ROOT = Path(__file__).resolve().parent.parent.parent
CHANNEL = "portal"

# A plain, current desktop Chrome UA. Playwright's default advertises
# HeadlessChrome, which several ATS bot filters flag on sight.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1440, "height": 1000}

# ATS URL shapes we know how to fill. Used by the candidate query so the batch
# never opens a page we have no adapter for.
ATS_URL_LIKES = (
    "%greenhouse.io%", "%gh_jid=%", "%lever.co%", "%ashbyhq.com%",
    "%workable.com%", "%smartrecruiters.com%", "%recruitee.com%",
)


# ── Pure logic (unit-tested without a browser) ───────────────────────

def _submit_decision(settings: dict | None) -> str:
    """THE dry-run gate. Returns 'submit' or 'dry_run'.

    Fails safe: anything other than an explicit, parseable `dry_run: false`
    results in 'dry_run'. This is the only place in the package that authorises
    pressing a submit button.
    """
    return "dry_run" if forms.is_dry_run(settings) else "submit"


def select_candidates(conn: sqlite3.Connection, settings: dict | None,
                      limit: int | None = None) -> list[sqlite3.Row]:
    """Shortlisted, well-scored, known-ATS jobs with no portal application yet.

    The `LEFT JOIN … a.id IS NULL` is the idempotency guarantee: a job that has
    ever been attempted through this channel — sent, drafted or failed — is never
    picked up again automatically. Clear its row to retry it.
    """
    cfg = portal_cfg(settings)
    likes = " OR ".join(["LOWER(j.url) LIKE ?"] * len(ATS_URL_LIKES))
    sql = (
        "SELECT j.* FROM jobs j "
        "LEFT JOIN applications a ON a.job_id = j.id AND a.channel = ? "
        "WHERE j.status = 'shortlisted' "
        "  AND COALESCE(j.match_score, 0) >= ? "
        "  AND j.url IS NOT NULL "
        f"  AND ({likes}) "
        "  AND a.id IS NULL "
        "ORDER BY COALESCE(j.match_score, 0) DESC, j.collected_at DESC"
    )
    params: list = [CHANNEL, int(cfg["min_score_to_apply"]), *ATS_URL_LIKES]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def find_master_cv(root: Path = ROOT) -> str | None:
    """Fallback résumé: the master CV PDF sitting in the project root."""
    scored = []
    for p in sorted(root.glob("*.pdf")):
        name = p.name.lower()
        if "portfolio" in name:
            continue
        scored.append((2 if ("cv" in name or "resume" in name) else 0, p))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1].name))
    return str(scored[0][1])


def resolve_resume_path(conn: sqlite3.Connection, job_id: str,
                        root: Path = ROOT) -> str | None:
    """Tailored résumé for this job if the tailor produced one, else the master CV."""
    try:
        row = latest_resume_for_job(conn, job_id)
    except Exception:  # noqa: BLE001 — the table may not be populated yet
        row = None
    if row is not None:
        try:
            path = row["pdf_path"]
        except (IndexError, KeyError):
            path = None
        if path and Path(path).exists():
            return str(path)
    return find_master_cv(root)


def load_profile_dict() -> dict:
    """Master profile as a plain dict (the answerer's only source of truth)."""
    import yaml

    from ..profile import load_profile_for_forms
    try:
        return load_profile_for_forms()
    except FileNotFoundError:
        return {}


def summarise_result(result: FillResult) -> str:
    """One-line human summary of a fill, for the run log."""
    return (f"{len(result.fields)} fields, "
            f"{'résumé attached' if result.resume_attached else 'no résumé'}, "
            f"{len(result.unanswered)} unanswered "
            f"({len(result.required_unanswered)} required), "
            f"submit {'found' if result.submit_found else 'NOT found'}")


# ── Browser ──────────────────────────────────────────────────────────

def launch(settings: dict | None, playwright):
    """Start Chromium with a plain desktop fingerprint. Returns (browser, context).

    No stored session, no login: ATS application forms are public. Nothing this
    package does requires credentials.
    """
    cfg = portal_cfg(settings)
    browser = playwright.chromium.launch(
        headless=bool(cfg["headless"]),
        args=["--disable-blink-features=AutomationControlled",
              "--no-default-browser-check", "--no-first-run"],
    )
    context = browser.new_context(
        user_agent=DEFAULT_UA, viewport=dict(DEFAULT_VIEWPORT),
        locale="en-US", timezone_id="Asia/Kolkata", accept_downloads=False,
    )
    context.set_default_timeout(45_000)
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    return browser, context


def _goto(page, url: str, settings: dict | None, log=print):
    """Navigate + bot-check guard + pace. The only navigation helper here."""
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    status = resp.status if resp else None
    page.wait_for_timeout(3_500)          # SPA forms need a beat to render
    forms.guard(page, status=status, log=log)
    human_delay(forms.pacing(settings), scale=0.4)
    return resp


def verify_submitted(page, adapter, timeout_ms: int = 20_000) -> tuple[bool, str]:
    """After a real submit: did the page turn into a confirmation?

    Returns (success, evidence). Deliberately strict — an unverified submit is
    reported as such rather than optimistically recorded as sent.
    """
    deadline = timeout_ms
    step = 2_000
    while deadline > 0:
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            url = ""
        try:
            text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            text = ""
        if forms.looks_successful(url=url, text=text):
            snippet = ""
            low = (text or "").lower()
            m = forms.SUCCESS_PATTERNS.search(low)
            if m:
                snippet = text[max(0, m.start() - 40):m.end() + 80].strip()
            return True, snippet or f"url: {url[:160]}"
        for marker in getattr(adapter, "success_markers", ()):
            try:
                if page.locator(marker).count():
                    return True, f"marker: {marker}"
            except Exception:  # noqa: BLE001
                continue
        page.wait_for_timeout(step)
        deadline -= step
    try:
        return False, f"no confirmation seen; still at {page.url[:160]}"
    except Exception:  # noqa: BLE001
        return False, "no confirmation seen"


# ── One application ──────────────────────────────────────────────────

@dataclass
class ApplyOutcome:
    status: str          # dry_run | sent | failed
    reason: str = ""
    payload: dict | None = None


def apply_to_job(context, conn: sqlite3.Connection, job_row, settings: dict | None,
                 profile: dict | None = None, log=print) -> dict:
    """Apply to one job through its company ATS.

    Returns:
        {'job_id','title','company','url','apply_url','provider','status',
         'reason','payload'}
    where status ∈ {'dry_run', 'sent', 'failed'}.

    Records to the DB:
      dry_run → applications.status='drafted' with the full payload in `body`
      sent    → 'sent' (upsert_application + mark_application_sent)
      failed  → 'failed' with `error='needs manual answer: …'` — the manual queue
    """
    job_id = job_row["id"]
    url = job_row["url"]
    title = job_row["title"]
    company = job_row["company"]
    source = job_row["source"] if "source" in job_row.keys() else None

    adapter = adapter_for(source, url)
    apply_url = adapter.application_url(url, company=company)
    result = {"job_id": job_id, "title": title, "company": company, "url": url,
              "apply_url": apply_url, "provider": adapter.name,
              "status": "failed", "reason": "", "payload": None}

    if profile is None:
        profile = load_profile_dict()
    resume_path = resolve_resume_path(conn, job_id)

    def record(status: str, *, error: str | None = None, body: dict | None = None,
               subject: str | None = None) -> int:
        return upsert_application(
            conn, job_id, CHANNEL, status, resume_path=resume_path,
            subject=subject or f"Portal apply ({adapter.name}) — {title} @ {company}",
            body=json.dumps(body, indent=2, default=str)[:20000] if body else None,
            error=error)

    page = context.new_page()
    try:
        log(f"\n  ▸ {title} @ {company}  [{adapter.name}]")
        log(f"    {apply_url}")
        _goto(page, apply_url, settings, log=log)
        forms.dismiss_cookie_banner(page, log=log)

        # The URL told us which provider to expect; ask the DOM to confirm it,
        # because company-hosted boards and redirects make URLs unreliable.
        live = detect_adapter(page)
        if live.name != adapter.name and live.name != "generic":
            log(f"    (page looks like {live.name}, not {adapter.name} — using {live.name})")
            adapter = live
            result["provider"] = adapter.name

        scope = adapter.open_form(page, settings, log=log)
        forms.guard(page, log=log)

        filled = forms.generic_fill(page, scope, adapter, profile, resume_path,
                                    settings, log=log)
        result["payload"] = filled.payload()
        log(f"    · {summarise_result(filled)}")

        if filled.error:
            reason = filled.error
            record("failed", error=f"needs manual answer: {reason}"[:500],
                   body=filled.payload())
            result.update(status="failed", reason=reason)
            return result

        if filled.blocked or filled.required_unanswered:
            question = filled.blocked or filled.required_unanswered[0]["question"]
            detail = next((u["reason"] for u in filled.unanswered
                           if u["question"] == question), "")
            reason = f"needs manual answer: {question}"
            log(f"    ✗ {reason}")
            record("failed", error=f"{reason} ({detail})"[:500], body=filled.payload())
            result.update(status="failed", reason=reason)
            return result

        if not filled.submit_found:
            reason = "no submit button found on the application form"
            record("failed", error=f"needs manual answer: {reason}",
                   body=filled.payload())
            result.update(status="failed", reason=reason)
            return result

        # ── THE GATE ── nothing below the dry-run branch touches submit.
        if _submit_decision(settings) == "dry_run":
            log("\n    ┌─ DRY RUN — application NOT submitted ─────────────")
            log(f"    │ provider : {adapter.name}")
            log(f"    │ résumé   : {resume_path or '(none)'} "
                f"({'attached' if filled.resume_attached else 'NOT attached'})")
            for item in filled.fields:
                log(f"    │ {str(item['question'])[:64]!r} = {item['answer']!r} "
                    f"[{item['source']}]")
            for item in filled.unanswered:
                log(f"    │ (left blank) {str(item['question'])[:60]!r} — {item['reason']}")
            log(f"    │ would have clicked: {filled.submit_label!r}")
            log("    └───────────────────────────────────────────────────")
            record("drafted", error="dry_run: filled but not submitted",
                   body=filled.payload())
            result.update(status="dry_run", reason="dry_run — not submitted")
            return result

        submit, label = forms.find_submit(scope, adapter)
        if submit is None:
            reason = "submit button vanished between fill and submit"
            record("failed", error=f"needs manual answer: {reason}",
                   body=filled.payload())
            result.update(status="failed", reason=reason)
            return result

        log(f"    ▶ submitting ({label!r})")
        human_delay(forms.pacing(settings), why="before submit", log=log)
        submit.click(timeout=20_000)
        human_delay(forms.pacing(settings), scale=0.8, why="after submit", log=log)
        forms.guard(page, log=log)

        ok, evidence = verify_submitted(page, adapter)
        payload = dict(filled.payload())
        payload["submitted"] = {"verified": ok, "evidence": evidence}
        result["payload"] = payload
        if ok:
            app_id = record("sent", body=payload)
            mark_application_sent(conn, app_id)
            log(f"    ✔ submitted — {evidence[:120]}")
            result.update(status="sent", reason="submitted")
            return result

        reason = f"submit clicked but not confirmed — {evidence}"
        log(f"    ! {reason}")
        record("failed", error=f"needs manual check: {reason}"[:500], body=payload)
        result.update(status="failed", reason=reason)
        return result

    except BlockerDetected:
        raise
    except Exception as e:  # noqa: BLE001 — one bad posting must not kill the batch
        reason = f"{type(e).__name__}: {e}"
        log(f"    ✗ {reason}")
        try:
            record("failed", error=f"needs manual answer: automation error — {reason}"[:500])
        except Exception:  # noqa: BLE001
            pass
        result.update(status="failed", reason=reason)
        return result
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


# ── The batch ────────────────────────────────────────────────────────

def apply_batch(conn: sqlite3.Connection, settings: dict | None,
                limit: int | None = None, log=print) -> dict:
    """Apply to every eligible shortlisted ATS job, within the daily cap.

    Returns:
        {'considered','attempted','sent','dry_run','failed','cap','sent_today',
         'quota','dry_run_mode','aborted','by_provider','results'}
    """
    cfg = portal_cfg(settings)
    dry = _submit_decision(settings) == "dry_run"

    sent_today = sent_today_count(conn, CHANNEL)
    quota = remaining_quota(cfg["daily_apply_cap"], sent_today)
    summary = {"considered": 0, "attempted": 0, "sent": 0, "dry_run": 0, "failed": 0,
               "cap": cfg["daily_apply_cap"], "sent_today": sent_today,
               "quota": quota, "dry_run_mode": dry, "aborted": None,
               "by_provider": {}, "results": []}

    candidates = select_candidates(conn, settings, limit=limit)
    summary["considered"] = len(candidates)

    log(f"\nATS portal auto-apply — "
        f"{'DRY RUN (nothing will be submitted)' if dry else '*** LIVE ***'}")
    log(f"  daily cap {cfg['daily_apply_cap']} · already sent today {sent_today} "
        f"· quota {quota}")
    log(f"  min score {cfg['min_score_to_apply']} · candidates {len(candidates)}")

    if not candidates:
        log("  Nothing to apply to. Run `collect` + `match` first, or check that "
            "shortlisted jobs have ATS URLs.")
        return summary

    # The cap bounds a dry run too, so a rehearsal looks exactly like the real thing.
    budget = quota
    if budget == 0:
        log("  ✋ Daily cap reached — stopping. Try again tomorrow.")
        return summary
    if limit:
        budget = min(budget, int(limit))
    targets = candidates[:budget]
    if len(candidates) > len(targets):
        log(f"  (capped to {len(targets)} of {len(candidates)} candidates)")

    profile = load_profile_dict()
    if not profile:
        log("  ! No master profile found — every question will be refused.")
    else:
        missing = [k for k in ("location", "linkedin", "portfolio")
                   if not profile.get(k)]
        if missing:
            log(f"  ! config/profile.yaml has no {', '.join(missing)} — forms that "
                "require those will go to the manual queue. Filling them in is the "
                "single biggest win for this channel.")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser, context = launch(settings, pw)
        try:
            for i, row in enumerate(targets, 1):
                log(f"\n[{i}/{len(targets)}]")
                try:
                    res = apply_to_job(context, conn, row, settings,
                                       profile=profile, log=log)
                except BlockerDetected as e:
                    summary["aborted"] = str(e)
                    log("  ✖ Run aborted — bot check detected. Nothing further "
                        "will be attempted.")
                    break
                summary["results"].append(res)
                summary["attempted"] += 1
                bucket = summary["by_provider"].setdefault(
                    res["provider"], {"sent": 0, "dry_run": 0, "failed": 0})
                key = res["status"] if res["status"] in bucket else "failed"
                bucket[key] += 1
                summary[key] += 1

                if i < len(targets):
                    human_delay(forms.pacing(settings), scale=1.5,
                                why="between applications", log=log)
        finally:
            try:
                context.close()
            finally:
                browser.close()

    log(f"\n✔ Done — attempted {summary['attempted']}, sent {summary['sent']}, "
        f"dry-run {summary['dry_run']}, needs-manual/failed {summary['failed']}")
    if summary["by_provider"]:
        log(f"  by provider: {summary['by_provider']}")
    if summary["failed"]:
        log("  Review the manual queue:  SELECT job_id, error FROM applications "
            "WHERE channel='portal' AND status='failed';")
    return summary


__all__ = ["apply_to_job", "apply_batch", "select_candidates", "launch",
           "resolve_resume_path", "find_master_cv", "load_profile_dict",
           "verify_submitted", "CHANNEL", "ADAPTERS", "is_known_ats_url"]
