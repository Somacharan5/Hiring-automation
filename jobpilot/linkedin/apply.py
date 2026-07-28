"""LinkedIn Easy Apply automation.

Safety posture, in order of importance:

1. **dry_run is the default and the gate is structural.** The submit button is
   reached through exactly one call — `_submit_decision()` — which returns
   `"submit"` only when `dry_run` parses cleanly to false. Every other outcome
   (key missing, key malformed, whole settings block missing) yields `"dry_run"`,
   and the modal is discarded without submitting.
2. **We refuse rather than invent.** If the form asks anything the master profile
   does not answer, the application is abandoned and recorded as
   `status='failed', error='needs manual answer: <question>'`.
3. **Daily cap** from `db.sent_today_count(conn, 'linkedin')`.
4. **Pacing** between every action; **blocker guard** after every navigation.

The DOM-walking half of this file is written against the authenticated Easy Apply
modal and is UNVERIFIED against live LinkedIn (no credentials here). It *is*
exercised end to end against a local mock modal — see
`fixtures/easy_apply_mock.html` and `test_linkedin.py`. Read NOTES.md before
trusting it against the real site.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..db import (latest_resume_for_job, mark_application_sent, sent_today_count,
                  upsert_application)
from . import session as sess
from .answers import Answer, FieldSpec, ProfileAnswerer, summarise_payload
from .session import BlockerDetected, human_delay

ROOT = Path(__file__).resolve().parent.parent.parent
CHANNEL = "linkedin"
MAX_MODAL_STEPS = 14        # a real Easy Apply is 1-5 steps; 14 means we're looping

# ── Selectors (authenticated Easy Apply modal — UNVERIFIED, see NOTES.md) ──

EASY_APPLY_BUTTON = (
    "button.jobs-apply-button",
    "button[aria-label*='Easy Apply']",
    "div.jobs-apply-button--top-card button",
)
MODAL = (
    "div.jobs-easy-apply-modal",
    "div[data-test-modal][role='dialog']",
    "div.artdeco-modal[role='dialog']",
    "div[role='dialog']",
)
FORM_CONTAINER = "form, div.jobs-easy-apply-content, div.artdeco-modal__content"
FILE_INPUT = "input[type='file']"
DISMISS_BUTTON = (
    "button[aria-label='Dismiss']",
    "button.artdeco-modal__dismiss",
    "button[aria-label*='Dismiss']",
)
DISCARD_BUTTON = (
    "button[data-control-name='discard_application_confirm_btn']",
    "button[data-test-dialog-secondary-btn]",
)
FOLLOW_COMPANY_CHECKBOX = "input#follow-company-checkbox, label[for='follow-company-checkbox'] input"

NEXT_LABELS = ("continue to next step", "continue", "next", "review your application", "review")
SUBMIT_LABELS = ("submit application", "submit")

# One JS pass beats dozens of locator round-trips, and gets us the label
# association (for=, aria-labelledby, enclosing fieldset) that Playwright alone
# would make painful.
_FIELD_SCRAPER = """
(root) => {
  const els = Array.from(root.querySelectorAll('input, select, textarea'));
  const txt = (n) => n ? (n.innerText || n.textContent || '').replace(/\\s+/g, ' ').trim() : '';
  return els.map((el, i) => {
    const tag = el.tagName.toLowerCase();
    const type = (tag === 'input' ? (el.type || 'text') : tag).toLowerCase();
    let label = el.getAttribute('aria-label') || '';
    if (!label && el.id) {
      const esc = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id;
      label = txt(root.querySelector('label[for="' + esc + '"]'))
           || txt(document.querySelector('label[for="' + esc + '"]'));
    }
    if (!label) {
      const lb = el.getAttribute('aria-labelledby');
      if (lb) label = lb.split(/\\s+/).map(id => txt(document.getElementById(id))).join(' ').trim();
    }
    let groupLabel = '';
    const grp = el.closest('fieldset, .fb-dash-form-element, .jobs-easy-apply-form-element');
    if (grp) groupLabel = txt(grp.querySelector('legend, .fb-dash-form-element__label, label, span[data-test-form-builder-radio-button-form-component__title]'));
    if (!label) {
      const anc = el.closest('.fb-dash-form-element, .jobs-easy-apply-form-element, .artdeco-text-input--container, .jobs-easy-apply-form-section__grouping');
      if (anc) label = txt(anc.querySelector('label, legend, .artdeco-text-input--label'));
    }
    if (!label) label = el.placeholder || el.name || '';
    const options = tag === 'select' ? Array.from(el.options).map(o => (o.text||'').trim()) : [];
    const required = !!(el.required || el.getAttribute('aria-required') === 'true'
                        || /\\*/.test(label) || /\\*/.test(groupLabel));
    const visible = !!(el.offsetParent !== null || el.getClientRects().length);
    return {index: i, tag, type, label: label.replace(/\\s+/g,' ').trim(),
            groupLabel: groupLabel, value: el.value == null ? '' : String(el.value),
            checked: !!el.checked, options, required, visible,
            name: el.name || '', id: el.id || ''};
  });
}
"""

SKIP_TYPES = {"hidden", "submit", "button", "image", "reset"}


# ── Pure logic (unit-tested without a browser) ───────────────────────

def _submit_decision(settings: dict | None) -> str:
    """THE dry-run gate. Returns 'submit' or 'dry_run'.

    Fails safe: anything other than an explicit, parseable `dry_run: false`
    results in 'dry_run'. This is the only place in the package that authorises
    clicking Submit.
    """
    return "dry_run" if sess.is_dry_run(settings) else "submit"


def remaining_quota(cap: int, sent_today: int) -> int:
    """Applications still allowed today. Never negative."""
    try:
        return max(0, int(cap) - int(sent_today))
    except (TypeError, ValueError):
        return 0


def classify_button(label: str) -> str | None:
    """'submit' | 'next' | None, from a footer button's label."""
    text = (label or "").strip().lower()
    if not text:
        return None
    for want in SUBMIT_LABELS:
        if want in text:
            return "submit"
    for want in NEXT_LABELS:
        if want in text:
            return "next"
    return None


