"""LinkedIn browser session: launch, manual-login handshake, pacing, blocker detection.

Design rules baked in here (see NOTES.md):

* **We never touch the password.** `ensure_logged_in` opens a visible browser and
  waits for *the human* to sign in. Afterwards the cookie jar is persisted with
  Playwright's `storage_state` and reused. There is no credential-entry code path
  in this package, deliberately — grep for `fill` on a password field and you will
  find nothing.
* **Every action is paced.** `human_delay` sleeps a random 4-12s (configurable).
  Nothing in this package fires two actions back to back.
* **Every navigation is screened.** `detect_blocker` must be called after each
  `goto`; if LinkedIn shows a CAPTCHA / checkpoint / "unusual activity" page we
  abort the whole run rather than trying to get past it.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# A plain, current desktop Chrome UA. Playwright's default UA advertises
# HeadlessChrome, which is an instant red flag.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}

FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_URL = "https://www.linkedin.com/login"
JOBS_URL = "https://www.linkedin.com/jobs/"

# Conservative fallbacks — used whenever settings.yaml is missing/short a key.
# Note every default is the *safe* one: dry run on, headless off, small cap.
DEFAULTS = {
    "dry_run": True,
    "headless": False,
    "daily_apply_cap": 15,
    "min_score_to_apply": 75,
    "easy_apply_only": True,
    "session_file": ".linkedin_session.json",
    "min_delay_seconds": 4.0,
    "max_delay_seconds": 12.0,
}


class BlockerDetected(RuntimeError):
    """LinkedIn showed a CAPTCHA / checkpoint / restriction page. Abort the run."""


class LoginRequired(RuntimeError):
    """No usable session and the user did not complete a manual login in time."""


# ── Settings ─────────────────────────────────────────────────────────

def linkedin_cfg(settings: dict | None) -> dict:
    """The `linkedin:` block, with safe defaults filled in for anything missing.

    Malformed values fall back to the default rather than raising — a typo in
    settings.yaml must never silently turn dry-run off or delays to zero.
    """
    cfg = dict(DEFAULTS)
    block = (settings or {}).get("linkedin") or {}
    if not isinstance(block, dict):
        return cfg
    for key, default in DEFAULTS.items():
        if key not in block or block[key] is None:
            continue
        val = block[key]
        try:
            if isinstance(default, bool):
                cfg[key] = bool(val)
            elif isinstance(default, float):
                cfg[key] = float(val)
            elif isinstance(default, int):
                cfg[key] = int(val)
            else:
                cfg[key] = str(val)
        except (TypeError, ValueError):
            cfg[key] = default  # unparseable → keep the safe default

    lo, hi = cfg["min_delay_seconds"], cfg["max_delay_seconds"]
    cfg["min_delay_seconds"] = max(0.0, lo)
    cfg["max_delay_seconds"] = max(cfg["min_delay_seconds"], hi)
    cfg["daily_apply_cap"] = max(0, cfg["daily_apply_cap"])
    return cfg


def is_dry_run(settings: dict | None) -> bool:
    """True unless dry_run was explicitly, parseably set to false."""
    return bool(linkedin_cfg(settings)["dry_run"])


def session_path(settings: dict | None, root: Path = ROOT) -> Path:
    p = Path(linkedin_cfg(settings)["session_file"]).expanduser()
    return p if p.is_absolute() else root / p


# ── Pacing ───────────────────────────────────────────────────────────

def delay_seconds(settings: dict | None, scale: float = 1.0,
                  rng: random.Random | None = None) -> float:
    """Pure: draw a randomised delay inside the configured band (× `scale`).

    Split out from `human_delay` so the bounds can be unit-tested without
    actually sleeping.
    """
    cfg = linkedin_cfg(settings)
    r = rng or random
    return r.uniform(cfg["min_delay_seconds"], cfg["max_delay_seconds"]) * max(0.0, scale)


def human_delay(settings: dict | None, scale: float = 1.0, why: str | None = None,
                log=None, _sleep=time.sleep) -> float:
    """Sleep a randomised, human-plausible interval. Returns seconds slept."""
    secs = delay_seconds(settings, scale)
    if log and why:
        log(f"    … waiting {secs:.1f}s ({why})")
    _sleep(secs)
    return secs


def jitter_type(locator, text: str, settings: dict | None = None) -> None:
    """Type into a field character-by-character instead of pasting."""
    cfg = linkedin_cfg(settings)
    per_char = max(20, min(120, int(cfg["min_delay_seconds"] * 15)))
    locator.click()
    locator.fill("")
    locator.type(text, delay=per_char)


# ── Blocker detection ────────────────────────────────────────────────

# URL fragments that mean "LinkedIn wants to verify you", never a normal page.
BLOCKER_URL_PATTERNS = [
    (r"/checkpoint/challenge", "security checkpoint / challenge page"),
    (r"/checkpoint/lg/login-submit", "login checkpoint"),
    (r"/checkpoint/rp/request-password-reset", "password-reset checkpoint"),
    (r"/checkpoint/", "LinkedIn checkpoint page"),
    (r"/uas/consumer-email-challenge", "email verification challenge"),
    (r"security-verification", "security verification page"),
    (r"[?&]challengeId=", "challenge flow"),
    (r"/captcha", "CAPTCHA page"),
]

# Phrases LinkedIn uses on interstitials. Matched against the *page title* and
# the first slice of body text only — matching whole-page text would false-positive
# on job descriptions that happen to contain e.g. "unusual".
BLOCKER_TEXT_PATTERNS = [
    (r"let('|’)?s do a quick security check", "security check interstitial"),
    (r"quick security check", "security check interstitial"),
    (r"unusual activity", "'unusual activity' interstitial"),
    (r"we('|’)?ve restricted your account", "account restricted"),
    (r"your account has been (temporarily )?restricted", "account restricted"),
    (r"account (has been )?suspended", "account suspended"),
    (r"verify (that )?you('|’)?re (a )?human", "human-verification challenge"),
    (r"verify you are (a )?human", "human-verification challenge"),
    (r"prove you('|’)?re not a robot", "robot check"),
    (r"complete this security check", "security check"),
    (r"solve this puzzle", "CAPTCHA puzzle"),
    (r"we detected unusual", "unusual-activity detection"),
    (r"too many requests", "rate limited"),
    (r"you('|’)?ve reached the (weekly|monthly) (invitation|application) limit",
     "LinkedIn usage limit reached"),
]

# Third-party CAPTCHA widgets LinkedIn embeds.
BLOCKER_DOM_PATTERNS = [
    (r"arkoselabs\.com", "Arkose Labs CAPTCHA widget"),
    (r"funcaptcha", "FunCaptcha widget"),
    (r"recaptcha/api", "reCAPTCHA widget"),
    (r"hcaptcha\.com", "hCaptcha widget"),
    (r'id="captcha-internal"', "LinkedIn internal CAPTCHA"),
    (r'class="[^"]*challenge-dialog', "challenge dialog"),
    (r"captcha-challenge", "CAPTCHA challenge element"),
]

# HTTP statuses LinkedIn returns when it is throttling or blocking a client.
BLOCKER_STATUSES = {429: "HTTP 429 rate limited", 999: "HTTP 999 (LinkedIn bot block)"}

TEXT_SCAN_CHARS = 3000


def classify_blocker(url: str = "", title: str = "", text: str = "",
                     html: str = "", status: int | None = None) -> str | None:
    """Pure blocker classifier — the whole of `detect_blocker`'s logic.

    Kept browser-free so it can be tested against saved sample HTML.
    Returns a human description of the blocker, or None if the page looks normal.
    """
    if status is not None and status in BLOCKER_STATUSES:
        return BLOCKER_STATUSES[status]

    low_url = (url or "").lower()
    for pattern, desc in BLOCKER_URL_PATTERNS:
        if re.search(pattern, low_url):
            return f"{desc} (url: {url[:120]})"

    haystack = f"{title or ''}\n{(text or '')[:TEXT_SCAN_CHARS]}".lower()
    for pattern, desc in BLOCKER_TEXT_PATTERNS:
        if re.search(pattern, haystack):
            return desc

    low_html = (html or "").lower()
    for pattern, desc in BLOCKER_DOM_PATTERNS:
        if re.search(pattern, low_html):
            return desc
    return None


def detect_blocker(page, status: int | None = None) -> str | None:
    """Inspect a live page for a CAPTCHA / checkpoint / restriction interstitial.

    Call this after EVERY navigation. Returns a description, or None.
    """
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001 — a dead page is not a blocker, just noise
        return None
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = ""
    try:
        text = page.inner_text("body")
    except Exception:  # noqa: BLE001
        text = ""
    try:
        # Only the head of the document: enough for widget/iframe markers,
        # cheap, and avoids scanning megabytes of feed HTML.
        html = page.content()[:200_000]
    except Exception:  # noqa: BLE001
        html = ""
    return classify_blocker(url=url, title=title, text=text, html=html, status=status)


def guard(page, status: int | None = None, log=print) -> None:
    """detect_blocker + abort. Raises BlockerDetected so the run unwinds cleanly."""
    blocker = detect_blocker(page, status=status)
    if blocker:
        log(f"\n  ✖ ABORTING — LinkedIn blocker detected: {blocker}")
        log("    Not attempting to solve or evade it. Open LinkedIn in your normal")
        log("    browser, clear the challenge by hand, then re-run later.")
        raise BlockerDetected(blocker)


# ── Login state ──────────────────────────────────────────────────────

LOGGED_OUT_URL_MARKERS = ("/login", "/authwall", "/uas/login", "/signup",
                          "/checkpoint/lg", "linkedin.com/home")


def looks_logged_out(url: str) -> bool:
    low = (url or "").lower()
    return any(marker in low for marker in LOGGED_OUT_URL_MARKERS)


def has_auth_cookie(context) -> bool:
    """`li_at` is LinkedIn's session cookie — its presence is the strongest signal."""
    try:
        return any(c.get("name") == "li_at" and c.get("value") for c in context.cookies())
    except Exception:  # noqa: BLE001
        return False


