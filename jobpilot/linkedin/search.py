"""LinkedIn job discovery by driving the job-search UI.

This complements the API collectors in `jobpilot/collectors/` — it is not a
replacement. Use it for the one thing the public APIs cannot tell us: whether a
posting supports **Easy Apply**, which is what `jobpilot.linkedin.apply` needs.

Two DOM shapes exist and this module handles both:

* **Guest / logged-out** (`div.base-card.job-search-card`) — the public JSERP.
  Every selector for this layout was verified live against linkedin.com.
* **Authenticated** (`div.job-card-container` in `.scaffold-layout__list`) — the
  real product. Those selectors are written from the known logged-in structure
  and are UNVERIFIED here; see NOTES.md.

Pacing and blocker-guarding are non-negotiable: one navigation per `human_delay`,
`session.guard` after each, and the run stops the moment LinkedIn challenges us.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from ..db import Job
from . import session as sess
from .session import BlockerDetected, guard, human_delay

SEARCH_BASE = "https://www.linkedin.com/jobs/search/"
PAGE_SIZE = 25          # LinkedIn's `start=` offset step
MAX_PAGES = 4           # deliberately modest — deep paging is what gets you a 429

# ── Card layouts ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class CardLayout:
    name: str
    card: str
    title: str
    company: str
    location: str
    link: str
    posted: str
    footer: str


# Verified live on the public logged-out JSERP (2026-07).
GUEST_LAYOUT = CardLayout(
    name="guest",
    card="div.base-card.job-search-card, div.base-search-card--link",
    title="h3.base-search-card__title",
    company="h4.base-search-card__subtitle",
    location="span.job-search-card__location",
    link="a.base-card__full-link",
    posted="time",
    footer="div.base-search-card__metadata",
)

# UNVERIFIED — written from the authenticated DOM structure.
AUTH_LAYOUT = CardLayout(
    name="authenticated",
    card="div.job-card-container, li.jobs-search-results__list-item, li.scaffold-layout__list-item",
    title="a.job-card-container__link, a.job-card-list__title--link, .job-card-list__title, .artdeco-entity-lockup__title",
    company=".job-card-container__primary-description, .artdeco-entity-lockup__subtitle, .job-card-container__company-name",
    location=".job-card-container__metadata-item, .job-card-container__metadata-wrapper li, .artdeco-entity-lockup__caption",
    link="a.job-card-container__link, a.job-card-list__title--link, a[href*='/jobs/view/']",
    posted="time",
    footer=".job-card-container__footer-wrapper, .job-card-list__footer-wrapper, .job-card-container__footer-item",
)

LAYOUTS = (AUTH_LAYOUT, GUEST_LAYOUT)

# Job-detail selectors. Guest ones verified live; authenticated ones UNVERIFIED.
DESC_SELECTORS = (
    "div.jobs-description__content",              # authenticated (UNVERIFIED)
    "div.jobs-box__html-content",                 # authenticated (UNVERIFIED)
    "#job-details",                               # authenticated (UNVERIFIED)
    "div.show-more-less-html__markup",            # guest (VERIFIED)
    "div.description__text",                      # guest (VERIFIED)
)
DETAIL_TITLE_SELECTORS = (
    "h1.top-card-layout__title",                  # guest (VERIFIED)
    "h1.job-details-jobs-unified-top-card__job-title",   # auth (UNVERIFIED)
    "h1.t-24",
)
DETAIL_COMPANY_SELECTORS = (
    "a.topcard__org-name-link",                   # guest (VERIFIED)
    "span.topcard__flavor",                       # guest (VERIFIED)
    "div.job-details-jobs-unified-top-card__company-name",  # auth (UNVERIFIED)
)
DETAIL_LOCATION_SELECTORS = (
    "span.topcard__flavor--bullet",               # guest (VERIFIED)
    "div.job-details-jobs-unified-top-card__primary-description-container span.tvm__text",
)
EASY_APPLY_BUTTON_SELECTORS = (
    "button.jobs-apply-button",                   # auth (UNVERIFIED)
    "button[aria-label*='Easy Apply']",
    "button[data-live-test-job-apply-button]",
)

EASY_APPLY_RE = re.compile(r"easy\s*apply", re.I)
JOB_ID_RE = re.compile(r"(?:/jobs/view/(?:[^/?#]*?-)?(\d{6,})|currentJobId=(\d{6,})|jobPosting:(\d{6,}))")


@dataclass
class LinkedInResult:
    """A search hit plus the LinkedIn-only metadata `Job` has no column for."""
    job: Job
    easy_apply: bool = False
    linkedin_job_id: str | None = None
    raw: dict = field(default_factory=dict)


# ── Pure helpers (unit-tested without a browser) ─────────────────────

def extract_job_id(url_or_urn: str | None) -> str | None:
    if not url_or_urn:
        return None
    m = JOB_ID_RE.search(url_or_urn)
    if not m:
        return None
    return next((g for g in m.groups() if g), None)


def canonical_job_url(url: str | None) -> str | None:
    """Strip LinkedIn's tracking query-string down to a stable canonical URL."""
    job_id = extract_job_id(url)
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}/"
    if not url:
        return None
    return url.split("?")[0]