def build_field_specs(descriptors: list[dict]) -> list[tuple[list[int], FieldSpec]]:
    """Turn raw DOM descriptors into FieldSpecs.

    Radio buttons sharing a `name` collapse into one question whose options are
    the individual radio labels — otherwise we'd treat "Yes" and "No" as two
    separate questions and answer neither correctly.
    Returns (element indices, spec) so the caller can act on the right node.
    """
    specs: list[tuple[list[int], FieldSpec]] = []
    radio_groups: dict[str, list[dict]] = {}

    for d in descriptors:
        dtype = (d.get("type") or "").lower()
        if dtype in SKIP_TYPES:
            continue
        if not d.get("visible", True) and dtype != "file":
            continue
        if dtype == "radio":
            radio_groups.setdefault(d.get("name") or d.get("id") or f"__{d['index']}",
                                    []).append(d)
            continue

        kind = {"textarea": "textarea", "select": "select", "checkbox": "checkbox",
                "file": "file", "number": "number", "tel": "text", "email": "text",
                "url": "text"}.get(dtype, "text")
        specs.append(([d["index"]], FieldSpec(
            label=d.get("label") or d.get("groupLabel") or d.get("name") or "",
            kind=kind,
            options=tuple(d.get("options") or ()),
            value=d.get("value") or "",
            required=bool(d.get("required")),
            name=d.get("name") or "",
            element_id=d.get("id") or "",
        )))

    for members in radio_groups.values():
        first = members[0]
        options = tuple((m.get("label") or m.get("value") or "").strip() for m in members)
        checked = next((m for m in members if m.get("checked")), None)
        specs.append(([m["index"] for m in members], FieldSpec(
            label=first.get("groupLabel") or first.get("label") or first.get("name") or "",
            kind="radio",
            options=options,
            value=(checked.get("label") or checked.get("value") or "") if checked else "",
            required=any(bool(m.get("required")) for m in members),
            name=first.get("name") or "",
            element_id=first.get("id") or "",
        )))
    return specs


