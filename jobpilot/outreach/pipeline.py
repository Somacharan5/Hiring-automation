"""Outreach orchestration: shortlisted job → address → tailored resume → email.

prepare_outreach() readies one 'preparing_resume' application per shortlisted job
that doesn't have one yet (discover a generic address, tailor the resume, compose
the email). send_pending / run_followups / scan_replies then act on those rows.
run_outreach_cycle() runs the whole thing in order. Sending stays gated by dry_run.
"""

from __future__ import annotations

from .composer import compose_email
from .find_addresses import best_sendable_contact, discover_generic
from .followups import run_followups
from .inbox_reader import scan_replies
from .send import send_pending


def prepare_outreach(conn, settings: dict, limit: int | None = None,
                     min_score: int | None = None, log=print) -> list[dict]:
    """Discover + tailor + compose for shortlisted jobs with no email application yet."""
    from ..resume.tailor import tailor_and_render

    cfg = settings.get("outreach", {})
    gate = min_score if min_score is not None else int(cfg.get("min_score_to_contact", 60))
    floor = int((settings.get("email_discovery", {}) or {}).get("min_confidence_to_autosend", 70))

    rows = conn.execute(
        "SELECT j.* FROM jobs j LEFT JOIN applications a "
        "  ON a.job_id = j.id AND a.channel = 'email' "
        "WHERE j.status = 'shortlisted' AND COALESCE(j.match_score, 0) >= %s AND a.id IS NULL "
        "ORDER BY j.match_score DESC", (gate,)).fetchall()
    if limit:
        rows = rows[:limit]

    results: list[dict] = []
    for job in rows:
        log(f"\n▸ {job['company']} — {job['title'].strip()}  [{job['match_score']}]")
        try:
            discover_generic(conn, job["company"], job_url=job["url"], settings=settings)
        except Exception as e:  # noqa: BLE001 — discovery must not kill the batch
            log(f"  ✗ discovery: {type(e).__name__}: {e}")
        contact = best_sendable_contact(conn, job["company"], min_confidence=floor)
        log(f"  address: {contact['email'] if contact else '(none found — drafts for review)'}")

        try:
            res = tailor_and_render(conn, job["id"], settings)
            log(f"  resume:  {res['pdf_path'].split('/')[-1]}"
                + (f"  ⚠ {'; '.join(res['warnings'])}" if res["warnings"] else ""))
        except Exception as e:  # noqa: BLE001
            log(f"  ✗ tailor: {type(e).__name__}: {e}")

        try:
            draft = compose_email(conn, job["id"], contact, settings)
            log(f"  email:   {draft.subject!r}")
            results.append({"job_id": job["id"], "company": job["company"],
                            "to": contact["email"] if contact else None,
                            "subject": draft.subject})
        except Exception as e:  # noqa: BLE001
            log(f"  ✗ compose: {type(e).__name__}: {e}")
            results.append({"job_id": job["id"], "company": job["company"], "error": str(e)})
    return results


def run_outreach_cycle(conn, settings: dict, limit: int | None = None, log=print) -> dict:
    """Prepare → send → follow up → scan replies. One pass. Returns a combined report."""
    prepared = prepare_outreach(conn, settings, limit=limit, log=log)
    sent = send_pending(conn, settings, limit=limit)
    followed = run_followups(conn, settings, limit=limit)
    try:
        replies = scan_replies(conn, settings)
    except Exception as e:  # noqa: BLE001 — a mailbox hiccup must not fail the cycle
        replies = {"error": f"{type(e).__name__}: {e}"}
    return {"prepared": len(prepared), "send": sent, "followups": followed, "replies": replies}
