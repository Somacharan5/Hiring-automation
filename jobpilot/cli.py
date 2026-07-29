"""JobPilot CLI — autonomous, email-only PM job-search pipeline (Neon-backed).

    python -m jobpilot init-profile <resume.pdf|docx>   parse resume → matcher profile
    python -m jobpilot collect [--no-jobspy]            pull jobs from ATS + boards
    python -m jobpilot match                            hard-filter + LLM-score new jobs
    python -m jobpilot report [--min-score N]           show shortlisted roles
    python -m jobpilot run [--no-jobspy]                collect + match + report
    python -m jobpilot tailor [--job-id ID]             tailor resume(s) via the kit
    python -m jobpilot prepare                          discover address + tailor + compose
    python -m jobpilot send                             send prepared emails (dry-run by default)
    python -m jobpilot followups                        send due follow-ups
    python -m jobpilot scan-replies                     read inbox → dashboard + bell
    python -m jobpilot agent-run [--no-jobspy]          the full daily cycle
    python -m jobpilot dashboard                        start the monitoring dashboard

Sending is gated by outreach.dry_run in config/settings.yaml — nothing transmits
until it is explicitly false.
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


# ── Discover ─────────────────────────────────────────────────────────

def cmd_init_profile(args) -> None:
    from .llm import active_provider
    from .profile import PROFILE_PATH, parse_resume, save_profile

    resume = Path(args.resume).expanduser()
    if not resume.exists():
        sys.exit(f"Resume not found: {resume}")
    print(f"Parsing {resume.name} with {active_provider()}…")
    profile = parse_resume(resume)
    save_profile(profile)
    print(f"✔ Matcher profile saved to {PROFILE_PATH}")
    print(f"  {profile.name} — {len(profile.experiences)} roles, {len(profile.skills)} skills")


def cmd_collect(args) -> None:
    settings = load_yaml(SETTINGS_PATH)
    companies = load_yaml(COMPANIES_PATH)
    keywords = [k.lower() for k in settings["hard_filter"]["title_must_contain_any"]]
    search = settings["search"]

    conn = db.connect()
    total_new = 0
    print("Collecting from ATS boards…")
    total_new += db.upsert_jobs(conn, ats.collect_all(companies, keywords))
    print("Collecting from remote boards…")
    total_new += db.upsert_jobs(conn, boards.remotive(keywords))
    total_new += db.upsert_jobs(conn, boards.remoteok(keywords))
    total_new += db.upsert_jobs(conn, boards.arbeitnow(keywords))
    total_new += db.upsert_jobs(conn, boards.jobicy(keywords))
    total_new += db.upsert_jobs(conn, boards.himalayas(keywords))
    adz = settings.get("adzuna", {})
    total_new += db.upsert_jobs(conn, boards.adzuna(adz.get("app_id", ""), adz.get("app_key", ""), keywords))

    js = settings.get("jobspy", {}) or {}
    if not args.no_jobspy and js.get("enabled", True):
        print("Collecting via JobSpy (LinkedIn/Indeed/Naukri/Google — the slow part)…")
        total_new += db.upsert_jobs(conn, jobspy_collector.collect(
            terms=js.get("terms") or search["terms"],
            locations=search["locations"],
            results_wanted=js.get("results_wanted", search["results_per_source"]),
            hours_old=search["hours_old"], include_remote=search["include_remote"],
            sites=js.get("sites"), fetch_descriptions=js.get("fetch_descriptions", True),
            naukri_for_india=js.get("naukri_for_india", True),
            proxies=js.get("proxies") or None))

    print(f"\n✔ Collected — {total_new} new jobs. Totals: {db.counts_by_status(conn)}")


# ── Match (hard filter + LLM score) ──────────────────────────────────

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

    to_score = db.jobs_with_status(conn, "screened", limit=m_cfg["max_jobs_per_run"])
    if not to_score:
        print("Nothing to score.")
        return
    try:
        profile_yaml = load_profile_for_matching()
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
        print(f"  {'★' if shortlisted else ' '} {v.score:3d}  {row['company']} — {row['title']}  ({v.verdict})")
    print(f"\n✔ Done. Totals: {db.counts_by_status(conn)}")


def cmd_report(args) -> None:
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'shortlisted' AND match_score >= %s "
        "ORDER BY match_score DESC", (args.min_score,)).fetchall()
    if not rows:
        print("No shortlisted jobs yet. Run: python -m jobpilot run")
        return
    print(f"\n{'═' * 70}\n SHORTLIST — {len(rows)} roles\n{'═' * 70}")
    for r in rows:
        v = r["match_json"] or {}
        print(f"\n  [{r['match_score']}] {r['title']} @ {r['company']}")
        print(f"      {r['location'] or '?'}  ·  {r['source']}\n      {r['url']}")
        if v.get("reasoning"):
            print(f"      Why: {v['reasoning']}")
        if v.get("tailoring_hints"):
            print(f"      Tailor: {'; '.join(v['tailoring_hints'][:3])}")


def cmd_run(args) -> None:
    cmd_collect(args)
    cmd_match(args)
    args.min_score = 0
    cmd_report(args)


# ── Tailor (kit) ─────────────────────────────────────────────────────

def cmd_tailor(args) -> None:
    from .resume.tailor import tailor_and_render

    conn = db.connect()
    settings = load_yaml(SETTINGS_PATH)
    if args.job_id:
        res = tailor_and_render(conn, args.job_id, settings)
        print(f"✔ {res['pdf_path']}")
        for w in res["warnings"]:
            print(f"   ⚠ {w}")
        return
    shortlisted = db.jobs_with_status(conn, "shortlisted")
    todo = [r for r in shortlisted if args.force or not db.latest_resume_for_job(conn, r["id"])]
    if not todo:
        print("No shortlisted jobs to tailor.")
        return
    print(f"Tailoring {len(todo)} shortlisted job(s)…")
    for r in todo:
        print(f"▸ {r['company']} — {r['title']}")
        try:
            res = tailor_and_render(conn, r["id"], settings)
            print(f"  ✔ {res['pdf_path'].split('/')[-1]}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {type(e).__name__}: {e}")


# ── Outreach (email) ─────────────────────────────────────────────────

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
    """The full daily cycle the scheduler runs at 08:00."""
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
            "prepared": report["prepared"], "sent": report["send"].get("sent", 0),
            "followups": report["followups"].get("sent", 0),
            "new_replies": report["replies"].get("new_replies", 0)})
        print(f"\n✔ Agent cycle complete: {report}")
    except Exception as e:  # noqa: BLE001
        db.finish_run(conn, run_id, status="error", error=f"{type(e).__name__}: {e}")
        raise


# ── Dashboard ────────────────────────────────────────────────────────

def cmd_dashboard(args) -> None:
    from .dashboard import dashboard_settings, run

    cfg = dashboard_settings(SETTINGS_PATH)
    host = args.host or cfg["host"]
    port = args.port or cfg["port"]
    print(f"Starting JobPilot Dashboard on http://{host}:{port} …")
    run(host=host, port=port, reload=args.reload)


# ── Parser ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(prog="jobpilot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-profile", help="Parse resume into the matcher profile")
    p.add_argument("resume", help="Path to resume (.pdf or .docx)")
    p.set_defaults(func=cmd_init_profile)

    p = sub.add_parser("collect", help="Pull jobs from ATS + boards")
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

    p = sub.add_parser("tailor", help="Tailor resume(s) for shortlisted jobs via the kit")
    p.add_argument("--job-id", help="Tailor one specific job ID")
    p.add_argument("--force", action="store_true", help="Re-tailor even if a resume exists")
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser("prepare", help="Discover address + tailor resume + compose email")
    p.add_argument("--limit", type=int)
    p.add_argument("--min-score", type=int)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("send", help="Send prepared emails (dry-run unless outreach.dry_run: false)")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("followups", help="Send due follow-ups (3-day cadence, capped)")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_followups)

    p = sub.add_parser("scan-replies", help="Read the inbox for replies → dashboard + bell")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_scan_replies)

    p = sub.add_parser("agent-run", help="Full daily cycle: collect → match → prepare → send → followups → replies")
    p.add_argument("--no-jobspy", action="store_true")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_agent_run)

    p = sub.add_parser("dashboard", help="Start the FastAPI monitoring dashboard")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_dashboard)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