def select_candidates(conn: sqlite3.Connection, settings: dict | None,
                      limit: int | None = None) -> list[sqlite3.Row]:
    """Shortlisted, well-scored, LinkedIn-hosted jobs with no application row yet."""
    cfg = sess.linkedin_cfg(settings)
    sql = (
        "SELECT j.* FROM jobs j "
        "LEFT JOIN applications a ON a.job_id = j.id AND a.channel = ? "
        "WHERE j.status = 'shortlisted' "
        "  AND COALESCE(j.match_score, 0) >= ? "
        "  AND j.url LIKE '%linkedin.com/jobs/%' "
        "  AND a.id IS NULL "
        "ORDER BY COALESCE(j.match_score, 0) DESC, j.collected_at DESC"
    )
    params: list = [CHANNEL, int(cfg["min_score_to_apply"])]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def find_master_cv(root: Path = ROOT) -> str | None:
    """Fallback resume: the master CV PDF sitting in the project root."""
    pdfs = sorted(root.glob("*.pdf"))
    scored = []
    for p in pdfs:
        name = p.name.lower()
        if "portfolio" in name:
            continue
        score = (2 if ("cv" in name or "resume" in name) else 0)
        scored.append((score, p))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1].name))
    return str(scored[0][1])


def resolve_resume_path(conn: sqlite3.Connection, job_id: str,
                        root: Path = ROOT) -> str | None:
    """Tailored resume for this job if another agent produced one, else master CV."""
    try:
        row = latest_resume_for_job(conn, job_id)
    except Exception:  # noqa: BLE001 — table may not be populated yet
        row = None
    if row is not None:
        try:
            path = row["pdf_path"]
        except (IndexError, KeyError):
            path = None
        if path and Path(path).exists():
            return str(path)
    return find_master_cv(root)


# ── Browser helpers ──────────────────────────────────────────────────

