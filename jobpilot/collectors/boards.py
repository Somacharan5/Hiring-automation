"""Aggregator / remote-job board collectors: Remotive, RemoteOK, Adzuna (optional keys)."""

from __future__ import annotations

import requests

from ..db import Job
from .ats import UA, TIMEOUT, _strip_html, _title_matches


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


ADZUNA_COUNTRIES = ["us", "gb", "in", "sg", "ae"]


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