def build_search_url(term: str, location: str | None, settings: dict | None = None,
                     start: int = 0, hours_old: int | None = None) -> str:
    """Compose a jobs-search URL.

    `f_AL=true` is LinkedIn's Easy-Apply filter (verified working logged-out) —
    letting the server filter is far cheaper and safer than paging through
    everything and discarding.
    """
    cfg = sess.linkedin_cfg(settings)
    params = [f"keywords={quote_plus(term)}"]
    if location:
        params.append(f"location={quote_plus(location)}")
    if cfg["easy_apply_only"]:
        params.append("f_AL=true")
    if hours_old:
        params.append(f"f_TPR=r{int(hours_old) * 3600}")
    if start:
        params.append(f"start={int(start)}")
    return f"{SEARCH_BASE}?{'&'.join(params)}"


def is_remote(location: str | None, title: str | None = None,
              workplace: str | None = None) -> bool:
    blob = " ".join(x for x in (location, title, workplace) if x).lower()
    return "remote" in blob or "work from home" in blob


def clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s{2,}", " ", text.replace(" ", " ")).strip()


def dedupe_results(results: list[LinkedInResult]) -> list[LinkedInResult]:
    """Collapse duplicates by LinkedIn job id, preferring rows that have a description."""
    best: dict[str, LinkedInResult] = {}
    order: list[str] = []
    for r in results:
        key = r.linkedin_job_id or (r.job.url or "") or r.job.id
        existing = best.get(key)
        if existing is None:
            best[key] = r
            order.append(key)
            continue
        if len(r.job.description or "") > len(existing.job.description or ""):
            best[key] = r
    return [best[k] for k in order]


# ── DOM extraction ───────────────────────────────────────────────────

