"""Aggregator / remote-job board collectors: Remotive, RemoteOK, Adzuna (optional keys)."""

from __future__ import annotations

from datetime import datetime, timezone

import requests

from ..db import Job
from .ats import UA, TIMEOUT, _strip_html, _title_matches


def _ts(val) -> str | None:
    """Best-effort → ISO timestamp for posted_at; None if unparseable (never breaks a batch)."""
    if val is None or val == "":
        return None
    try:
        if isinstance(val, (int, float)):
            v = float(val)
            if v > 1e12:            # milliseconds
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        return datetime.fromisoformat(str(val).strip().replace("Z", "+00:00")).isoformat()
    except Exception:  # noqa: BLE001
        return None


def remotive(keywords: list[str], log=print) -> list[Job]:
    """Remotive free API — remote roles, has a dedicated 'product' category."""
    try:
        resp = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"category": "product", "limit": 100},
            headers=UA, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        log(f"  [remotive] skipped ({type(e).__name__}: {e})")
        return []
    jobs = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not _title_matches(title, keywords):
            continue
        jobs.append(Job(
            source="remotive", company=j.get("company_name", "?"), title=title,
            location=j.get("candidate_required_location", "Remote"),
            is_remote=True,
            url=j.get("url"),
            description=_strip_html(j.get("description")),
            posted_at=j.get("publication_date"),
        ))
    log(f"  [remotive] {len(jobs)} matching roles")
    return jobs


def remoteok(keywords: list[str], log=print) -> list[Job]:
    """RemoteOK free API — remote roles across categories."""
    try:
        resp = requests.get("https://remoteok.com/api", headers=UA, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        log(f"  [remoteok] skipped ({type(e).__name__}: {e})")
        return []
    jobs = []
    for j in data:
        if not isinstance(j, dict) or "position" not in j:
            continue  # first element is a legal notice
        title = j.get("position", "")
        if not _title_matches(title, keywords):
            continue
        jobs.append(Job(
            source="remoteok", company=j.get("company", "?"), title=title,
            location=j.get("location") or "Remote",
            is_remote=True,
            url=j.get("url"),
            description=_strip_html(j.get("description")),
            posted_at=j.get("date"),
        ))
    log(f"  [remoteok] {len(jobs)} matching roles")
    return jobs


# Adzuna-supported country indexes matching our targets (UAE/ae is not an Adzuna index).
ADZUNA_COUNTRIES = ["us", "gb", "in", "sg", "de", "nl", "ch", "ca", "au"]


def adzuna(app_id: str, app_key: str, keywords: list[str], log=print) -> list[Job]:
    """Adzuna API — free key at developer.adzuna.com. Skipped when keys are blank."""
    if not app_id or not app_key:
        return []
    jobs = []
    for country in ADZUNA_COUNTRIES:
        try:
            resp = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params={
                    "app_id": app_id, "app_key": app_key,
                    "what": "product manager", "results_per_page": 50,
                    "max_days_old": 7, "content-type": "application/json",
                },
                headers=UA, timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            log(f"  [adzuna:{country}] skipped ({type(e).__name__}: {e})")
            continue
        for j in data.get("results", []):
            title = j.get("title", "")
            if not _title_matches(_strip_html(title), keywords):
                continue
            jobs.append(Job(
                source=f"adzuna:{country}",
                company=(j.get("company") or {}).get("display_name", "?"),
                title=_strip_html(title),
                location=(j.get("location") or {}).get("display_name", ""),
                url=j.get("redirect_url"),
                description=j.get("description", ""),
                posted_at=j.get("created"),
            ))
    log(f"  [adzuna] {len(jobs)} matching roles")
    return jobs


def arbeitnow(keywords: list[str], log=print) -> list[Job]:
    """Arbeitnow free job-board API — Europe-heavy (good for DE/CH/NL), remote + onsite."""
    try:
        resp = requests.get("https://www.arbeitnow.com/api/job-board-api", headers=UA, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        log(f"  [arbeitnow] skipped ({type(e).__name__}: {e})")
        return []
    jobs = []
    for j in data.get("data", []):
        title = j.get("title", "")
        if not _title_matches(title, keywords):
            continue
        jobs.append(Job(
            source="arbeitnow", company=j.get("company_name", "?"), title=title,
            location=j.get("location") or "", is_remote=bool(j.get("remote")),
            url=j.get("url"), description=_strip_html(j.get("description")),
            posted_at=_ts(j.get("created_at"))))
    log(f"  [arbeitnow] {len(jobs)} matching roles")
    return jobs


def jobicy(keywords: list[str], log=print) -> list[Job]:
    """Jobicy free remote-jobs API (v2) — global remote, title-filtered."""
    try:
        resp = requests.get("https://jobicy.com/api/v2/remote-jobs?count=100", headers=UA, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        log(f"  [jobicy] skipped ({type(e).__name__}: {e})")
        return []
    jobs = []
    for j in data.get("jobs", []):
        title = j.get("jobTitle", "")
        if not _title_matches(title, keywords):
            continue
        jobs.append(Job(
            source="jobicy", company=j.get("companyName", "?"), title=title,
            location=j.get("jobGeo") or "Remote", is_remote=True,
            url=j.get("url") or f"https://jobicy.com/jobs/{j.get('jobSlug', '')}",
            description=_strip_html(j.get("jobDescription")), posted_at=_ts(j.get("pubDate"))))
    log(f"  [jobicy] {len(jobs)} matching roles")
    return jobs


def himalayas(keywords: list[str], log=print) -> list[Job]:
    """Himalayas free remote-jobs API — global remote; carries salary fields."""
    try:
        resp = requests.get("https://himalayas.app/jobs/api?limit=100", headers=UA, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        log(f"  [himalayas] skipped ({type(e).__name__}: {e})")
        return []
    jobs = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not _title_matches(title, keywords):
            continue
        loc = j.get("locationRestrictions")
        loc = ", ".join(loc) if isinstance(loc, list) else (loc or "Remote")
        jobs.append(Job(
            source="himalayas", company=j.get("companyName", "?"), title=title,
            location=loc, is_remote=True,
            url=j.get("applicationLink") or j.get("guid"),
            description=_strip_html(j.get("description")), posted_at=_ts(j.get("pubDate"))))
    log(f"  [himalayas] {len(jobs)} matching roles")
    return jobs