def is_logged_in(page, context=None) -> bool:
    """Navigate to the feed and decide whether we have an authenticated session."""
    try:
        resp = page.goto(FEED_URL, wait_until="domcontentloaded", timeout=45_000)
    except Exception:  # noqa: BLE001 — network hiccup ≠ logged out, but treat as such
        return False
    guard(page, status=resp.status if resp else None)
    page.wait_for_timeout(2500)

    if looks_logged_out(page.url):
        return False
    if context is not None and not has_auth_cookie(context):
        return False
    # Belt and braces: the authenticated shell renders the global nav.
    for sel in ("#global-nav", "nav.global-nav", "[data-test-global-nav]",
                "input.search-global-typeahead__input", "div.feed-identity-module"):
        try:
            if page.locator(sel).count():
                return True
        except Exception:  # noqa: BLE001
            continue
    # Nav selectors churn; if we're on /feed with a session cookie, believe it.
    return "/feed" in page.url


# ── Launch / login ───────────────────────────────────────────────────

@dataclass
class LaunchResult:
    browser: object
    context: object


def launch(settings: dict | None, playwright):
    """Start Chromium and build a context with the saved session applied.

    Returns `(browser, context)`. Honours `headless` (default False so the user
    can watch), sets a real desktop UA/viewport, and loads `session_file` if present.
    """
    cfg = linkedin_cfg(settings)
    browser = playwright.chromium.launch(
        headless=bool(cfg["headless"]),
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
        ],
    )
    state = session_path(settings)
    ctx_kwargs = dict(
        user_agent=DEFAULT_UA,
        viewport=dict(DEFAULT_VIEWPORT),
        locale="en-US",
        timezone_id="Asia/Kolkata",
        device_scale_factor=2,
    )
    if state.exists() and state.stat().st_size > 0:
        ctx_kwargs["storage_state"] = str(state)
    context = browser.new_context(**ctx_kwargs)
    context.set_default_timeout(45_000)
    # navigator.webdriver === true is the cheapest bot tell there is.
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return browser, context


