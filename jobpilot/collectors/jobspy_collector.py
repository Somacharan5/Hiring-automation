"""JobSpy collector — scrapes LinkedIn / Indeed / Google Jobs in one call.

python-jobspy is a heavy optional dependency; the pipeline degrades gracefully
(ATS + board collectors still run) if it's missing or a scrape fails.
LinkedIn scraping is rate-limit sensitive — keep results_wanted modest.
"""

from __future__ import annotations

from ..db import Job

SITES = ["linkedin", "indeed", "google"]


def collect(terms: list[str], locations: list[str], results_wanted: int,
            hours_old: int, include_remote: bool, log=print) -> list[Job]:
    try:
        from jobspy import scrape_jobs  # imported lazily — heavy dep
    except ImportError:
        log("  [jobspy] python-jobspy not installed — skipping LinkedIn/Indeed/Google")
        return []

    jobs: list[Job] = []
    searches = [(t, loc) for t in terms for loc in locations]
    if include_remote:
        searches += [(t, "Remote") for t in terms]

    for term, location in searches:
        try:
            df = scrape_jobs(
                site_name=SITES,
                search_term=term,
                google_search_term=f"{term} jobs in {location}",
                location=location,
                results_wanted=results_wanted,
                hours_old=hours_old,
                is_remote=(location == "Remote"),
                linkedin_fetch_description=False,  # keep it light; descriptions via job URL later
                verbose=0,
            )
        except Exception as e:  # noqa: BLE001 — one failed scrape must not kill the run
            log(f"  [jobspy] '{term}' @ {location}: skipped ({type(e).__name__}: {e})")
            continue

        for _, row in df.iterrows():
            jobs.append(Job(
                source=f"jobspy:{row.get('site', '?')}",
                company=str(row.get("company") or "?"),
                title=str(row.get("title") or ""),
                location=str(row.get("location") or location),
                is_remote=bool(row.get("is_remote") or location == "Remote"),
                url=str(row.get("job_url") or ""),
                description=str(row.get("description") or "") if row.get("description") else None,
                posted_at=str(row.get("date_posted") or ""),
            ))
        log(f"  [jobspy] '{term}' @ {location}: {len(df)} jobs")
    return jobs
