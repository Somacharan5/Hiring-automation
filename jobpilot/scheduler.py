"""The always-on agent loop — APScheduler. This is what the Oracle VM runs 24/7.

    python -m jobpilot.scheduler

Schedules, from config/settings.yaml `schedule`:
  • daily at scrape_at (08:00 IST)  — full cycle: collect → match → prepare → send
                                       → follow-ups → scan replies
  • every followup_every_hours      — send any due follow-ups
  • every reply_scan_every_hours    — read the inbox for replies (→ bell)

Every job is wrapped so one failure never kills the loop; failures are logged to
agent_runs (visible on the dashboard Health page). Sending stays gated by
outreach.dry_run — the scheduler prepares and drafts regardless, but transmits
nothing until dry_run is explicitly false.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db
from .cli import cmd_agent_run, cmd_followups, cmd_scan_replies
from .outreach import load_settings

log = logging.getLogger("jobpilot.scheduler")


def _safe(fn, name: str, **kwargs) -> None:
    """Run a scheduled job, logging any failure to agent_runs instead of crashing."""
    try:
        fn(SimpleNamespace(**kwargs))
    except Exception as e:  # noqa: BLE001 — the loop must survive every job
        log.exception("scheduled job %s failed", name)
        try:
            conn = db.connect()
            rid = db.start_run(conn, name)
            db.finish_run(conn, rid, status="error", error=f"{type(e).__name__}: {e}")
            db.add_notification(conn, "run_error", f"Scheduled '{name}' failed", body=str(e)[:200],
                                link="/health")
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def daily_cycle() -> None:
    # JobSpy included (LinkedIn/Indeed/Naukri) — gated by jobspy.enabled in settings.
    # Use residential proxies on the VM (jobspy.proxies) to avoid datacenter blocks.
    _safe(cmd_agent_run, "agent", no_jobspy=False, limit=None)


def followup_cycle() -> None:
    _safe(cmd_followups, "followup", limit=None)


def reply_cycle() -> None:
    _safe(cmd_scan_replies, "reply_scan", days=14)


def build_scheduler(settings: dict) -> BlockingScheduler:
    sc = settings.get("schedule", {}) or {}
    tz = sc.get("timezone", "Asia/Kolkata")
    hh, mm = (sc.get("scrape_at", "08:00").split(":") + ["0"])[:2]

    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(daily_cycle, CronTrigger(hour=int(hh), minute=int(mm), timezone=tz),
                      id="daily", name="daily full cycle", misfire_grace_time=3600)
    scheduler.add_job(followup_cycle,
                      IntervalTrigger(hours=int(sc.get("followup_every_hours", 6))),
                      id="followups", name="follow-up sweep")
    scheduler.add_job(reply_cycle,
                      IntervalTrigger(hours=int(sc.get("reply_scan_every_hours", 2))),
                      id="replies", name="reply scan")
    return scheduler


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    scheduler = build_scheduler(settings)
    sc = settings.get("schedule", {}) or {}
    log.info("JobPilot scheduler up — daily at %s %s, follow-ups every %sh, replies every %sh. "
             "Sending is %s.", sc.get("scrape_at", "08:00"), sc.get("timezone", "Asia/Kolkata"),
             sc.get("followup_every_hours", 6), sc.get("reply_scan_every_hours", 2),
             "LIVE" if not settings.get("outreach", {}).get("dry_run", True) else "DRY-RUN (nothing sends)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")


if __name__ == "__main__":
    main()