def save_session(context, settings: dict | None, log=print) -> Path:
    """Persist cookies to `session_file` (0600 — it is a session credential)."""
    path = session_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    log(f"  ✔ Session saved to {path} (cookies only — no password is ever stored)")
    return path


LOGIN_BANNER = """
╔══════════════════════════════════════════════════════════════════════╗
║  MANUAL LOGIN NEEDED                                                 ║
╠══════════════════════════════════════════════════════════════════════╣
║  A Chromium window is open on the LinkedIn login page.               ║
║                                                                      ║
║    1. Sign in THERE, by hand, as you normally would.                 ║
║    2. Complete any 2FA / email code LinkedIn asks for.               ║
║    3. Wait until your feed loads. Then leave the window alone.       ║
║                                                                      ║
║  JobPilot never sees, asks for, or stores your password — it only    ║
║  saves the resulting session cookie so you don't repeat this.        ║
║  Waiting up to {mins} minutes…                                             ║
╚══════════════════════════════════════════════════════════════════════╝
"""


def ensure_logged_in(context, settings: dict | None, timeout_seconds: int = 300,
                     poll_seconds: float = 5.0, log=print) -> bool:
    """Guarantee an authenticated session, prompting for manual login if needed.

    Returns True once the feed is reachable. Raises `BlockerDetected` if LinkedIn
    throws a challenge, `LoginRequired` if the user never finishes logging in.
    """
    page = context.new_page()
    try:
        if is_logged_in(page, context):
            log("  ✔ Existing LinkedIn session is valid.")
            save_session(context, settings, log=log)  # refresh rotated cookies
            return True

        cfg = linkedin_cfg(settings)
        if cfg["headless"]:
            # We refuse to "handle" login invisibly — the user must be able to see it.
            raise LoginRequired(
                "No valid LinkedIn session and headless=true. Set linkedin.headless "
                "to false in config/settings.yaml and re-run so you can log in "
                "manually in the visible browser window."
            )

        log(LOGIN_BANNER.format(mins=max(1, timeout_seconds // 60)))
        resp = page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
        guard(page, status=resp.status if resp else None, log=log)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(poll_seconds)
            try:
                url = page.url
            except Exception:  # noqa: BLE001 — user may have closed/replaced the tab
                break
            blocker = detect_blocker(page)
            if blocker:
                # A challenge during a *manual* login is the user's to solve in
                # the window; only abort if it persists past the deadline.
                log(f"    (LinkedIn is showing: {blocker} — solve it in the window)")
                continue
            if not looks_logged_out(url) and has_auth_cookie(context):
                log("  ✔ Login detected.")
                if is_logged_in(page, context):
                    save_session(context, settings, log=log)
                    return True

        raise LoginRequired(
            f"Manual login not completed within {timeout_seconds}s. Re-run when "
            "you have a moment to sign in."
        )
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def goto(page, url: str, settings: dict | None = None, wait_until: str = "domcontentloaded",
         log=print, pace: bool = True):
    """Navigate + blocker-guard + pace. The only navigation helper this package uses."""
    resp = page.goto(url, wait_until=wait_until, timeout=60_000)
    status = resp.status if resp else None
    guard(page, status=status, log=log)
    if pace:
        human_delay(settings, scale=0.5)
    return resp
