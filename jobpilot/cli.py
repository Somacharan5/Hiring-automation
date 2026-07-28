"""JobPilot CLI.

    python -m jobpilot init-profile <resume.pdf|docx>   parse resume → master profile
    python -m jobpilot collect [--no-jobspy]            pull jobs from all sources
    python -m jobpilot match                            hard-filter + LLM-score new jobs
    python -m jobpilot report [--min-score N]           show shortlisted roles
    python -m jobpilot run [--no-jobspy]                collect + match + report
    python -m jobpilot tailor [--job-id ID]             tailor resume for job(s)
    python -m jobpilot outreach-compose                 draft emails & find contacts
    python -m jobpilot outreach-approve [--all]         approve drafted emails
    python -m jobpilot outreach-send                    send approved emails
    python -m jobpilot linkedin-search                  search LinkedIn for Easy Apply jobs
    python -m jobpilot linkedin-apply                   apply to shortlisted LinkedIn jobs
    python -m jobpilot portal-apply [--live]            apply via company ATS forms
    python -m jobpilot find-contacts [COMPANY]          discover + verify recruiter emails
    python -m jobpilot review-queue                     addresses needing manual approval
    python -m jobpilot scan-bounces                     fold bounce-backs into confidence
    python -m jobpilot dashboard                        start the monitoring dashboard

Channels, by volume and cost:
    portal-apply   ATS forms  — free, unlimited, lands in their real pipeline
    linkedin-apply Easy Apply — free, session-based
    outreach-send  cold email — reserved for high-score roles; needs a verified address
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from . import db
from .collectors import ats, boards, jobspy_collector
from .matching import hard_filter

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
COMPANIES_PATH = ROOT / "config" / "companies.yaml"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


# ── Commands ─────────────────────────────────────────────────────────

def cmd_init_profile(args) -> None:
    from .profile import parse_resume, save_profile, PROFILE_PATH

    resume = Path(args.resume).expanduser()
    if not resume.exists():
        sys.exit(f"Resume not found: {resume}")
    from .llm import active_provider

    print(f"Parsing {resume.name} with {active_provider()}…")
    profile = parse_resume(resume)
    save_profile(profile)
    print(f"✔ Master profile saved to {PROFILE_PATH}")
    print(f"  {profile.name} — {profile.headline or 'no headline'}")
    print(f"  {len(profile.experiences)} roles, {len(profile.skills)} skills, "
          f"~{profile.years_experience or '?'} years experience")
    print("  Review/edit the YAML — the matcher scores against it verbatim.")


def cmd_collect(args) -> None:
    settings = load_yaml(SETTINGS_PATH)
    companies = load_yaml(COMPANIES_PATH)
    keywords = [k.lower() for k in settings["hard_filter"]["title_must_contain_any"]]
    search = settings["search"]

    conn = db.connect()
    total_new = 0

    print("Collecting from ATS boards…")
    jobs = ats.collect_all(companies, keywords)
    total_new += db.upsert_jobs(conn, jobs)

    print("Collecting from remote boards…")
    total_new += db.upsert_jobs(conn, boards.remotive(keywords))
    total_new += db.upsert_jobs(conn, boards.remoteok(keywords))
    adz = settings.get("adzuna", {})
    total_new += db.upsert_jobs(conn, boards.adzuna(adz.get("app_id", ""), adz.get("app_key", ""), keywords))

    if not args.no_jobspy:
        print("Collecting via JobSpy (LinkedIn/Indeed/Google — this is the slow part)…")
        jobs = jobspy_collector.collect(
            terms=search["terms"], locations=search["locations"],
            results_wanted=search["results_per_source"], hours_old=search["hours_old"],
            include_remote=search["include_remote"],
        )
        total_new += db.upsert_jobs(conn, jobs)

    print(f"\n✔ Collected — {total_new} new jobs added. Totals: {db.counts_by_status(conn)}")


def cmd_match(args) -> None:
    from .matching.llm_scorer import Scorer
    from .profile import load_profile_for_matching

    settings = load_yaml(SETTINGS_PATH)
    m_cfg = settings["matching"]
    # Mirror the work-auth gate (lives under `matching`) into the hard-filter cfg
    # so screened-out visa-blocked roles never reach the LLM.
    hf_cfg = {**settings["hard_filter"],
              "skip_hard_work_auth": m_cfg.get("skip_hard_work_auth", False)}
    conn = db.connect()

    # Stage 1: hard filter every 'new' job
    new_jobs = db.jobs_with_status(conn, "new")
    passed = 0
    for row in new_jobs:
        reason = hard_filter.check(row["title"], row["description"], hf_cfg)
        if reason:
            db.set_status(conn, row["id"], "rejected", reason)
        else:
            db.set_status(conn, row["id"], "screened")
            passed += 1
    print(f"Hard filter: {passed}/{len(new_jobs)} new jobs passed")

    # Stage 2: LLM scoring, capped per run
    to_score = db.jobs_with_status(conn, "screened", limit=m_cfg["max_jobs_per_run"])
    if not to_score:
        print("Nothing to score.")
        return

    try:
        profile_yaml = load_profile_for_matching()  # contact details stripped
    except FileNotFoundError as e:
        sys.exit(f"\n{e}")
    scorer = Scorer(profile_yaml, model=m_cfg["model"])
    threshold = m_cfg["shortlist_threshold"]
    print(f"Scoring {len(to_score)} jobs with {m_cfg['model']} (threshold {threshold})…")

    for row in to_score:
        try:
            v = scorer.score(row["company"], row["title"], row["location"], row["description"])
        except Exception as e:  # noqa: BLE001 — keep scoring the rest
            print(f"  ✗ {row['company']} — {row['title']}: {type(e).__name__}: {e}")
            continue
        shortlisted = v.score >= threshold and not v.hard_blockers
        db.set_match(conn, row["id"], v.score, v.model_dump(), shortlisted)
        mark = "★" if shortlisted else " "
        print(f"  {mark} {v.score:3d}  {row['company']} — {row['title']}  ({v.verdict})")

    print(f"\n✔ Done. Totals: {db.counts_by_status(conn)}")


def cmd_report(args) -> None:
    import json

    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'shortlisted' AND match_score >= ? "
        "ORDER BY match_score DESC", (args.min_score,)
    ).fetchall()
    if not rows:
        print("No shortlisted jobs yet. Run: python -m jobpilot run")
        return

    print(f"\n{'═' * 70}\n SHORTLIST — {len(rows)} roles\n{'═' * 70}")
    for r in rows:
        v = json.loads(r["match_json"] or "{}")
        print(f"\n  [{r['match_score']}] {r['title']} @ {r['company']}")
        print(f"      {r['location'] or '?'}  ·  {r['source']}")
        print(f"      {r['url']}")
        if v.get("reasoning"):
            print(f"      Why: {v['reasoning']}")
        if v.get("tailoring_hints"):
            print(f"      Tailor: {'; '.join(v['tailoring_hints'][:3])}")


def cmd_run(args) -> None:
    cmd_collect(args)
    cmd_match(args)
    args.min_score = 0
    cmd_report(args)

def cmd_tailor(args) -> None:
    from .tailor import tailor_and_render
    from .db import connect, latest_resume_for_job, jobs_with_status
    
    conn = connect()
    settings = load_yaml(SETTINGS_PATH)
    
    if args.job_id:
        print(f"Tailoring resume for job {args.job_id}...")
        try:
            res = tailor_and_render(conn, args.job_id, settings)
            print(f"✔ Tailored resume saved:\n  PDF:  {res.pdf_path or 'none'}\n  DOCX: {res.docx_path or 'none'}")
            print(f"  ATS Score: {res.ats_score}")
            if res.warnings:
                print("  Warnings:")
                for w in res.warnings:
                    print(f"    - {w}")
        except Exception as e:
            sys.exit(f"✗ Tailoring failed: {e}")
    else:
        # tailor all shortlisted jobs that don't have a tailored resume yet
        shortlisted = jobs_with_status(conn, "shortlisted")
        to_tailor = []
        for r in shortlisted:
            existing = latest_resume_for_job(conn, r["id"])
            if not existing or args.force:
                to_tailor.append(r)
        
        if not to_tailor:
            print("No new shortlisted jobs to tailor.")
            return
            
        print(f"Found {len(to_tailor)} shortlisted jobs to tailor...")
        success, failed = 0, 0
        for r in to_tailor:
            print(f"▸ Tailoring for {r['company']} — {r['title']}...")
            try:
                res = tailor_and_render(conn, r["id"], settings)
                print(f"  ✔ ATS Score: {res.ats_score}")
                success += 1
            except Exception as e:
                print(f"  ✗ Failed: {e}")
                failed += 1
        print(f"\n✔ Done. Success: {success}, Failed: {failed}")


def cmd_outreach_compose(args) -> None:
    from .outreach import compose_for_shortlist, load_settings
    conn = db.connect()
    settings = load_settings(SETTINGS_PATH)
    print("Drafting outreach emails for shortlisted jobs...")
    results = compose_for_shortlist(conn, settings, limit=args.limit, min_score=args.min_score)
    drafted_count = len([r for r in results if "error" not in r])
    print(f"\n✔ Finished compose run. Drafted for {drafted_count} roles.")


def cmd_outreach_approve(args) -> None:
    from .outreach import approve, approve_all
    conn = db.connect()
    if args.all:
        count = approve_all(conn, min_score=args.min_score)
        print(f"✔ Approved {count} drafted emails.")
    elif args.id:
        if approve(conn, args.id):
            print(f"✔ Approved draft #{args.id}.")
        else:
            sys.exit(f"✗ Failed to approve draft #{args.id}.")
    else:
        sys.exit("✗ Either --all or --id must be specified.")


def cmd_outreach_send(args) -> None:
    from .outreach import send_pending, load_settings
    conn = db.connect()
    settings = load_settings(SETTINGS_PATH)
    print("Sending pending approved outreach emails...")
    report = send_pending(conn, settings, limit=args.limit)
    if report["dry_run"]:
        print("\n✔ Run complete (DRY RUN). No emails were sent.")
    else:
        print(f"\n✔ Run complete. Sent: {report['sent']} | Failed: {len(report['failed'])} | Skipped: {len(report['skipped'])}")


def cmd_linkedin_search(args) -> None:
    from .linkedin import search_and_store
    conn = db.connect()
    settings = load_yaml(SETTINGS_PATH)
    print("Searching for jobs on LinkedIn via Playwright UI...")
    search_and_store(conn, settings, limit_per_search=args.limit)


def cmd_linkedin_apply(args) -> None:
    from .linkedin import apply_batch
    conn = db.connect()
    settings = load_yaml(SETTINGS_PATH)
    print("Applying to eligible LinkedIn jobs...")
    apply_batch(conn, settings, limit=args.limit)


def cmd_dashboard(args) -> None:
    from .dashboard import run, dashboard_settings
    cfg = dashboard_settings(SETTINGS_PATH)
    host = args.host or cfg["host"]
    port = args.port or cfg["port"]
    print(f"Starting JobPilot Dashboard on http://{host}:{port}...")
    run(host=host, port=port, reload=args.reload)


def cmd_portal_apply(args) -> None:
    """Apply through company ATS forms — the highest-volume free channel."""
    from playwright.sync_api import sync_playwright

    from .linkedin.session import launch
    from .portal.apply import apply_batch, apply_to_job

    settings = load_yaml(SETTINGS_PATH)
    cfg = settings.setdefault("portal", {})
    # Dry-run unless --live is passed, regardless of what the config says. Real
    # applications carry the user's name; they should never start by accident.
    cfg["dry_run"] = not args.live
    if args.live:
        print("⚠  LIVE MODE — applications will actually be submitted.\n")
    else:
        print("Dry-run: forms are filled but nothing is submitted. Use --live to submit.\n")

    conn = db.connect()
    with sync_playwright() as pw:
        browser, ctx = launch(cfg, pw)
        try:
            if args.job_id:
                job = db.get_job(conn, args.job_id)
                if not job:
                    sys.exit(f"No job with id {args.job_id}")
                result = apply_to_job(ctx, conn, job, settings)
                print(f"\n{result.get('status')}: {result.get('reason') or ''}")
            else:
                summary = apply_batch(conn, settings, limit=args.limit)
                print(f"\n✔ {summary}")
        finally:
            browser.close()


def cmd_find_contacts(args) -> None:
    """Discover and verify recruiter addresses — free sources first."""
    from .outreach.discovery import discover_and_verify

    settings = load_yaml(SETTINGS_PATH)
    conn = db.connect()

    if args.company:
        companies = [args.company]
    else:
        companies = [r["company"] for r in conn.execute(
            "SELECT DISTINCT company FROM jobs WHERE status = 'shortlisted'")]
        if not companies:
            sys.exit("No shortlisted jobs. Run: python -m jobpilot run")

    for company in companies:
        print(f"\n▸ {company}")
        try:
            found = discover_and_verify(conn, company, settings=settings,
                                        use_smtp=not args.no_smtp)
        except Exception as e:  # noqa: BLE001 — one company must not kill the sweep
            print(f"  ✗ {type(e).__name__}: {e}")
            continue
        if not found:
            print("  (nothing found)")
        for c in found[:8]:
            mark = "✔" if c.get("auto_sendable") else "·"
            print(f"  {mark} {c['email']:38s} conf={c['confidence']:3d} "
                  f"{c.get('tier',''):10s} {c.get('source','')}")


def cmd_review_queue(args) -> None:
    """Addresses that need a human look before they may be used."""
    from .outreach.discovery import review_queue

    conn = db.connect()
    rows = review_queue(conn, args.company, settings=load_yaml(SETTINGS_PATH))
    if not rows:
        print("Review queue empty — every known address is either auto-sendable or dead.")
        return
    print(f"\n{len(rows)} address(es) awaiting review "
          f"(catch-all domains and unverified guesses land here):\n")
    for r in rows:
        print(f"  {r['email']:40s} {r['company']:16s} conf={r['confidence']:3d}  {r.get('reason','')}")


def cmd_scan_bounces(args) -> None:
    """Fold bounce-backs into contact confidence so we stop repeating misses."""
    from .outreach.bounce import apply_bounce_feedback, scan_bounces

    conn = db.connect()
    try:
        bounces = scan_bounces(since_days=args.days)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Bounce scan failed: {type(e).__name__}: {e}")
    if not bounces:
        print("No bounces found — nothing to downgrade.")
        return
    result = apply_bounce_feedback(conn, bounces)
    print(f"✔ {len(bounces)} bounce(s) processed: {result}")


def cmd_pending_answers(args) -> None:
    """Questions that blocked applications — answer once in config/answers.yaml."""
    import re as _re
    from collections import Counter

    from .profile import ANSWERS_PATH, load_answer_overrides

    conn = db.connect()
    rows = conn.execute(
        "SELECT a.error, j.company FROM applications a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.status = 'failed' AND a.error LIKE '%manual answer%'"
    ).fetchall()

    known = {_re.sub(r"[^a-z0-9 ]", "", k.lower()).strip() for k in load_answer_overrides()}
    questions: Counter = Counter()
    for r in rows:
        m = _re.search(r"needs manual answer:\s*(.+?)(?:\s*\(|$)", r["error"] or "")
        if not m:
            continue
        q = m.group(1).strip()
        if _re.sub(r"[^a-z0-9 ]", "", q.lower()).strip() not in known:
            questions[q] += 1

    if not questions:
        print("Nothing pending — every blocking question is answered in "
              f"{ANSWERS_PATH.name}.")
        return

    print(f"\n{len(questions)} unanswered question(s) blocking applications.")
    print(f"Add each to {ANSWERS_PATH}, then re-run the apply command.\n")
    for q, n in questions.most_common():
        print(f"  ({n}x)  {q}")
    print("\nFormat (answer only what is true — this is submitted under your name):")
    for q, _ in questions.most_common(3):
        print(f'  {q.rstrip("*")}: ""')


# ── Autonomous email pipeline (Postgres) ─────────────────────────────

def cmd_prepare(args) -> None:
    from .outreach import load_settings
    from .outreach.pipeline import prepare_outreach

    conn = db.connect()
    settings = load_settings(SETTINGS_PATH)
    print("Preparing outreach (discover address → tailor resume → compose email)…")
    results = prepare_outreach(conn, settings, limit=args.limit, min_score=args.min_score)
    ok = len([r for r in results if "error" not in r])
    print(f"\n✔ Prepared {ok}/{len(results)} application(s). Nothing sent — run: jobpilot send")


def cmd_send(args) -> None:
    from .outreach import load_settings
    from .outreach.send import send_pending

    conn = db.connect()
    settings = load_settings(SETTINGS_PATH)
    report = send_pending(conn, settings, limit=args.limit)
    if report["dry_run"] or report.get("first_run_draft"):
        print(f"\nⓘ {report.get('reason', 'dry-run')} — {len(report['would_send'])} email(s) ready:")
        for w in report["would_send"]:
            print(f"   → {w['to'] or '(no address)':32s} {w['company']}  |  {w['subject']}")
    else:
        print(f"\n✔ Sent {report['sent']} · skipped {len(report['skipped'])} · failed {len(report['failed'])}")
        for s in report["skipped"] + report["failed"]:
            print(f"   - {s.get('company')}: {s.get('reason') or s.get('error')}")


def cmd_followups(args) -> None:
    from .outreach import load_settings
    from .outreach.followups import run_followups

    conn = db.connect()
    settings = load_settings(SETTINGS_PATH)
    report = run_followups(conn, settings, limit=args.limit)
    if report["dry_run"]:
        print(f"ⓘ dry-run — {len(report['would_send'])} follow-up(s) due:")
        for w in report["would_send"]:
            print(f"   → {w['company']} (bump #{w['n']}) → {w['to']}")
    else:
        print(f"✔ Follow-ups sent {report['sent']} · skipped {len(report['skipped'])} · failed {len(report['failed'])}")


def cmd_scan_replies(args) -> None:
    from .outreach import load_settings
    from .outreach.inbox_reader import scan_replies

    conn = db.connect()
    settings = load_settings(SETTINGS_PATH)
    print("Scanning inbox for replies…")
    report = scan_replies(conn, settings, since_days=args.days)
    print(f"✔ Scanned {report['scanned']} (watching {report['domains_watched']} domain(s)) → "
          f"{report['new_replies']} new repl(y/ies).")


def cmd_agent_run(args) -> None:
    """The full daily cycle the Oracle VM scheduler runs at 08:00."""
    from .outreach import load_settings
    from .outreach.pipeline import run_outreach_cycle

    settings = load_settings(SETTINGS_PATH)
    conn = db.connect()
    run_id = db.start_run(conn, "agent")
    try:
        cmd_collect(args)
        cmd_match(args)
        report = run_outreach_cycle(conn, settings, limit=args.limit)
        db.finish_run(conn, run_id, status="ok", stats={
            "prepared": report["prepared"],
            "sent": report["send"].get("sent", 0),
            "followups": report["followups"].get("sent", 0),
            "new_replies": report["replies"].get("new_replies", 0),
        })
        print(f"\n✔ Agent cycle complete: {report}")
    except Exception as e:  # noqa: BLE001
        db.finish_run(conn, run_id, status="error", error=f"{type(e).__name__}: {e}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(prog="jobpilot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-profile", help="Parse resume into the master profile")
    p.add_argument("resume", help="Path to resume (.pdf or .docx)")
    p.set_defaults(func=cmd_init_profile)

    p = sub.add_parser("collect", help="Pull jobs from all sources")
    p.add_argument("--no-jobspy", action="store_true", help="Skip LinkedIn/Indeed scraping")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("match", help="Filter + LLM-score collected jobs")
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("report", help="Show shortlisted roles")
    p.add_argument("--min-score", type=int, default=0)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("run", help="collect + match + report")
    p.add_argument("--no-jobspy", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("tailor", help="Tailor resume for shortlisted jobs")
    p.add_argument("--job-id", help="Tailor for a specific job ID")
    p.add_argument("--force", action="store_true", help="Force re-tailoring even if a resume already exists")
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser("outreach-compose", help="Discover contacts and draft outreach emails")
    p.add_argument("--limit", type=int, help="Limit the number of jobs processed")
    p.add_argument("--min-score", type=int, help="Override minimum score threshold")
    p.set_defaults(func=cmd_outreach_compose)

    p = sub.add_parser("outreach-approve", help="Approve drafted emails")
    p.add_argument("--all", action="store_true", help="Approve all eligible drafts")
    p.add_argument("--id", type=int, help="Approve a specific email draft by application ID")
    p.add_argument("--min-score", type=int, default=0, help="Minimum score to approve when using --all")
    p.set_defaults(func=cmd_outreach_approve)

    p = sub.add_parser("outreach-send", help="Send approved emails")
    p.add_argument("--limit", type=int, help="Limit number of emails to send")
    p.set_defaults(func=cmd_outreach_send)

    p = sub.add_parser("linkedin-search", help="Search LinkedIn for Easy Apply jobs")
    p.add_argument("--limit", type=int, default=25, help="Results limit per search term")
    p.set_defaults(func=cmd_linkedin_search)

    p = sub.add_parser("linkedin-apply", help="Apply to shortlisted LinkedIn jobs")
    p.add_argument("--limit", type=int, help="Limit number of applications in this batch")
    p.set_defaults(func=cmd_linkedin_apply)

    p = sub.add_parser("dashboard", help="Start the FastAPI monitoring dashboard")
    p.add_argument("--host", help="Dashboard host IP")
    p.add_argument("--port", type=int, help="Dashboard port")
    p.add_argument("--reload", action="store_true", help="Enable FastAPI hot reload")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("portal-apply", help="Apply via company ATS forms (Greenhouse/Lever/Ashby/…)")
    p.add_argument("--limit", type=int, help="Max applications this run")
    p.add_argument("--job-id", help="Apply to one specific job")
    p.add_argument("--live", action="store_true",
                   help="Actually submit. Without this, runs in dry-run and submits nothing.")
    p.set_defaults(func=cmd_portal_apply)

    p = sub.add_parser("find-contacts", help="Discover + verify recruiter emails (free sources first)")
    p.add_argument("company", nargs="?", help="Company name; omit to sweep all shortlisted")
    p.add_argument("--no-smtp", action="store_true", help="Skip SMTP probing (faster, less certain)")
    p.set_defaults(func=cmd_find_contacts)

    p = sub.add_parser("review-queue", help="Addresses needing manual approval before use")
    p.add_argument("company", nargs="?")
    p.set_defaults(func=cmd_review_queue)

    p = sub.add_parser("pending-answers", help="Questions blocking applications — fill in answers.yaml")
    p.set_defaults(func=cmd_pending_answers)

    p = sub.add_parser("scan-bounces", help="Read bounce-backs from Gmail and mark addresses dead")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_scan_bounces)

    # ── Autonomous email pipeline ────────────────────────────────────
    p = sub.add_parser("prepare", help="Discover address + tailor resume + compose email for shortlisted jobs")
    p.add_argument("--limit", type=int)
    p.add_argument("--min-score", type=int)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("send", help="Send prepared emails (dry-run unless outreach.dry_run: false)")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("followups", help="Send due follow-ups (3-day cadence, capped)")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_followups)

    p = sub.add_parser("scan-replies", help="Read the inbox for replies → dashboard inbox + bell")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_scan_replies)

    p = sub.add_parser("agent-run", help="Full daily cycle: collect → match → prepare → send → followups → replies")
    p.add_argument("--no-jobspy", action="store_true")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_agent_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