def _dedupe_label(text: str) -> str:
    """Collapse LinkedIn's doubled card labels to a single clean line.

    Job cards render the title twice — a visible span plus a `visually-hidden`
    one for screen readers — so `inner_text()` yields
    "Product Manager\\nProduct Manager with verification". Keeping both corrupts
    the title for the hard filter and the scorer, so keep the first line when
    the lines are duplicates or one prefixes the other.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    first = lines[0]
    if all(ln == first or ln.startswith(first) or first.startswith(ln) for ln in lines[1:]):
        return first
    return " ".join(lines)


def _text(scope, selector: str) -> str:
    for sel in selector.split(", "):
        try:
            loc = scope.locator(sel).first
            if loc.count():
                return _dedupe_label(clean(loc.inner_text()))
        except Exception:  # noqa: BLE001 — selector churn must not kill the row
            continue
    return ""


def _attr(scope, selector: str, name: str) -> str | None:
    for sel in selector.split(", "):
        try:
            loc = scope.locator(sel).first
            if loc.count():
                val = loc.get_attribute(name)
                if val:
                    return val
        except Exception:  # noqa: BLE001
            continue
    return None


def detect_layout(page) -> CardLayout | None:
    """Pick whichever card layout is actually on the page."""
    for layout in LAYOUTS:
        try:
            if page.locator(layout.card).count():
                return layout
        except Exception:  # noqa: BLE001
            continue
    return None


def parse_card(card, layout: CardLayout) -> LinkedInResult | None:
    """Turn one search-result card into a LinkedInResult. Returns None if unusable."""
    title = _text(card, layout.title)
    company = _text(card, layout.company)
    if not title or not company:
        return None

    href = _attr(card, layout.link, "href")
    urn = None
    try:
        urn = card.get_attribute("data-entity-urn") or card.get_attribute("data-job-id")
    except Exception:  # noqa: BLE001
        pass
    job_id = extract_job_id(href) or extract_job_id(urn)
    url = canonical_job_url(href) or (
        f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else None)

    location = _text(card, layout.location)
    posted = _attr(card, layout.posted, "datetime")

    try:
        card_text = clean(card.inner_text())
    except Exception:  # noqa: BLE001
        card_text = ""

    job = Job(
        source="linkedin",
        company=company,
        title=title,
        location=location or None,
        is_remote=is_remote(location, title),
        url=url,
        description=None,
        posted_at=posted,
    )
    return LinkedInResult(
        job=job,
        easy_apply=bool(EASY_APPLY_RE.search(card_text)),
        linkedin_job_id=job_id,
        raw={"layout": layout.name, "card_text": card_text[:400]},
    )


def fetch_description(page, url: str, settings: dict | None = None,
                      log=print) -> tuple[str, bool]:
    """Open a job detail page and return (description_text, easy_apply_seen).

    One navigation per job — this is the expensive part of a search; the caller
    caps how many it does.
    """
    sess.goto(page, url, settings, log=log)

    # "See more" expander — the guest page truncates descriptions behind it.
    for sel in ("button.show-more-less-html__button--more",
                "button[aria-label='Click to see more description']",
                "button.jobs-description__footer-button"):
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=5_000)
                human_delay(settings, scale=0.3)
                break
        except Exception:  # noqa: BLE001 — expander is a nicety, not a requirement
            continue

    description = ""
    for sel in DESC_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count():
                description = clean(loc.inner_text())
                if description:
                    break
        except Exception:  # noqa: BLE001
            continue

    easy_apply = False
    for sel in EASY_APPLY_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.count() and EASY_APPLY_RE.search(btn.inner_text() or ""):
                easy_apply = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not easy_apply:
        try:
            easy_apply = bool(EASY_APPLY_RE.search(page.inner_text("body")[:6000]))
        except Exception:  # noqa: BLE001
            pass
    return description, easy_apply


# ── Public API ───────────────────────────────────────────────────────

def search_jobs_detailed(context, terms: list[str], locations: list[str],
                         settings: dict | None = None, limit_per_search: int = 25,
                         fetch_descriptions: bool = True,
                         hours_old: int | None = None,
                         log=print) -> list[LinkedInResult]:
    """Full-fidelity search: results keep their Easy-Apply flag and LinkedIn job id.

    `search_jobs` wraps this and returns plain `Job`s for `upsert_jobs`.
    """
    cfg = sess.linkedin_cfg(settings)
    results: list[LinkedInResult] = []
    page = context.new_page()
    detail_page = None

    try:
        for term in terms or []:
            for location in (locations or [None]):
                label = f"{term!r}" + (f" in {location!r}" if location else "")
                log(f"  [linkedin] searching {label}")
                collected = 0
                seen_ids: set[str] = set()

                for page_no in range(MAX_PAGES):
                    if collected >= limit_per_search:
                        break
                    url = build_search_url(term, location, settings,
                                           start=page_no * PAGE_SIZE, hours_old=hours_old)
                    try:
                        sess.goto(page, url, settings, log=log)
                    except BlockerDetected:
                        raise
                    except Exception as e:  # noqa: BLE001 — a bad page ends this search only
                        log(f"    ✗ page {page_no + 1} failed ({type(e).__name__}: {e})")
                        break

                    page.wait_for_timeout(2500)
                    layout = detect_layout(page)
                    if layout is None:
                        log(f"    · no result cards on page {page_no + 1} — stopping this search")
                        break

                    cards = page.locator(layout.card)
                    count = cards.count()
                    if not count:
                        break

                    new_this_page = 0
                    for i in range(count):
                        if collected >= limit_per_search:
                            break
                        try:
                            parsed = parse_card(cards.nth(i), layout)
                        except Exception:  # noqa: BLE001
                            continue
                        if parsed is None:
                            continue
                        key = parsed.linkedin_job_id or parsed.job.url or parsed.job.id
                        if key in seen_ids:
                            continue
                        seen_ids.add(key)
                        results.append(parsed)
                        collected += 1
                        new_this_page += 1

                    log(f"    · page {page_no + 1} ({layout.name} layout): "
                        f"{new_this_page} new / {count} cards")
                    if new_this_page == 0:
                        # `start=` offsets that get bounced back to page 1 return the
                        # same cards — that means we've been throttled. Stop.
                        break
                    human_delay(settings, why="between result pages", log=log)

        if fetch_descriptions and results:
            detail_page = context.new_page()
            targets = [r for r in dedupe_results(results) if r.job.url]
            log(f"  [linkedin] fetching {len(targets)} job descriptions "
                f"(paced {cfg['min_delay_seconds']:.0f}-{cfg['max_delay_seconds']:.0f}s apart)")
            for idx, r in enumerate(targets, 1):
                try:
                    desc, easy = fetch_description(detail_page, r.job.url, settings, log=log)
                except BlockerDetected:
                    raise
                except Exception as e:  # noqa: BLE001 — skip one description, keep the job
                    log(f"    ✗ [{idx}/{len(targets)}] {r.job.title}: "
                        f"{type(e).__name__}: {e}")
                    continue
                if desc:
                    r.job.description = desc
                r.easy_apply = r.easy_apply or easy
                if idx < len(targets):
                    human_delay(settings, why="between job pages", log=log)

    finally:
        for p in (detail_page, page):
            try:
                if p:
                    p.close()
            except Exception:  # noqa: BLE001
                pass

    out = dedupe_results(results)
    if cfg["easy_apply_only"]:
        kept = [r for r in out if r.easy_apply]
        dropped = len(out) - len(kept)
        if dropped:
            log(f"  [linkedin] dropped {dropped} non-Easy-Apply results "
                "(linkedin.easy_apply_only = true)")
        out = kept
    return out


def search_jobs(context, terms: list[str], locations: list[str],
                settings: dict | None = None, limit_per_search: int = 25,
                fetch_descriptions: bool = True, hours_old: int | None = None,
                log=print) -> list[Job]:
    """Drive the LinkedIn job-search UI and return `Job`s ready for `upsert_jobs`.

    All jobs come back with `source='linkedin'`. When `easy_apply_only` is set
    (the default) only Easy-Apply postings are returned, so anything this yields
    is a valid target for `apply.apply_batch`.
    """
    return [r.job for r in search_jobs_detailed(
        context, terms, locations, settings, limit_per_search=limit_per_search,
        fetch_descriptions=fetch_descriptions, hours_old=hours_old, log=log)]


def collect(settings: dict | None, terms: list[str], locations: list[str],
            limit_per_search: int = 25, hours_old: int | None = None,
            log=print) -> list[Job]:
    """Convenience wrapper that owns the browser lifecycle end to end."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser, context = sess.launch(settings, pw)
        try:
            sess.ensure_logged_in(context, settings, log=log)
            return search_jobs(context, terms, locations, settings,
                               limit_per_search=limit_per_search,
                               hours_old=hours_old, log=log)
        finally:
            try:
                context.close()
            finally:
                browser.close()
