"""Zero-cost recruiter discovery. No paid API is required by anything here.

Four sources, in the order the orchestrator should try them:

    crawl_site_for_emails   free, no key, ~2s      — addresses the company itself published
    github_emails           free, no key (60/hr)   — commit authors on the company's org
    linkedin_recruiter_names free, existing session — NAMES only, to feed pattern generation
    google_cse_search       free tier, needs a key — 100 queries/day

Every function returns `finder.ContactCandidate` objects so they drop straight
into the existing pipeline, and every one of them degrades to "log a warning and
return []" when its dependency is missing. No key in `.env` is a normal state,
not an error.

The confidences set here are **source** confidences — "how much do I believe
this string is a real address at this company". They are deliberately modest and
are later combined with (and usually overruled by) `verify.verify_email`.
"""

from __future__ import annotations

import html
import re
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from .finder import ContactCandidate, _is_recruiterish, _warn
from .verify import role_kind

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 JobPilot/1.0 "
              "(+personal job-search assistant)")
HTTP_TIMEOUT = 10
GITHUB_API = "https://api.github.com"

# ── site crawl ───────────────────────────────────────────────────────

CRAWL_PATHS = ("/", "/about", "/about-us", "/team", "/careers", "/jobs",
               "/contact", "/contact-us", "/people", "/company")
MAX_PAGES = 8
MAX_BYTES = 800_000

EMAIL_IN_TEXT = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}")
# "name [at] company [dot] com" and friends — common anti-scrape obfuscation.
OBFUSCATED = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\s+at\s+|&#64;)\s*"
    r"([A-Za-z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\s+dot\s+)\s*([A-Za-z]{2,63})",
    re.IGNORECASE)

# Things that look like addresses but aren't ones we can mail.
JUNK_LOCAL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
JUNK_SUBSTRINGS = ("example.com", "sentry.io", "wixpress.com", "@2x", "@3x",
                   "yourdomain", "domain.com", "email.com", "test.com",
                   "schema.org", "sentry-next", "@media")


def _normalize_markup(blob: str) -> str:
    """Undo the escaping that hides addresses inside embedded JSON / HTML entities.

    Modern sites inline their page data as JSON, so a real address arrives as
    `\\u003ea href=...\\u003einfo@acme.com`. Without decoding first, the regex
    happily harvests `u003einfo@acme.com` — a plausible-looking address that
    does not exist. Also handles `&#64;`/`&amp;` entity obfuscation.
    """
    if not blob:
        return ""
    try:
        blob = re.sub(r"\\u([0-9a-fA-F]{4})",
                      lambda m: chr(int(m.group(1), 16)), blob)
    except (ValueError, OverflowError):
        pass
    blob = blob.replace("\\/", "/").replace("\\@", "@")
    return html.unescape(blob)


def _clean_emails(blob: str) -> set[str]:
    blob = _normalize_markup(blob)
    found = {m.group(0).lower().strip(".,;:)>\"'") for m in EMAIL_IN_TEXT.finditer(blob)}
    for m in OBFUSCATED.finditer(blob):
        found.add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower())
    out = set()
    for e in found:
        if any(s in e for s in JUNK_SUBSTRINGS):
            continue
        if any(e.endswith(s) for s in JUNK_LOCAL_SUFFIXES):
            continue
        if len(e) > 100 or e.count("@") != 1:
            continue
        local, _, dom = e.partition("@")
        if not local or not dom or "." not in dom:
            continue
        # Hex blobs (tracking pixels, minified junk) aren't people.
        if re.fullmatch(r"[0-9a-f]{16,}", local):
            continue
        out.add(e)
    return out


def _domain_matches(email: str, domain: str) -> bool:
    """Accept the exact domain and its subdomains (mail.acme.com, eu.acme.com)."""
    dom = email.rsplit("@", 1)[1].lower()
    domain = domain.lower()
    return dom == domain or dom.endswith("." + domain)


_robots_cache: dict[str, RobotFileParser | None] = {}