def _first_visible(scope, selectors) -> object | None:
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def find_modal(page):
    """Locate the Easy Apply modal, tolerating LinkedIn's several dialog shells."""
    for sel in MODAL:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _footer_buttons(modal) -> list[tuple[object, str, str]]:
    """(locator, label, kind) for every actionable button in the modal."""
    out = []
    try:
        buttons = modal.locator("button")
        for i in range(min(buttons.count(), 30)):
            btn = buttons.nth(i)
            try:
                if not btn.is_visible():
                    continue
                label = (btn.get_attribute("aria-label") or "").strip() or \
                        (btn.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            kind = classify_button(label)
            if kind:
                out.append((btn, label, kind))
    except Exception:  # noqa: BLE001
        pass
    return out


def _scrape_fields(modal) -> list[dict]:
    try:
        return modal.evaluate(_FIELD_SCRAPER) or []
    except Exception:  # noqa: BLE001
        return []


def _controls(modal):
    return modal.locator("input, select, textarea")


def dismiss_modal(page, settings: dict | None, log=print) -> None:
    """Close the modal and discard the draft — used for dry runs and refusals."""
    try:
        btn = _first_visible(page, DISMISS_BUTTON)
        if btn:
            btn.click(timeout=8_000)
            human_delay(settings, scale=0.3)
        # LinkedIn asks "Save this application?" — always Discard, never Save,
        # so a dry run leaves nothing behind in the user's account.
        for sel in DISCARD_BUTTON:
            try:
                d = page.locator(sel).first
                if d.count() and d.is_visible():
                    d.click(timeout=5_000)
                    human_delay(settings, scale=0.3)
                    return
            except Exception:  # noqa: BLE001
                continue
        try:
            page.get_by_role("button", name="Discard").first.click(timeout=4_000)
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001 — closing is best-effort
        log(f"    (could not cleanly dismiss the modal: {type(e).__name__}: {e})")


def _apply_answer(modal, indices: list[int], spec: FieldSpec, ans: Answer,
                  settings: dict | None, log=print) -> bool:
    """Write one answer into the DOM. Returns True if something was set."""
    if ans.value is None or ans.source in ("prefilled", "skipped:optional-blank",
                                           "skipped:optional-checkbox",
                                           "handled:file-upload"):
        return False
    controls = _controls(modal)
    try:
        if spec.kind == "select":
            el = controls.nth(indices[0])
            try:
                el.select_option(label=ans.value, timeout=8_000)
            except Exception:  # noqa: BLE001 — some selects only match by value
                el.select_option(ans.value, timeout=8_000)
        elif spec.kind == "radio":
            target = ans.value.strip().lower()
            for pos, idx in enumerate(indices):
                option = (spec.options[pos] if pos < len(spec.options) else "").strip().lower()
                if option == target:
                    controls.nth(idx).check(timeout=8_000, force=True)
                    break
            else:
                return False
        else:
            el = controls.nth(indices[0])
            sess.jitter_type(el, ans.value, settings)
            # City/company fields are typeaheads: accept the first suggestion so
            # LinkedIn stores a real entity rather than free text.  UNVERIFIED.
            try:
                if "city" in spec.label.lower() or "location" in spec.label.lower():
                    el.press("ArrowDown")
                    el.press("Enter")
            except Exception:  # noqa: BLE001
                pass
        return True
    except Exception as e:  # noqa: BLE001
        log(f"    (could not set {spec.label!r}: {type(e).__name__}: {e})")
        return False


def _upload_resume(modal, resume_path: str | None, settings: dict | None,
                   log=print) -> bool:
    if not resume_path or not Path(resume_path).exists():
        return False
    try:
        inputs = modal.locator(FILE_INPUT)
        if not inputs.count():
            return False
        inputs.first.set_input_files(resume_path, timeout=20_000)
        human_delay(settings, scale=0.5, why="resume upload", log=log)
        log(f"    ↑ attached resume: {Path(resume_path).name}")
        return True
    except Exception as e:  # noqa: BLE001 — a pre-attached resume is acceptable
        log(f"    (resume upload skipped: {type(e).__name__}: {e})")
        return False


def _untick_follow_company(modal, log=print) -> None:
    """Easy Apply silently opts you into following the company. Undo that."""
    try:
        box = modal.locator(FOLLOW_COMPANY_CHECKBOX).first
        if box.count() and box.is_checked():
            box.uncheck(timeout=5_000, force=True)
            log("    · unticked 'follow company'")
    except Exception:  # noqa: BLE001
        pass


# ── The modal walk ───────────────────────────────────────────────────

@dataclass
class ApplyContext:
    settings: dict | None
    answerer: ProfileAnswerer
    resume_path: str | None = None
    log: object = print
    answered: list = field(default_factory=list)   # [(FieldSpec, Answer)]
    steps: int = 0
    resume_uploaded: bool = False


def walk_modal(page, modal, ctx: ApplyContext) -> dict:
    """Step through the Easy Apply modal.

    Returns one of:
      {'outcome': 'dry_run',  'payload': [...]}     — filled but NOT submitted
      {'outcome': 'submitted','payload': [...]}
      {'outcome': 'needs_manual', 'question': str, 'reason': str}
      {'outcome': 'stuck', 'reason': str}
    """
    log = ctx.log
    for step in range(MAX_MODAL_STEPS):
        ctx.steps = step + 1
        sess.guard(page, log=log)

        if not ctx.resume_uploaded:
            ctx.resume_uploaded = _upload_resume(modal, ctx.resume_path, ctx.settings, log=log)
        _untick_follow_company(modal, log=log)

        descriptors = _scrape_fields(modal)
        for indices, spec in build_field_specs(descriptors):
            if spec.kind == "file":
                continue
            ans = ctx.answerer.answer(spec)
            ctx.answered.append((spec, ans))
            if not ans.ok:
                log(f"    ✋ cannot answer: {spec.label!r} — {ans.reason}")
                return {"outcome": "needs_manual", "question": spec.label,
                        "reason": ans.reason or "unanswerable"}
            if _apply_answer(modal, indices, spec, ans, ctx.settings, log=log):
                log(f"    ✎ {spec.label!r} ← {ans.value!r} [{ans.source}]")
                human_delay(ctx.settings, scale=0.25)

        buttons = _footer_buttons(modal)
        submit = next((b for b in buttons if b[2] == "submit"), None)
        nxt = next((b for b in buttons if b[2] == "next"), None)

        if submit is not None:
            payload = summarise_payload(ctx.answered)
            if _submit_decision(ctx.settings) == "dry_run":
                # ── THE GATE ── nothing below this branch touches submit.
                log("\n    ┌─ DRY RUN — application NOT submitted ─────────────")
                log(f"    │ resume : {ctx.resume_path or '(none attached)'}")
                for item in payload:
                    if item["answer"] is not None or item["source"].startswith("skipped"):
                        log(f"    │ {item['question']!r} = {item['answer']!r} "
                            f"[{item['source']}]")
                log(f"    │ would have clicked: {submit[1]!r}")
                log("    └───────────────────────────────────────────────────")
                return {"outcome": "dry_run", "payload": payload}

            log(f"    ▶ submitting ({submit[1]!r})")
            human_delay(ctx.settings, why="before submit", log=log)
            submit[0].click(timeout=15_000)
            human_delay(ctx.settings, scale=0.6, why="after submit", log=log)
            sess.guard(page, log=log)
            return {"outcome": "submitted", "payload": payload}

        if nxt is None:
            return {"outcome": "stuck",
                    "reason": "no Next/Review/Submit button found in the modal"}

        log(f"    → {nxt[1]!r}")
        nxt[0].click(timeout=15_000)
        human_delay(ctx.settings, why="between modal steps", log=log)

        # An error banner means LinkedIn rejected something we filled — do not
        # keep hammering Next.
        try:
            errs = modal.locator(
                ".artdeco-inline-feedback--error, .fb-dash-form-element__error-text")
            if errs.count():
                msg = (errs.first.inner_text() or "").strip()
                return {"outcome": "needs_manual",
                        "question": msg or "form validation error",
                        "reason": f"LinkedIn rejected the form: {msg[:200]}"}
        except Exception:  # noqa: BLE001
            pass

    return {"outcome": "stuck", "reason": f"still in the modal after {MAX_MODAL_STEPS} steps"}


# ── Public API ───────────────────────────────────────────────────────

def apply_to_job(context, conn: sqlite3.Connection, job_row, settings: dict | None,
                 profile: dict | None = None, log=print) -> dict:
    """Apply to one job via Easy Apply.

    Returns a result dict:
        {'job_id', 'title', 'company', 'url', 'status', 'reason', 'payload', 'steps'}
    where status ∈ {'dry_run', 'sent', 'failed', 'skipped'}.

    Records to the DB:
      dry_run → applications.status = 'drafted'
      sent    → 'sent' (via upsert_application + mark_application_sent)
      failed  → 'failed' with the reason (this is the manual-attention queue)
    """
    job_id = job_row["id"]
    url = job_row["url"]
    title = job_row["title"]
    company = job_row["company"]
    result = {"job_id": job_id, "title": title, "company": company, "url": url,
              "status": "failed", "reason": "", "payload": [], "steps": 0}

    if profile is None:
        profile = load_profile_dict()
    answerer = ProfileAnswerer(profile or {})
    resume_path = resolve_resume_path(conn, job_id)
    ctx = ApplyContext(settings=settings, answerer=answerer,
                       resume_path=resume_path, log=log)

    page = context.new_page()
    try:
        log(f"\n  ▸ {title} @ {company}")
        log(f"    {url}")
        sess.goto(page, url, settings, log=log)
        page.wait_for_timeout(2000)

        btn = _first_visible(page, EASY_APPLY_BUTTON)
        btn_text = ""
        if btn is not None:
            try:
                btn_text = (btn.inner_text() or "") + " " + (btn.get_attribute("aria-label") or "")
            except Exception:  # noqa: BLE001
                pass
        if btn is None or "easy apply" not in btn_text.lower():
            reason = "no Easy Apply button (external ATS application)"
            log(f"    ✗ {reason}")
            upsert_application(conn, job_id, CHANNEL, "failed", resume_path=resume_path,
                               error=f"needs manual answer: {reason}")
            result.update(status="failed", reason=reason)
            return result

        human_delay(settings, why="before opening the modal", log=log)
        btn.click(timeout=15_000)
        human_delay(settings, scale=0.6, why="modal open", log=log)
        sess.guard(page, log=log)

        modal = find_modal(page)
        if modal is None:
            reason = "Easy Apply modal did not open"
            log(f"    ✗ {reason}")
            upsert_application(conn, job_id, CHANNEL, "failed", resume_path=resume_path,
                               error=f"needs manual answer: {reason}")
            result.update(status="failed", reason=reason)
            return result

        walked = walk_modal(page, modal, ctx)
        result["steps"] = ctx.steps
        outcome = walked["outcome"]

        if outcome == "dry_run":
            result.update(status="dry_run", payload=walked["payload"],
                          reason="dry_run — not submitted")
            upsert_application(
                conn, job_id, CHANNEL, "drafted", resume_path=resume_path,
                subject=f"Easy Apply — {title} @ {company}",
                body=json.dumps(walked["payload"], indent=2)[:20000],
                error="dry_run: filled but not submitted")
            dismiss_modal(page, settings, log=log)
            return result

        if outcome == "submitted":
            app_id = upsert_application(
                conn, job_id, CHANNEL, "sent", resume_path=resume_path,
                subject=f"Easy Apply — {title} @ {company}",
                body=json.dumps(walked["payload"], indent=2)[:20000])
            mark_application_sent(conn, app_id)
            log("    ✔ submitted")
            result.update(status="sent", payload=walked["payload"],
                          reason="submitted")
            return result

        if outcome == "needs_manual":
            reason = f"needs manual answer: {walked.get('question', '?')}"
            detail = walked.get("reason") or ""
            upsert_application(conn, job_id, CHANNEL, "failed", resume_path=resume_path,
                               error=f"{reason} ({detail})"[:500])
            dismiss_modal(page, settings, log=log)
            result.update(status="failed", reason=reason)
            return result

        reason = walked.get("reason", "unknown modal state")
        upsert_application(conn, job_id, CHANNEL, "failed", resume_path=resume_path,
                           error=f"needs manual answer: {reason}")
        dismiss_modal(page, settings, log=log)
        result.update(status="failed", reason=reason)
        return result

    except BlockerDetected:
        raise
    except Exception as e:  # noqa: BLE001 — one bad posting must not kill the batch
        reason = f"{type(e).__name__}: {e}"
        log(f"    ✗ {reason}")
        try:
            upsert_application(conn, job_id, CHANNEL, "failed", resume_path=resume_path,
                               error=f"needs manual answer: automation error — {reason}"[:500])
        except Exception:  # noqa: BLE001
            pass
        result.update(status="failed", reason=reason)
        return result
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def load_profile_dict() -> dict:
    """Master profile as a plain dict (the answerer's only source of truth)."""
    import yaml

    from ..profile import load_profile_for_forms
    try:
        return load_profile_for_forms()
    except FileNotFoundError:
        return {}


def apply_batch(conn: sqlite3.Connection, settings: dict | None,
                limit: int | None = None, log=print) -> dict:
    """Apply to every eligible shortlisted LinkedIn job, within the daily cap.

    Returns a summary dict:
        {'considered', 'attempted', 'sent', 'dry_run', 'failed',
         'cap', 'sent_today', 'quota', 'dry_run_mode', 'aborted', 'results'}
    """
    cfg = sess.linkedin_cfg(settings)
    dry = _submit_decision(settings) == "dry_run"

    sent_today = sent_today_count(conn, CHANNEL)
    quota = remaining_quota(cfg["daily_apply_cap"], sent_today)
    summary = {"considered": 0, "attempted": 0, "sent": 0, "dry_run": 0, "failed": 0,
               "cap": cfg["daily_apply_cap"], "sent_today": sent_today,
               "quota": quota, "dry_run_mode": dry, "aborted": None, "results": []}

    candidates = select_candidates(conn, settings, limit=limit)
    summary["considered"] = len(candidates)

    log(f"\nLinkedIn Easy Apply — {'DRY RUN (nothing will be submitted)' if dry else '*** LIVE ***'}")
    log(f"  daily cap {cfg['daily_apply_cap']} · already sent today {sent_today} · quota {quota}")
    log(f"  min score {cfg['min_score_to_apply']} · candidates {len(candidates)}")

    if not candidates:
        log("  Nothing to apply to. Run `collect` + `match` first.")
        return summary

    # In a dry run the cap still bounds the work, so a rehearsal looks exactly
    # like the real thing.
    budget = quota if quota > 0 else 0
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
        log("  ! No master profile found — every screening question will be refused.")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser, context = sess.launch(settings, pw)
        try:
            sess.ensure_logged_in(context, settings, log=log)
            for i, row in enumerate(targets, 1):
                log(f"\n[{i}/{len(targets)}]")
                try:
                    res = apply_to_job(context, conn, row, settings, profile=profile, log=log)
                except BlockerDetected as e:
                    summary["aborted"] = str(e)
                    log("  ✖ Run aborted — LinkedIn challenge detected. "
                        "Nothing further will be attempted.")
                    break
                summary["results"].append(res)
                summary["attempted"] += 1
                if res["status"] == "sent":
                    summary["sent"] += 1
                elif res["status"] == "dry_run":
                    summary["dry_run"] += 1
                else:
                    summary["failed"] += 1

                if i < len(targets):
                    human_delay(settings, scale=1.5, why="between applications", log=log)
        finally:
            try:
                context.close()
            finally:
                browser.close()

    log(f"\n✔ Done — attempted {summary['attempted']}, sent {summary['sent']}, "
        f"dry-run {summary['dry_run']}, needs-manual/failed {summary['failed']}")
    if summary["failed"]:
        log("  Review the manual queue:  SELECT * FROM applications "
            "WHERE channel='linkedin' AND status='failed';")
    return summary
