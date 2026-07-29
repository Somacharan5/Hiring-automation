"""JobSpy collector — LinkedIn / Indeed / Naukri / Google in one call.

Scrapes the public (guest, no-login) job pages, so the user's own LinkedIn account
is never used and can't be flagged — the only real risk is IP rate-limiting, which
is why the search matrix is kept modest and `proxies` is supported (recommended on a
datacenter/VM IP). Naukri is India-only, so it's routed to India locations. Fetching
descriptions is ON by default — the scorer, tailor and work-auth filter all need the JD.

python-jobspy is a heavy optional dep; the pipeline degrades gracefully if it's
missing or a scrape fails (ATS + board collectors still run).
"""

from __future__ import annotations

from ..db import Job

SITES_GLOBAL = ["linkedin", "indeed", "google"]

# location substring → Indeed country domain (JobSpy's country_indeed)
_INDEED_COUNTRY = {
    "india": "india", "bangalore": "india", "mumbai": "india", "delhi": "india",
    "gurugram": "india", "hyderabad": "india", "pune": "india",
    "united kingdom": "uk", "london": "uk", "ireland": "ireland",
    "germany": "germany", "berlin": "germany", "netherlands": "netherlands",
    "amsterdam": "netherlands", "switzerland": "switzerland", "zurich": "switzerland",
    "singapore": "singapore", "united arab emirates": "united arab emirates",
    "dubai": "united arab emirates", "united states": "usa", "canada": "canada",
    "australia": "australia", "luxembourg": "luxembourg", "qatar": "qatar",
}


def _indeed_country(location: str) -> str:
    low = (location or "").lower()
    for key, val in _INDEED_COUNTRY.items():
        if key in low:
            return val
    return "usa"


def _is_india(location: str) -> bool:
    return "india" in (location or "").lower()


def collect(terms: list[str], locations: list[str], results_wanted: int,
            hours_old: int, include_remote: bool, *, sites: list[str] | None = None,
            fetch_descriptions: bool = True, naukri_for_india: bool = True,
            proxies: list[str] | None = None, log=print) -> list[Job]:
    try:
        from jobspy import scrape_jobs  # imported lazily — heavy dep
    except ImportError:
        log("  [jobspy] python-jobspy not installed — skipping LinkedIn/Indeed/Naukri")
        return []

    sites = sites or SITES_GLOBAL
    jobs: list[Job] = []
    searches = [(t, loc) for t in terms for loc in locations]
    if include_remote:
        searches += [(t, "Remote") for t in terms]

    for term, location in searches:
        loc_sites = list(sites)
        if naukri_for_india and _is_india(location) and "naukri" not in loc_sites:
            loc_sites.append("naukri")
        try:
            df = scrape_jobs(
                site_name=loc_sites,
                search_term=term,
                google_search_term=f"{term} jobs in {location}",
                location=None if location == "Remote" else location,
                results_wanted=results_wanted,
                hours_old=hours_old,
                country_indeed=_indeed_country(location),
                is_remote=(location == "Remote"),
                linkedin_fetch_description=fetch_descriptions,
                description_format="markdown",
                proxies=proxies,
                verbose=0,
            )
        except Exception as e:  # noqa: BLE001 — one failed scrape must not kill the run
            log(f"  [jobspy] '{term}' @ {location}: skipped ({type(e).__name__}: {e})")
            continue

        for _, row in df.iterrows():
            desc = row.get("description")
            jobs.append(Job(
                source=f"jobspy:{row.get('site', '?')}",
                company=str(row.get("company") or "?"),
                title=str(row.get("title") or ""),
                location=str(row.get("location") or location),
                is_remote=bool(row.get("is_remote") or location == "Remote"),
                url=str(row.get("job_url") or ""),
                description=str(desc) if desc else None,
                posted_at=None,       # date_posted formats vary; collected_at drives freshness
            ))
        log(f"  [jobspy] '{term}' @ {location} ({'+'.join(loc_sites)}): {len(df)} jobs")
    return jobs
