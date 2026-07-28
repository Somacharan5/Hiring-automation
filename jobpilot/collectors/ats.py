"""Collectors for free, no-auth public ATS APIs.

Each company careers board built on Greenhouse / Lever / Ashby / Workable /
SmartRecruiters / Recruitee exposes a public JSON endpoint. We poll those
directly — structured data, no scraping. Wrong/renamed board slugs return
404 and are skipped silently.
"""

from __future__ import annotations

import html
import re

import requests

from ..db import Job

UA = {"User-Agent": "Mozilla/5.0 (jobpilot; personal job search)"}
TIMEOUT = 20


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<(br|/p|/li|/div|/h\d)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _title_matches(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(k in t for k in keywords)


def _get_json(url: str, **kwargs):
    resp = requests.get(url, headers=UA, timeout=TIMEOUT, **kwargs)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


# ── Providers ────────────────────────────────────────────────────────

def greenhouse(slug: str, keywords: list[str]) -> list[Job]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not data:
        return []
    jobs = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not _title_matches(title, keywords):
            continue
        location = (j.get("location") or {}).get("name", "")
        jobs.append(Job(
            source="greenhouse", company=slug, title=title, location=location,
            is_remote="remote" in location.lower(),
            url=j.get("absolute_url"),
            description=_strip_html(j.get("content")),
            posted_at=j.get("updated_at"),
        ))
    return jobs


def lever(slug: str, keywords: list[str]) -> list[Job]:
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if data is None:
        return []
    jobs = []
    for j in data:
        title = j.get("text", "")
        if not _title_matches(title, keywords):
            continue
        cats = j.get("categories") or {}
        location = cats.get("location", "") or ""
        jobs.append(Job(
            source="lever", company=slug, title=title, location=location,
            is_remote="remote" in location.lower(),
            url=j.get("hostedUrl"),
            description=j.get("descriptionPlain") or _strip_html(j.get("description")),
        ))
    return jobs


def ashby(slug: str, keywords: list[str]) -> list[Job]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not data:
        return []
    jobs = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not j.get("isListed", True) or not _title_matches(title, keywords):
            continue
        jobs.append(Job(
            source="ashby", company=slug, title=title,
            location=j.get("location", ""),
            is_remote=bool(j.get("isRemote")),
            url=j.get("jobUrl") or j.get("applyUrl"),
            description=j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml")),
            posted_at=j.get("publishedAt"),
        ))
    return jobs


def workable(slug: str, keywords: list[str]) -> list[Job]:
    data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    if not data:
        return []
    jobs = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not _title_matches(title, keywords):
            continue
        location = ", ".join(filter(None, [j.get("city"), j.get("country")]))
        jobs.append(Job(
            source="workable", company=slug, title=title, location=location,
            is_remote=bool(j.get("telecommuting")),
            url=j.get("url"),
            description=_strip_html(j.get("description")),
        ))
    return jobs


def smartrecruiters(slug: str, keywords: list[str]) -> list[Job]:
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if not data:
        return []
    jobs = []
    for j in data.get("content", []):
        title = j.get("name", "")
        if not _title_matches(title, keywords):
            continue
        loc = j.get("location") or {}
        location = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
        # Description requires a per-posting detail call — only for matched titles
        description = ""
        detail = _get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{j.get('id')}")
        if detail:
            sections = ((detail.get("jobAd") or {}).get("sections") or {})
            description = "\n\n".join(
                _strip_html(sec.get("text")) for sec in sections.values() if isinstance(sec, dict)
            )
        jobs.append(Job(
            source="smartrecruiters", company=slug, title=title, location=location,
            is_remote=bool(loc.get("remote")),
            url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            description=description,
            posted_at=j.get("releasedDate"),
        ))
    return jobs


def recruitee(slug: str, keywords: list[str]) -> list[Job]:
    data = _get_json(f"https://{slug}.recruitee.com/api/offers/")
    if not data:
        return []
    jobs = []
    for j in data.get("offers", []):
        title = j.get("title", "")
        if not _title_matches(title, keywords):
            continue
        jobs.append(Job(
            source="recruitee", company=slug, title=title,
            location=j.get("location", ""),
            is_remote="remote" in (j.get("location") or "").lower(),
            url=j.get("careers_url"),
            description=_strip_html(j.get("description")),
        ))
    return jobs


PROVIDERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
    "recruitee": recruitee,
}


def collect_all(companies: dict, keywords: list[str], log=print) -> list[Job]:
    """Poll every configured company board. Individual failures never abort the run."""
    all_jobs: list[Job] = []
    for provider, slugs in companies.items():
        fn = PROVIDERS.get(provider)
        if not fn or not slugs:
            continue
        for slug in slugs:
            try:
                found = fn(slug, keywords)
                if found:
                    log(f"  [{provider}] {slug}: {len(found)} matching roles")
                all_jobs.extend(found)
            except Exception as e:  # noqa: BLE001 — one bad board must not kill the run
                log(f"  [{provider}] {slug}: skipped ({type(e).__name__}: {e})")
    return all_jobs