def _robots_for(base: str) -> RobotFileParser | None:
    """Fetch and parse robots.txt once per host, using our real User-Agent.

    Deliberately NOT `RobotFileParser.read()`: that uses urllib's default UA,
    which Cloudflare-fronted sites (sarvam.ai, postman.com, atlan.com all do
    this) answer with 403 — and the stdlib parser interprets a 403 as
    "disallow everything". That silently turned the crawler off on exactly the
    startups this tool is most useful for. Returns None when there is no usable
    robots.txt, which means "no restrictions stated".
    """
    host = urlparse(base).netloc.lower()
    if host in _robots_cache:
        return _robots_cache[host]
    rp: RobotFileParser | None = None
    try:
        r = requests.get(urljoin(base, "/robots.txt"),
                         headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        # 4xx/5xx → no enforceable policy published to us; proceed politely.
        if r.ok and "html" not in (r.headers.get("Content-Type") or "").lower():
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
    except requests.RequestException:
        rp = None
    _robots_cache[host] = rp
    return rp


def _robots_ok(base: str, path: str, agent: str = "JobPilot") -> bool:
    """Respect robots.txt. Absent/unreadable policy → allowed, which is the
    convention for a single-digit number of polite, non-recursive requests."""
    rp = _robots_for(base)
    if rp is None:
        return True
    try:
        return rp.can_fetch(agent, urljoin(base, path))
    except Exception:  # noqa: BLE001
        return True


def crawl_site_for_emails(domain: str, paths: tuple[str, ...] = CRAWL_PATHS,
                          max_pages: int = MAX_PAGES, timeout: int = HTTP_TIMEOUT,
                          delay: float = 0.4) -> list[ContactCandidate]:
    """Fetch a handful of the company's own pages and harvest on-domain addresses.

    These are the highest-quality free signal available: an address the company
    published itself. Role addresses (`careers@`) are kept but scored below
    personal ones — they are lower value, but they are legitimately deliverable.
    """
    if not domain:
        return []
    base = f"https://{domain}"
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml"})

    hits: dict[str, str] = {}   # email → page it was found on
    pages = 0
    for path in paths:
        if pages >= max_pages:
            break
        if not _robots_ok(base, path):
            continue
        url = urljoin(base, path)
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
        except requests.RequestException:
            continue
        pages += 1
        if not resp.ok or "html" not in (resp.headers.get("Content-Type") or "").lower():
            continue
        body = resp.text[:MAX_BYTES]
        for email in _clean_emails(body):
            if _domain_matches(email, domain):
                hits.setdefault(email, path)
        time.sleep(delay)  # be a polite guest

    out: list[ContactCandidate] = []
    for email, path in sorted(hits.items()):
        local = email.split("@", 1)[0]
        kind = role_kind(local)
        if kind == "undeliverable":
            continue
        if kind == "recruiting":
            conf, title = 62, "Recruiting (role address, published on site)"
        elif kind == "generic":
            conf, title = 48, "Generic inbox (published on site)"
        else:
            conf, title = 68, None  # looks like a named person
        out.append(ContactCandidate(
            email=email, name=None, title=title, source="site-crawl",
            confidence=conf, verified=False,
            notes=[f"found on https://{domain}{path}"]))
    out.sort(key=lambda c: c.confidence, reverse=True)
    if out:
        print(f"    site-crawl: {len(out)} address(es) on {domain} from {pages} page(s)")
    else:
        print(f"    site-crawl: nothing on {domain} ({pages} page(s) fetched)")
    return out


# ── GitHub ───────────────────────────────────────────────────────────

# Circuit breaker: once GitHub says we're out of quota, every further call this
# run would 403 too. Anonymous quota is only 60/hr — roughly three companies —
# so without this a batch run emits a wall of identical warnings and wastes time.
_gh_blocked_until: float = 0.0


def github_rate_limited() -> bool:
    return time.time() < _gh_blocked_until


def _gh(session: requests.Session, path: str, **params) -> tuple[object, int]:
    global _gh_blocked_until
    if github_rate_limited():
        return None, 403
    try:
        r = session.get(f"{GITHUB_API}{path}", params=params, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        return None, 0
    if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
        try:
            reset = float(r.headers.get("X-RateLimit-Reset") or 0)
        except ValueError:
            reset = 0.0
        _gh_blocked_until = reset or (time.time() + 300)
        mins = max(0, int((_gh_blocked_until - time.time()) / 60))
        _warn(f"github: rate limit exhausted, backing off ~{mins}min — "
              f"set GITHUB_TOKEN in .env for 5000 req/hr instead of 60")
        return None, 403
    if not r.ok:
        return None, r.status_code
    try:
        return r.json(), 200
    except ValueError:
        return None, 200


def _org_candidates(company: str, domain: str | None) -> list[str]:
    """Plausible GitHub org slugs, cheapest guesses first."""
    slugs: list[str] = []
    clean = re.sub(r"[^a-z0-9 ]", "", (company or "").lower()).strip()
    if clean:
        slugs += [clean.replace(" ", ""), clean.replace(" ", "-")]
    if domain:
        stem = domain.split(".")[0]
        if stem:
            slugs.append(stem)
    seen, out = set(), []
    for s in slugs:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def find_github_org(company: str, domain: str | None = None,
                    token: str | None = None) -> str | None:
    """Resolve a company to its GitHub org login, verifying via the org's blog URL."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/vnd.github+json"})
    if token:
        session.headers["Authorization"] = f"Bearer {token}"

    def _confirm(data: dict, slug: str | None = None) -> str | None:
        """An org only counts if we can tie it to the target domain."""
        if not isinstance(data, dict) or not data.get("login"):
            return None
        if not domain:
            return data["login"]
        blob = " ".join(str(data.get(k) or "")
                        for k in ("blog", "name", "email", "description")).lower()
        if domain.lower() in blob:
            return data["login"]
        # A slug derived from the domain stem is decent evidence on its own.
        if slug and slug == domain.split(".")[0]:
            return data["login"]
        return None

    for slug in _org_candidates(company, domain):
        data, _ = _gh(session, f"/orgs/{slug}")
        login = _confirm(data if isinstance(data, dict) else {}, slug)
        if login:
            return login

    # Fallback: many orgs aren't named after the company ("postman" → "postmanlabs").
    # The search API is 10 req/min unauthenticated, so this runs only on a miss.
    results, _ = _gh(session, "/search/users",
                     q=f"{company} type:org", per_page=5)
    for item in (results or {}).get("items", [])[:5] if isinstance(results, dict) else []:
        login = item.get("login") if isinstance(item, dict) else None
        if not login:
            continue
        data, _ = _gh(session, f"/orgs/{login}")
        confirmed = _confirm(data if isinstance(data, dict) else {})
        if confirmed:
            return confirmed
    return None


def github_emails(company: str, domain: str | None = None, token: str | None = None,
                  max_repos: int = 4, max_members: int = 12) -> list[ContactCandidate]:
    """Harvest on-domain addresses from a company's public GitHub activity.

    Two seams, both public and both free:
      * `/orgs/{org}/public_members` → each member's public `email` field.
      * recent commits on the org's most-recently-pushed repos → `commit.author.email`,
        which is whatever git identity the developer had configured locally. That
        is very often their real corporate address.

    `users.noreply.github.com` addresses are skipped — they are unroutable.
    Anonymous limit is 60 req/hr; a GITHUB_TOKEN raises it to 5000.
    """
    if not domain:
        _warn("github: no domain to match against — skipping")
        return []
    if github_rate_limited():
        _warn("github: still rate-limited from an earlier call — skipping")
        return []

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/vnd.github+json"})
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        print("    github: no GITHUB_TOKEN — running anonymously (60 req/hr)")

    org = find_github_org(company, domain, token)
    if not org:
        print(f"    github: no org found for {company!r}")
        return []
    print(f"    github: org → {org}")

    found: dict[str, ContactCandidate] = {}

    def _add(email: str, name: str | None, why: str, conf: int) -> None:
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            return
        if email.endswith("users.noreply.github.com") or "noreply" in email:
            return
        if not _domain_matches(email, domain):
            return
        if email in found:
            if name and not found[email].name:
                found[email].name = name
            return
        found[email] = ContactCandidate(
            email=email, name=name, source="github", confidence=conf,
            verified=False, notes=[why])

    # 1. Public org members with a public email.
    members, _ = _gh(session, f"/orgs/{org}/public_members", per_page=max_members)
    for m in (members or [])[:max_members]:
        login = m.get("login") if isinstance(m, dict) else None
        if not login:
            continue
        user, _ = _gh(session, f"/users/{login}")
        if isinstance(user, dict) and user.get("email"):
            _add(user["email"], user.get("name"),
                 f"public GitHub profile email (@{login})", 70)

    # 2. Commit author emails on recently-pushed repos.
    repos, _ = _gh(session, f"/orgs/{org}/repos", sort="pushed", per_page=max_repos)
    for repo in (repos or [])[:max_repos]:
        name = repo.get("name") if isinstance(repo, dict) else None
        if not name or repo.get("fork"):
            continue
        commits, _ = _gh(session, f"/repos/{org}/{name}/commits", per_page=100)
        for c in commits or []:
            if not isinstance(c, dict):
                continue
            author = ((c.get("commit") or {}).get("author") or {})
            _add(author.get("email", ""), author.get("name"),
                 f"git commit author in {org}/{name}", 60)

    out = sorted(found.values(), key=lambda c: c.confidence, reverse=True)
    print(f"    github: {len(out)} on-domain address(es) from {org}")
    return out


# ── Google Programmable Search (optional key) ────────────────────────

CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"


def google_cse_search(company: str, domain: str, key: str | None = None,
                      cx: str | None = None, max_results: int = 10) -> list[ContactCandidate]:
    """Programmable Search for published recruiter addresses. 100 queries/day free."""
    if not (key and cx):
        _warn("cse: GOOGLE_CSE_KEY / GOOGLE_CSE_CX not set — skipping")
        return []
    if not domain:
        return []

    name = (company or "").strip()
    queries = [
        f'"@{domain}" (recruiter OR "talent acquisition" OR hiring)',
        (f'"{name}" "@{domain}" (careers OR jobs OR recruiting) email'
         if name else f'"@{domain}" (careers OR jobs) email'),
    ]
    found: dict[str, ContactCandidate] = {}
    for q in queries:
        try:
            r = requests.get(CSE_ENDPOINT, timeout=HTTP_TIMEOUT,
                             params={"key": key, "cx": cx, "q": q,
                                     "num": min(10, max_results)})
        except requests.RequestException as e:
            _warn(f"cse: request failed ({type(e).__name__})")
            continue
        if not r.ok:
            _warn(f"cse: HTTP {r.status_code} — {r.text[:140]}")
            continue
        for item in (r.json() or {}).get("items", []) or []:
            blob = " ".join(str(item.get(k) or "") for k in
                            ("title", "snippet", "htmlSnippet", "link"))
            recruiterish = _is_recruiterish(item.get("title"), item.get("snippet"))
            for email in _clean_emails(blob):
                if not _domain_matches(email, domain):
                    continue
                if role_kind(email.split("@")[0]) == "undeliverable":
                    continue
                if email in found:
                    continue
                found[email] = ContactCandidate(
                    email=email, source="cse",
                    confidence=55 if recruiterish else 45, verified=False,
                    notes=[f"Google CSE result: {(item.get('link') or '')[:120]}"])
    out = sorted(found.values(), key=lambda c: c.confidence, reverse=True)
    print(f"    cse: {len(out)} address(es) for {domain}")
    return out


# ── LinkedIn names (no emails — those come from pattern generation) ──

RECRUITER_QUERIES = ("recruiter", "talent acquisition", "technical recruiter",
                     "head of people")
NAME_RE = re.compile(r"^[A-Z][A-Za-z''\-]+(?: [A-Z][A-Za-z''\-]+){1,2}$")


def linkedin_recruiter_names(company: str, settings: dict | None = None,
                             max_people: int = 10,
                             queries: tuple[str, ...] = RECRUITER_QUERIES[:2]
                             ) -> list[ContactCandidate]:
    """Find recruiter/talent NAMES at a company via the saved LinkedIn session.

    Returns `ContactCandidate`s with `name`/`title`/`linkedin_url` but **no
    email** — feeding `finder.pattern_guess`, which is then SMTP-verified.

    Safety, reusing `jobpilot.linkedin.session`:
      * uses the existing cookie jar; never touches credentials
      * `human_delay` between every action
      * `detect_blocker` after every navigation, and aborts the whole run on any
        CAPTCHA/checkpoint rather than trying to get past it
      * missing/expired session → warn and return [], never raise
    """
    if not company:
        return []
    try:
        from playwright.sync_api import sync_playwright

        from ..linkedin.session import (BlockerDetected, detect_blocker, human_delay,
                                        launch, looks_logged_out, session_path)
    except ImportError as e:
        _warn(f"linkedin: playwright unavailable ({e}) — skipping")
        return []

    state = session_path(settings)
    if not state.exists() or state.stat().st_size == 0:
        _warn("linkedin: no saved session (.linkedin_session.json) — skipping. "
              "Run the LinkedIn login flow first to enable this source.")
        return []

    people: dict[str, ContactCandidate] = {}
    browser = context = None
    try:
        with sync_playwright() as pw:
            browser, context = launch(settings, pw)
            page = context.new_page()
            for q in queries:
                if len(people) >= max_people:
                    break
                url = ("https://www.linkedin.com/search/results/people/"
                       f"?keywords={requests.utils.quote(f'{company} {q}')}")
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                except Exception as e:  # noqa: BLE001
                    _warn(f"linkedin: navigation failed ({type(e).__name__}) — skipping")
                    break
                blocker = detect_blocker(page, status=resp.status if resp else None)
                if blocker:
                    _warn(f"linkedin: blocker detected ({blocker}) — aborting this source")
                    break
                if looks_logged_out(page.url):
                    _warn("linkedin: session expired — skipping (re-run the login flow)")
                    break
                human_delay(settings, scale=0.6)

                try:
                    cards = page.locator("li.reusable-search__result-container, "
                                         "div.entity-result, li.artdeco-list__item")
                    count = min(cards.count(), max_people)
                except Exception:  # noqa: BLE001
                    count = 0
                for i in range(count):
                    try:
                        card = cards.nth(i)
                        text = card.inner_text()
                    except Exception:  # noqa: BLE001
                        continue
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    name = next((ln for ln in lines if NAME_RE.match(ln)
                                 and "LinkedIn" not in ln), None)
                    title = next((ln for ln in lines[1:]
                                  if _is_recruiterish(ln)), None)
                    if not name or not title:
                        continue
                    href = None
                    try:
                        href = card.locator("a[href*='/in/']").first.get_attribute("href")
                        href = (href or "").split("?")[0] or None
                    except Exception:  # noqa: BLE001
                        pass
                    key = name.lower()
                    if key not in people:
                        people[key] = ContactCandidate(
                            name=name, title=title, linkedin_url=href,
                            source="linkedin", confidence=35, verified=False,
                            notes=["name only — email must be pattern-guessed + verified"])
                human_delay(settings, scale=1.0)
    except BlockerDetected as e:
        _warn(f"linkedin: aborted — {e}")
    except Exception as e:  # noqa: BLE001
        _warn(f"linkedin: unavailable ({type(e).__name__}: {e}) — skipping")
    finally:
        for obj in (context, browser):
            try:
                if obj:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass

    out = list(people.values())[:max_people]
    print(f"    linkedin: {len(out)} recruiter name(s) for {company}")
    return out


# ── availability ─────────────────────────────────────────────────────

def free_source_availability(env: dict | None = None) -> dict[str, bool]:
    """Which free sources can run right now. Only CSE needs a key."""
    import os

    from ..llm import _load_env
    _load_env()
    e = env or os.environ
    from ..linkedin.session import session_path
    state = session_path(None)
    return {
        "site-crawl": True,
        "github": True,                                   # anonymous works
        "github-token": bool(e.get("GITHUB_TOKEN")),      # just raises the limit
        "cse": bool(e.get("GOOGLE_CSE_KEY") and e.get("GOOGLE_CSE_CX")),
        "linkedin": state.exists() and state.stat().st_size > 0,
    }
