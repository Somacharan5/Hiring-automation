"""Per-ATS application-form adapters (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Recruitee) plus a generic label-driven fallback.

Design, in order of importance:

1. **We refuse rather than invent.** Answers come from `config/profile.yaml` via
   `ProfileAnswerer` (reused verbatim from the LinkedIn package — it is the same
   honesty policy, already unit-tested). If a *required* field has no honest
   answer, `fill()` returns `blocked=<question>` and the caller records the job
   for manual attention. The one deliberate exception is EEO/demographic
   questions, which almost always offer "Decline to self-identify" — selecting
   that is a real, honest answer, so `PortalAnswerer` prefers it.
2. **The adapters are thin.** One generic engine does the scraping, label
   matching, answering and writing. An adapter only supplies: how to reach the
   application form, where the form lives (page vs iframe), which file input is
   the résumé, how to press a dropdown, and what "submitted" looks like. So a
   provider whose selectors we have not verified live still degrades to the
   generic engine rather than to garbage.
3. **Selectors are centralised** at the top of each adapter class, because they
   *will* drift. Field matching never depends on a single attribute: we resolve a
   label from `aria-label` → `label[for=]` → `aria-labelledby` → a wrapping
   `<label>` → the enclosing field container → placeholder → `name`, and match
   case-insensitively with fuzzy contains.

Everything below the "Pure logic" banner is browser-free and unit-testable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from ..linkedin.answers import (VOCABULARY_KEYS, Answer, FieldSpec, ProfileAnswerer,
                                find_other_option, match_option,
                                normalise, question_key, unanswerable)

CHANNEL = "portal"

# Conservative fallbacks for every settings key. Note each default is the *safe*
# one: dry run on, headless off, optional questions left alone.
DEFAULTS = {
    "dry_run": True,
    "headless": False,
    "daily_apply_cap": 25,
    "min_score_to_apply": 70,
    "answer_optional_questions": False,
    "min_delay_seconds": 4.0,
    "max_delay_seconds": 12.0,
}


class BlockerDetected(RuntimeError):
    """A CAPTCHA challenge / Cloudflare interstitial / bot wall. Abort the run."""


# ── Settings ─────────────────────────────────────────────────────────

def portal_cfg(settings: dict | None) -> dict:
    """The `portal:` block with safe defaults filled in for anything missing.

    Malformed values fall back to the default rather than raising — a typo in
    settings.yaml must never silently turn dry-run off.
    """
    cfg = dict(DEFAULTS)
    block = (settings or {}).get("portal") or {}
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
            cfg[key] = default
    cfg["min_delay_seconds"] = max(0.0, cfg["min_delay_seconds"])
    cfg["max_delay_seconds"] = max(cfg["min_delay_seconds"], cfg["max_delay_seconds"])
    cfg["daily_apply_cap"] = max(0, cfg["daily_apply_cap"])
    return cfg


def is_dry_run(settings: dict | None) -> bool:
    """True unless `portal.dry_run` was explicitly, parseably set to false."""
    return bool(portal_cfg(settings)["dry_run"])


def pacing(settings: dict | None) -> dict:
    """Adapt portal delays to the shape `linkedin.session.human_delay` expects.

    `human_delay` reads its band out of `settings['linkedin']`; rather than
    duplicate the pacing helper we hand it a settings-shaped shim built from the
    `portal:` block, so both channels share one implementation.
    """
    cfg = portal_cfg(settings)
    return {"linkedin": {"min_delay_seconds": cfg["min_delay_seconds"],
                         "max_delay_seconds": cfg["max_delay_seconds"]}}


# ══ Pure logic (no browser) ══════════════════════════════════════════

# ── EEO / demographics ───────────────────────────────────────────────

EEO_PATTERNS = re.compile(
    r"\b(gender|gender identity|sex\b|race|ethnic|ethnicity|hispanic|latino|"
    r"veteran|protected veteran|disability|disabilit|sexual orientation|"
    r"pronoun|transgender|lgbt|national origin|self[- ]?identif|"
    r"eeo|eeoc|equal employment|voluntary self)\b"
)

# Options that mean "I choose not to answer" — a truthful selection, not a guess.
DECLINE_PATTERNS = re.compile(
    r"decline|prefer not|prefer to not|do ?n[o']?t wish|do not wish|"
    r"don'?t want|do not want|choose not|rather not|not to answer|"
    r"no answer|not disclose|not specif|opt out|i don'?t know|unspecified"
)

# Attesting *as* the user is impersonation, even when the underlying fact is
# true. These always go to the manual queue.
SIGNATURE_PATTERNS = re.compile(
    r"\bsignature|sign here|e-?sign|initials?\b|type your (full )?name to (sign|certify)|"
    r"i certify|i attest|i acknowledge|electronic signature\b"
)

# ── "years of experience" with a *named* thing ───────────────────────
#
# `answers.parse_years_question` classifies "How many years of experience with
# Jira?" as tool-specific and refuses, but misses the equally common inverted
# phrasing "How many years of experience DO YOU HAVE WITH Jira?" — it only looks
# for the qualifier immediately adjacent to the word "experience". Answering that
# one from `years_experience` is exactly the classic automated-apply lie, so we
# re-check it here: any preposition + named subject appearing after "experience"
# means the question is about a specific tool/domain, not a career length.
_TOOL_AFTER_EXPERIENCE = re.compile(
    r"experience\b[^.?]*?\b(?:with|using|in|on|as an?|as a|for)\s+"
    r"(?P<qual>[a-z0-9][a-z0-9+#/.'&-]*(?:\s+[a-z0-9+#/.'&-]+){0,3})"
)
# Tails that still mean "your whole career", not a named tool.
_GENERIC_TAILS = {
    "", "total", "work", "working", "professional", "industry", "the industry",
    "full time", "full-time", "career", "your career", "a professional setting",
    "a professional environment", "the workforce", "employment", "general",
    "product", "product management", "years", "total years",
}


def asks_about_a_named_tool(label: str) -> bool:
    """True when a years-of-experience question names a specific tool or domain."""
    text = normalise(label)
    if "experience" not in text or "year" not in text:
        return False
    m = _TOOL_AFTER_EXPERIENCE.search(text)
    if not m:
        return False
    qual = re.sub(r"\b(do you have|have you|of|the|a|an)\b", " ",
                  m.group("qual")).strip()
    qual = re.sub(r"\s+", " ", qual)
    return qual not in _GENERIC_TAILS


def is_eeo_question(*texts: str) -> bool:
    """True if any of the given label/name strings looks demographic."""
    for text in texts:
        if text and EEO_PATTERNS.search(normalise(text)):
            return True
    return False


def is_signature_field(*texts: str) -> bool:
    for text in texts:
        if text and SIGNATURE_PATTERNS.search(normalise(text)):
            return True
    return False


def pick_decline_option(options) -> str | None:
    """The 'decline to self-identify' option from a list, or None if absent.

    Prefers the most explicit decline wording so we never pick, say, "I don't
    know" when a real "Decline to self-identify" exists.
    """
    ranked: list[tuple[int, str]] = []
    for opt in options or ():
        text = (opt or "").strip()
        if not text:
            continue
        low = normalise(text)
        if not DECLINE_PATTERNS.search(low):
            continue
        rank = 0
        if "decline" in low:
            rank = 3
        elif "wish" in low or "want" in low or "prefer" in low or "choose" in low:
            rank = 2
        else:
            rank = 1
        ranked.append((rank, text))
    if not ranked:
        return None
    ranked.sort(key=lambda t: (-t[0], len(t[1])))
    return ranked[0][1]


# Address fields that portals require and LinkedIn never asks for. Each is
# answered *only* from an explicit profile key — we never split "Hyderabad,
# India" and hope, because "Hyderabad, Telangana" would silently become a country.
CURRENT_COMPANY_RE = re.compile(
    r"\b(current|present|most recent) (company|employer|organi[sz]ation)\b|"
    r"^(company|employer|organi[sz]ation)$|\bwhere do you (currently )?work\b")
# A GitHub field is not a portfolio field: the LinkedIn answerer maps both to
# `portfolio`, which would put a personal site into a GitHub box. Portals list
# them separately, so GitHub is answered only from an explicit profile key.
GITHUB_RE = re.compile(r"\bgit ?hub\b")

ADDRESS_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bcountry\b(?!.*\bcode\b)"), "country"),
    (re.compile(r"\b(post(al)? ?code|zip ?code|pincode|pin code)\b"), "postal_code"),
    (re.compile(r"\b(street address|address line|mailing address|^address$)\b"),
     "address"),
    (re.compile(r"\b(city|town)\b"), "city"),
    (re.compile(r"\b(state|province|region)\b"), "state"),
)


class PortalAnswerer(ProfileAnswerer):
    """`ProfileAnswerer` + the concessions company ATS forms require.

    Three additions over the LinkedIn answerer, all of them still refusals-by-default:

    * **EEO/demographics** — every compliant EEO form offers a decline option.
      Selecting it is a truthful answer and it is what a careful human does.
    * **Address fields** (country / city / postcode) — portals require these and
      LinkedIn does not, so they are answered from explicit `profile.yaml` keys,
      never inferred from another field.
    * **Phone** — written in full international form, because a portal's separate
      country selector defaults to the US and would otherwise mislabel the number.

    Everything else — visa, salary, notice period, "why us", per-tool tenure —
    refuses, inherited unchanged.
    """

    @property
    def current_company(self) -> str:
        """The employer the profile says the user is at *now*, or ''.

        A fact already in `profile.yaml`, not an inference: we take the role whose
        `end` says "present". If several do, the one that started most recently.
        If none does, we return '' and the field is refused — the profile does not
        actually claim a current employer.
        """
        exps = self.profile.get("experiences") or []
        if not isinstance(exps, list):
            return ""
        current = []
        for exp in exps:
            if not isinstance(exp, dict):
                continue
            end = str(exp.get("end") or "").strip().lower()
            if end in ("present", "current", "now", "ongoing"):
                current.append(exp)
        if not current:
            return ""
        current.sort(key=lambda e: str(e.get("start") or ""), reverse=True)
        return str(current[0].get("company") or "").strip()

    def _value_for(self, key: str) -> str:
        if key in ("country", "city", "state", "address", "postal_code", "github"):
            return self._get(key)
        if key == "current_company":
            return self.current_company
        if key == "phone":
            # Full E.164 when the profile records a country code: portals put the
            # country in a separate selector that defaults to the US, so a bare
            # national number would be recorded as a wrong US phone number.
            raw = self._get("phone")
            digits = re.sub(r"[^\d+]", "", raw)
            if digits.startswith("+"):
                return digits
            return super()._value_for(key)
        return super()._value_for(key)

    def answer(self, spec: FieldSpec) -> Answer:
        label = spec.label or spec.name

        if spec.kind == "file":
            return super().answer(spec)

        # Signing on the user's behalf is impersonation — never do it.
        if is_signature_field(label, spec.name, spec.element_id):
            return unanswerable(label, "signature/attestation — only you can sign this")

        if asks_about_a_named_tool(label):
            return unanswerable(
                label, "asks tenure with a specific tool/domain, which the profile "
                       "does not record")

        if is_eeo_question(label, spec.name, spec.element_id, *(spec.options or ())):
            picked = pick_decline_option(spec.options)
            if picked is not None:
                return Answer(ok=True, value=picked,
                              source="eeo:declined-to-self-identify")
            # No decline option offered → fall through; the parent refuses.

        text = normalise(label)

        # Whatever the *site* pre-filled is not an answer from the user.
        #
        # The LinkedIn answerer treats a non-empty field as "already answered",
        # which is safe there — LinkedIn pre-fills from the user's own profile.
        # A company portal pre-fills from IP geolocation and browser locale: a
        # live run found Workable's required Address box already containing
        # "Gurugram, India", a city the profile never mentions, and a Recruitee
        # phone box holding "+31". Submitting either would put a fact the user
        # never stated onto their application. So we blank the pre-set value
        # before deciding, answer from the profile if we can, and refuse if we
        # cannot and the field is required.
        prefilled = spec.value.strip()
        if prefilled and spec.kind in ("text", "textarea", "number", "select"):
            spec = FieldSpec(label=spec.label, kind=spec.kind, options=spec.options,
                             value="", required=spec.required, name=spec.name,
                             element_id=spec.element_id)

        # GitHub must not inherit the parent's portfolio answer.
        if GITHUB_RE.search(text):
            value = self._get("github")
            if not value:
                return unanswerable(
                    label, "profile has no github (add `github:` to config/profile.yaml)")
            return Answer(ok=True, value=value, source="profile:github")

        # Address parts the profile states outright win over the parent's
        # single `location` string — a "City" box should hold "Hyderabad", not
        # "Hyderabad, India". Only explicit keys qualify; we never split
        # `location` and hope, because "Hyderabad, Telangana" would silently
        # turn a state into a country.
        for pattern, key in ADDRESS_RULES:
            if pattern.search(text) and self._get(key):
                picked = match_option(self._get(key), spec.options)
                if picked is not None:
                    return Answer(ok=True, value=picked, source=f"profile:{key}")

        # Ask the parent next, so its refusals always win over the extra rules
        # below; these only fill gaps the LinkedIn answerer never had to cover.
        parent = super().answer(spec)
        if parent.ok:
            return parent

        if CURRENT_COMPANY_RE.search(text):
            value = self.current_company
            if not value:
                return unanswerable(
                    label, "profile lists no role with end: present — only you can "
                           "say where you work now")
            picked = match_option(value, spec.options)
            if picked is None:
                return unanswerable(label, "cannot map current company to the options")
            return Answer(ok=True, value=picked, source="profile:current_company")

        for pattern, key in ADDRESS_RULES:
            if not pattern.search(text):
                continue
            value = self._value_for(key)
            if not value:
                return unanswerable(
                    label, f"profile has no {key} (add `{key}:` to config/profile.yaml)")
            picked = match_option(value, spec.options)
            if picked is None and key in VOCABULARY_KEYS:
                # Same reasoning as the base answerer: a closed school/degree
                # list that omits the candidate's institution is answered
                # truthfully by its own "Other", not by a near-miss.
                other = find_other_option(spec.options)
                if other:
                    return Answer(ok=True, value=other,
                                  source=f"profile:{key}→other (not in this list)")
            if picked is None:
                return unanswerable(label, f"cannot map profile {key} to the options")
            return Answer(ok=True, value=picked, source=f"profile:{key}")
        return parent


# ── URL → application-form URL ───────────────────────────────────────

GREENHOUSE_HOSTS = ("job-boards.greenhouse.io", "boards.greenhouse.io",
                    "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io")


def greenhouse_job_id(url: str) -> str | None:
    """The Greenhouse posting id, from `?gh_jid=` or a `/jobs/<id>` path."""
    parts = urlsplit(url or "")
    jid = (parse_qs(parts.query).get("gh_jid") or [None])[0]
    if jid and jid.isdigit():
        return jid
    m = re.search(r"/jobs/(\d{5,})", parts.path)
    return m.group(1) if m else None


def application_url(url: str, source: str | None = None,
                    company: str | None = None) -> str:
    """Normalise a collected job URL to the URL that actually shows the form.

    Most providers host the posting and the application at different paths;
    Greenhouse additionally lets companies embed the form on their own domain
    (handled at fill time by `GreenhouseAdapter.scope`, not here).
    """
    url = (url or "").strip()
    if not url:
        return url
    low = url.lower()
    src = (source or "").lower()

    if src == "greenhouse" or "greenhouse.io" in low or "gh_jid=" in low:
        host = urlsplit(url).netloc.lower()
        if host in GREENHOUSE_HOSTS:
            return url
        jid = greenhouse_job_id(url)
        # A company-hosted board embeds the Greenhouse iframe; the canonical
        # board URL is equivalent and simpler, so prefer it when we can build it.
        if jid and company:
            slug = re.sub(r"[^a-z0-9]+", "", company.lower())
            if slug:
                return f"https://job-boards.greenhouse.io/{slug}/jobs/{jid}"
        return url

    if src == "lever" or "jobs.lever.co" in low or "jobs.eu.lever.co" in low:
        return url if low.rstrip("/").endswith("/apply") else url.rstrip("/") + "/apply"

    if src == "ashby" or "jobs.ashbyhq.com" in low or "ashbyhq.com" in low:
        if low.rstrip("/").endswith("/application"):
            return url
        return url.rstrip("/") + "/application"

    if src == "workable" or "workable.com" in low:
        if low.rstrip("/").endswith("/apply"):
            return url
        return url.rstrip("/") + "/apply/"

    if src == "recruitee" or "recruitee.com" in low or "/o/" in low:
        if low.rstrip("/").endswith("/c/new"):
            return url
        if "/o/" in low:
            return url.rstrip("/") + "/c/new"
        return url

    # SmartRecruiters: the posting page carries an "I'm interested" link into the
    # apply UI; the adapter clicks it rather than guessing the URL shape.
    return url


# ── Provider detection ───────────────────────────────────────────────

URL_MARKERS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io", "gh_jid="),
    "lever": ("jobs.lever.co", "jobs.eu.lever.co"),
    "ashby": ("ashbyhq.com",),
    "workable": ("workable.com",),
    "smartrecruiters": ("smartrecruiters.com", "smartr.me"),
    "recruitee": ("recruitee.com",),
}


def provider_from_url(url: str) -> str | None:
    low = (url or "").lower()
    for name, markers in URL_MARKERS.items():
        if any(m in low for m in markers):
            return name
    return None


def is_known_ats_url(url: str) -> bool:
    return provider_from_url(url) is not None


def remaining_quota(cap: int, sent_today: int) -> int:
    """Applications still allowed today. Never negative."""
    try:
        return max(0, int(cap) - int(sent_today))
    except (TypeError, ValueError):
        return 0


# ── Button / success classification ──────────────────────────────────

SUBMIT_LABELS = ("submit application", "submit your application", "send application",
                 "submit", "send", "apply now", "apply")
# Words that look like a submit but are not — pressing these loses the form or
# starts an unrelated flow.
NOT_SUBMIT_LABELS = ("apply with", "autofill", "upload", "attach", "dropbox",
                     "google drive", "enter manually", "add", "back", "cancel",
                     "save", "sign in", "log in", "linkedin", "indeed", "refer",
                     "share", "cookie", "accept", "reject", "manage", "settings",
                     "search", "next slide", "previous")

SUCCESS_PATTERNS = re.compile(
    r"thank you for applying|thanks for applying|thank you for your (interest|application)|"
    r"your application (has been |was )?(submitted|received|sent)|"
    r"application (submitted|received|complete)|we('| ha)?ve received your application|"
    r"we have received your application|successfully submitted|"
    r"application was submitted successfully"
)
SUCCESS_URL_PATTERNS = ("confirmation", "thank-you", "thankyou", "/success",
                        "application_confirmation", "/applied", "/submitted")


def classify_button(label: str) -> str | None:
    """'submit' or None, from a button's visible label. Conservative by design."""
    text = normalise(label)
    if not text:
        return None
    if any(bad in text for bad in NOT_SUBMIT_LABELS):
        # "Submit application" must still win over the generic "apply" veto.
        if not any(text.startswith(good) for good in ("submit", "send application")):
            return None
    for want in SUBMIT_LABELS:
        if want in text:
            return "submit"
    return None


def looks_successful(url: str = "", text: str = "") -> bool:
    """Did the page turn into a confirmation? Used only in live (non-dry) mode."""
    low_url = (url or "").lower()
    if any(m in low_url for m in SUCCESS_URL_PATTERNS):
        return True
    return bool(SUCCESS_PATTERNS.search((text or "").lower()[:4000]))


# ── Blocker classification ───────────────────────────────────────────

# NOTE — deliberate departure from `linkedin/session.py`. That module treats the
# mere *presence* of a reCAPTCHA/hCaptcha script as a blocker. Every Greenhouse,
# Ashby and Lever form embeds an invisible reCAPTCHA/hCaptcha that scores the
# session silently and never shows a challenge, so that rule would abort 100% of
# portal applications. Here we abort on an *interactive* challenge instead: a
# visible challenge iframe, or a Cloudflare/bot-wall interstitial. We still never
# attempt to solve or evade either.
BLOCKER_URL_PATTERNS = [
    (r"/cdn-cgi/challenge", "Cloudflare challenge page"),
    (r"__cf_chl", "Cloudflare challenge flow"),
    (r"/captcha", "CAPTCHA page"),
    (r"/blocked", "block page"),
    (r"perimeterx|px-captcha", "PerimeterX bot wall"),
    (r"/distil_", "Distil bot wall"),
]

BLOCKER_TEXT_PATTERNS = [
    (r"checking your browser before accessing", "Cloudflare interstitial"),
    (r"attention required!?\s*\|?\s*cloudflare", "Cloudflare block page"),
    (r"enable javascript and cookies to continue", "Cloudflare JS wall"),
    (r"verify (that )?you (are|'re) (a )?human", "human-verification challenge"),
    (r"prove you('|’)?re not a robot", "robot check"),
    (r"unusual traffic from your computer", "unusual-traffic block"),
    (r"our systems have detected unusual", "unusual-activity detection"),
    (r"access denied", "access denied page"),
    (r"you have been blocked", "IP block page"),
    (r"rate limit exceeded|too many requests", "rate limited"),
    (r"request blocked", "request blocked"),
]

BLOCKER_STATUSES = {403: "HTTP 403 (blocked)", 429: "HTTP 429 rate limited",
                    503: "HTTP 503 (bot wall / unavailable)"}

# Only the *challenge* surfaces, never the passive badge/anchor frames.
CHALLENGE_IFRAME_RE = re.compile(
    r"recaptcha/(api2|enterprise)/bframe|hcaptcha\.com/captcha/v1/[^\"']*"
    r"(challenge|hcaptcha-challenge)|challenges\.cloudflare\.com/turnstile|"
    r"funcaptcha|arkoselabs\.com|geo\.captcha-delivery\.com", re.I)

TEXT_SCAN_CHARS = 3000


def classify_blocker(url: str = "", title: str = "", text: str = "",
                     status: int | None = None,
                     visible_challenge: str | None = None) -> str | None:
    """Pure blocker classifier. Returns a description, or None if the page is fine.

    `visible_challenge` is the src of an on-screen, non-trivially-sized challenge
    iframe, supplied by `detect_blocker`. Passive/invisible widgets are ignored
    on purpose — see the NOTE above.
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

    if visible_challenge and CHALLENGE_IFRAME_RE.search(visible_challenge):
        return f"interactive CAPTCHA challenge ({visible_challenge[:90]})"
    return None


# ── Field descriptors → specs ────────────────────────────────────────

SKIP_TYPES = {"hidden", "submit", "button", "image", "reset"}
KIND_BY_TYPE = {"textarea": "textarea", "select": "select", "checkbox": "checkbox",
                "file": "file", "number": "number", "tel": "text", "email": "text",
                "url": "text", "date": "text", "search": "text"}

# Cookie-consent / marketing widgets that live inside the same DOM. Filling or
# reading these is never useful and their checkboxes look like consent questions.
NOISE_ID_RE = re.compile(
    r"cookiebot|onetrust|cookiescript|cookie-consent|osano|truste|usercentrics|"
    r"g-recaptcha|h-captcha|hcaptcha|recaptcha|__next|gtm-", re.I)


def is_noise(descriptor: dict) -> bool:
    """True for consent-banner / captcha plumbing controls."""
    blob = " ".join(str(descriptor.get(k) or "") for k in ("id", "name", "cls", "label"))
    return bool(NOISE_ID_RE.search(blob))


REQUIRED_MARK_RE = re.compile(r"[*✱✻]|\(required\)|\brequired\b", re.I)


def descriptor_required(descriptor: dict) -> bool:
    """Is this control mandatory?

    Belt and braces: the scraper already reads `required` / `aria-required`, but
    several ATS templates mark a field mandatory only by putting an asterisk in
    its label. Re-deriving it here (rather than in JS alone) keeps the rule
    testable and means a scraper regression cannot silently downgrade a required
    question into an optional one we skip.
    """
    if descriptor.get("required"):
        return True
    for key in ("label", "groupLabel"):
        text = descriptor.get(key) or ""
        if text and REQUIRED_MARK_RE.search(text):
            return True
    return False


@dataclass(frozen=True)
class Handle:
    """How to re-find one control after the page re-renders.

    Marks alone are not enough: filling one field can make a framework re-render
    a *later* widget (Greenhouse swaps its phone component when Country changes),
    which destroys the marked node and leaves the locator hanging until timeout.
    `name`/`id` come from the DOM and survive those re-renders, so they serve as
    fallbacks. See `locate()`.
    """
    marks: tuple[str, ...]      # data-jobpilot-field values, one per element
    combobox: bool = False
    list_id: str = ""           # aria-controls listbox, for comboboxes
    name: str = ""              # element name attribute — survives re-render
    element_id: str = ""        # element id attribute — survives re-render


def build_field_specs(descriptors: list[dict]) -> list[tuple[Handle, FieldSpec]]:
    """Turn raw DOM descriptors into (handle, FieldSpec) pairs.

    Radios *and* same-named checkbox groups collapse into a single question whose
    options are the individual labels — otherwise "Yes" and "No" would look like
    two unrelated questions and we would answer neither correctly.
    """
    specs: list[tuple[Handle, FieldSpec]] = []
    groups: dict[str, list[dict]] = {}
    order: list[str] = []

    for d in descriptors:
        dtype = (d.get("type") or "").lower()
        if dtype in SKIP_TYPES or is_noise(d):
            continue
        if not d.get("visible", True) and dtype != "file":
            continue

        if dtype == "radio":
            key = d.get("name") or d.get("groupLabel") or f"__radio{d['mark']}"
            if key not in groups:
                order.append(key)
            groups.setdefault(key, []).append(d)
            continue

        kind = KIND_BY_TYPE.get(dtype, "text")
        # An *unchecked* checkbox still carries a `value` attribute ("He/him" on
        # Lever's pronoun list). Treating that as a pre-filled answer would make
        # the answerer echo it straight back and tick the box — inventing a
        # demographic answer nobody gave. Only a checked box has a value.
        raw_value = d.get("value") or ""
        if dtype == "checkbox" and not d.get("checked"):
            raw_value = ""
        # For a checkbox the wrapping label is the *option* ("No"); the question
        # lives on the container. Carry both so the manual queue is readable.
        label = d.get("label") or d.get("groupLabel") or d.get("name") or ""
        group = (d.get("groupLabel") or "").strip()
        if dtype == "checkbox" and group and group != label:
            label = f"{group} — {label}" if label else group
        specs.append((
            Handle(marks=(d["mark"],), combobox=bool(d.get("combobox")),
                   list_id=d.get("listId") or "",
                   name=d.get("name") or "", element_id=d.get("id") or ""),
            FieldSpec(
                label=label,
                kind="select" if d.get("combobox") else kind,
                options=tuple(d.get("options") or ()),
                value=raw_value,
                required=descriptor_required(d),
                name=d.get("name") or "",
                element_id=d.get("id") or "",
            )))

    for key in order:
        members = groups[key]
        first = members[0]
        options = tuple((m.get("label") or m.get("value") or "").strip() for m in members)
        checked = next((m for m in members if m.get("checked")), None)
        specs.append((
            Handle(marks=tuple(m["mark"] for m in members),
                   name=first.get("name") or "", element_id=first.get("id") or ""),
            FieldSpec(
                label=first.get("groupLabel") or first.get("name") or "",
                kind="radio",
                options=options,
                value=(checked.get("label") or checked.get("value") or "") if checked else "",
                required=any(descriptor_required(m) for m in members),
                name=first.get("name") or "",
                element_id=first.get("id") or "",
            )))
    return specs


@dataclass
class FillResult:
    """Everything one pass over a form produced — the audit record."""
    provider: str = "generic"
    url: str = ""
    fields: list[dict] = field(default_factory=list)      # question/answer/source
    files: list[dict] = field(default_factory=list)       # {field, path, attached}
    unanswered: list[dict] = field(default_factory=list)  # {question, reason, required}
    submit_found: bool = False
    submit_label: str = ""
    blocked: str | None = None       # a REQUIRED question we cannot answer honestly
    error: str | None = None         # mechanical failure (page/selector/timeout)

    @property
    def resume_attached(self) -> bool:
        return any(f.get("attached") for f in self.files)

    @property
    def required_unanswered(self) -> list[dict]:
        return [u for u in self.unanswered if u.get("required")]

    @property
    def ready_to_submit(self) -> bool:
        """A submit may only be attempted when nothing is missing or invented."""
        return (self.blocked is None and self.error is None
                and self.submit_found and not self.required_unanswered)

    def payload(self) -> dict:
        """The complete record of what would be / was submitted."""
        return {
            "provider": self.provider,
            "url": self.url,
            "fields": self.fields,
            "files": self.files,
            "unanswered": self.unanswered,
            "submit": {"found": self.submit_found, "label": self.submit_label},
        }


# ══ Browser-facing engine ════════════════════════════════════════════

MARK_ATTR = "data-jobpilot-field"


def locate(scope, handle, mark: str | None = None):
    """Resolve a handle to a live locator, tolerating re-renders.

    Tries the mark first (exact, set during the scan). If that node is gone —
    a framework re-rendered the widget after we filled an earlier field — falls
    back to the element's `name`/`id`, which survive re-renders. Returns None
    when the control genuinely is not on the page, so callers can refuse rather
    than block until timeout.
    """
    mark = mark or (handle.marks[0] if handle.marks else "")
    candidates = []
    if mark:
        candidates.append(f"[{MARK_ATTR}='{mark}']")
    if getattr(handle, "name", ""):
        candidates.append(f"[name={json.dumps(handle.name)}]")
    if getattr(handle, "element_id", ""):
        candidates.append(f"#{css_escape(handle.element_id)}")
    for sel in candidates:
        try:
            loc = scope.locator(sel).first
            if loc.count():
                return loc
        except Exception:  # noqa: BLE001 — bad selector must not abort the fill
            continue
    return None


def css_escape(value: str) -> str:
    """Escape an id for use in a CSS selector."""
    return re.sub(r"([^\w-])", r"\\\1", value)

# One JS pass beats dozens of locator round-trips, and it is the only way to get
# the label association (`for=`, `aria-labelledby`, wrapping `<label>`, enclosing
# field container) that Playwright alone would make painful. Every element is
# stamped with a unique `data-jobpilot-field` so Python can address it again
# without relying on DOM order.
FIELD_SCRAPER = """
(rootSel) => {
  const root = rootSel ? (document.querySelector(rootSel) || document.body) : document.body;
  const txt = (n) => n ? (n.innerText || n.textContent || '').replace(/\\s+/g,' ').trim() : '';
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  const CONTAINERS = [
    '[class*="field"]', '[class*="Field"]', '[class*="question"]',
    '[class*="form-group"]', '[class*="formGroup"]', '[class*="input-wrapper"]',
    'fieldset', 'li', '[role="group"]', '[role="radiogroup"]'
  ].join(',');

  const labelFor = (el) => {
    let l = (el.getAttribute('aria-label') || '').trim();
    if (l) return l;
    if (el.id) {
      const byFor = document.querySelector('label[for="' + esc(el.id) + '"]');
      if (byFor) { l = txt(byFor); if (l) return l; }
      const gh = document.getElementById(el.id + '-label');   // greenhouse react-select
      if (gh) { l = txt(gh); if (l) return l; }
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      l = lb.split(/\\s+/).map(id => txt(document.getElementById(id))).join(' ').trim();
      if (l) return l;
    }
    const wrap = el.closest('label');
    if (wrap) {
      const clone = wrap.cloneNode(true);
      clone.querySelectorAll('input,select,textarea,button').forEach(n => n.remove());
      l = txt(clone);
      if (l) return l;
    }
    const box = el.closest(CONTAINERS);
    if (box) {
      const lab = box.querySelector('label,legend,[class*="label"]');
      if (lab && !lab.contains(el)) { l = txt(lab); if (l) return l; }
    }
    let prev = el.previousElementSibling;
    for (let i = 0; i < 3 && prev; i++, prev = prev.previousElementSibling) {
      if (/^(label|legend|h[1-6]|p|span|div)$/i.test(prev.tagName)) {
        l = txt(prev); if (l && l.length < 200) return l;
      }
    }
    return (el.placeholder || el.name || '').trim();
  };

  // The heading of the group a radio/checkbox belongs to ("Do you require
  // sponsorship?"), as opposed to the option's own label ("Yes").
  const groupLabelFor = (el) => {
    // Climb outwards until a container yields a heading. One level is not
    // enough: Lever nests each option in its own <li> inside a <ul> inside a
    // <div class="application-field">, and the question text lives a further
    // level up in a sibling <div class="application-label">.
    const boxes = [];
    const strict = el.closest('fieldset,[role="radiogroup"],[role="group"]');
    if (strict) boxes.push(strict);
    let box = el.closest(CONTAINERS);
    for (let i = 0; i < 5 && box; i++) {
      boxes.push(box);
      box = box.parentElement ? box.parentElement.closest(CONTAINERS) : null;
    }
    const SEL = 'legend,label,[class*="label"],[class*="title"],[class*="question"],' +
                'h1,h2,h3,h4,h5,h6,p';
    for (const b of boxes) {
      // Scan *all* candidates, not just the first: a checkbox is usually
      // wrapped in its own <label> ("No"), which would otherwise be mistaken
      // for the question and land a useless string in the manual queue.
      for (const cand of b.querySelectorAll(SEL)) {
        if (cand.contains(el)) continue;
        if (cand.querySelector('input,select,textarea')) continue;
        const t = txt(cand);
        if (t && t.length > 2 && t.length < 400) return t;
      }
    }
    return '';
  };

  const els = Array.from(root.querySelectorAll('input, select, textarea'));
  let n = 0;
  return els.map((el) => {
    const mark = 'f' + (n++);
    el.setAttribute('%(attr)s', mark);
    const tag = el.tagName.toLowerCase();
    const type = (tag === 'input' ? (el.type || 'text') : tag).toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    const combobox = role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox';
    const label = labelFor(el);
    const groupLabel = (type === 'radio' || type === 'checkbox') ? groupLabelFor(el) : '';
    const options = tag === 'select'
      ? Array.from(el.options).map(o => (o.text || '').trim()).filter(Boolean) : [];
    const required = !!(el.required || el.getAttribute('aria-required') === 'true'
                        || /[*✱✻]/.test(label) || /[*✱✻]/.test(groupLabel)
                        || /required/i.test(el.className || ''));
    const visible = !!(el.offsetParent !== null || el.getClientRects().length);
    return {mark, tag, type, combobox, role,
            listId: el.getAttribute('aria-controls') || '',
            label, groupLabel,
            value: el.value == null ? '' : String(el.value),
            checked: !!el.checked, options, required, visible,
            name: el.name || '', id: el.id || '',
            cls: (el.className || '').toString().slice(0, 120)};
  });
}
""" % {"attr": MARK_ATTR}

# Options of an already-opened combobox listbox.
COMBO_OPTIONS = """
(args) => {
  const el = document.querySelector('[%(attr)s="' + args.mark + '"]');
  if (!el) return [];
  const id = el.getAttribute('aria-controls') || args.listId;
  let box = id ? document.getElementById(id) : null;
  if (!box) {
    const boxes = Array.from(document.querySelectorAll('[role="listbox"],[class*="select__menu"]'))
      .filter(b => b.getClientRects().length);
    box = boxes[boxes.length - 1] || null;
  }
  if (!box) return [];
  return Array.from(box.querySelectorAll('[role="option"],[class*="select__option"],li'))
    .map(o => (o.innerText || '').replace(/\\s+/g,' ').trim()).filter(Boolean);
}
""" % {"attr": MARK_ATTR}

# A challenge iframe that is actually on screen and big enough to interact with.
VISIBLE_CHALLENGE = """
() => {
  const frames = Array.from(document.querySelectorAll('iframe'));
  for (const f of frames) {
    const r = f.getBoundingClientRect();
    const style = window.getComputedStyle(f);
    if (r.width < 80 || r.height < 80) continue;
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    if (/bframe|hcaptcha-challenge|turnstile|funcaptcha|arkoselabs|captcha-delivery/i.test(f.src || '')) {
      return f.src;
    }
  }
  return null;
}
"""

COOKIE_BUTTONS = (
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#cookiescript_accept",
    "button#hs-eu-confirmation-button",
    "[data-testid='uc-accept-all-button']",
    "button[aria-label*='Accept']",
)
COOKIE_TEXTS = ("Accept all cookies", "Accept All Cookies", "Allow all",
                "Accept all", "Accept", "I accept", "Got it", "OK")


def detect_blocker(page, status: int | None = None) -> str | None:
    """Inspect a live page for a CAPTCHA challenge / bot wall. Call after every nav."""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001 — a dead page is noise, not a blocker
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
        challenge = page.evaluate(VISIBLE_CHALLENGE)
    except Exception:  # noqa: BLE001
        challenge = None
    return classify_blocker(url=url, title=title, text=text, status=status,
                            visible_challenge=challenge)


def guard(page, status: int | None = None, log=print) -> None:
    """detect_blocker + abort. Raises BlockerDetected so the run unwinds cleanly."""
    blocker = detect_blocker(page, status=status)
    if blocker:
        log(f"\n  ✖ ABORTING — bot check detected: {blocker}")
        log("    Not attempting to solve or evade it. Open the page in your normal")
        log("    browser, apply by hand if you want this role, then re-run later.")
        raise BlockerDetected(blocker)


def dismiss_cookie_banner(scope_page, log=print) -> bool:
    """Clear a consent overlay so it cannot swallow clicks on the form.

    Consent banners are page furniture, not employer questions; the alternative
    is every click landing on an invisible backdrop.
    """
    for sel in COOKIE_BUTTONS:
        try:
            loc = scope_page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=4_000)
                return True
        except Exception:  # noqa: BLE001
            continue
    for name in COOKIE_TEXTS:
        try:
            loc = scope_page.get_by_role("button", name=name, exact=True).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3_000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ── Adapters ─────────────────────────────────────────────────────────

class Adapter:
    """Common interface. Subclasses override selectors and the two open hooks.

    `scope(page)` returns the Playwright *frame* the form lives in — `page.main_frame`
    for most providers, the embedded Greenhouse iframe for company-hosted boards.
    All scraping and filling goes through that frame, so an iframe costs nothing.
    """

    name = "generic"
    # CSS that identifies this provider's form in the DOM. Order matters: the
    # first that matches wins.
    dom_markers: tuple[str, ...] = ()
    # Scope the scrape to the form when we can, so page chrome is not scraped.
    form_root: str | None = None
    resume_selectors: tuple[str, ...] = (
        "input[type=file][name*='resume' i]", "input[type=file][id*='resume' i]",
        "input[type=file][id*='cv' i]", "input[type=file][name*='cv' i]",
        "input[type=file]",
    )
    submit_selectors: tuple[str, ...] = (
        "button[type=submit]", "input[type=submit]", "#btn-submit", "button",
    )
    # Text a successful submission puts on the page, beyond the generic list.
    success_markers: tuple[str, ...] = ()

    # -- discovery ----------------------------------------------------
    def application_url(self, url: str, company: str | None = None) -> str:
        return application_url(url, source=self.name, company=company)

    def detect(self, page) -> bool:
        """True when this adapter recognises the page it is looking at."""
        if provider_from_url(getattr(page, "url", "") or "") == self.name:
            return True
        scope = self.scope(page)
        for sel in self.dom_markers:
            try:
                if scope.locator(sel).count():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def scope(self, page):
        """The frame the application form lives in."""
        return page.main_frame

    def open_form(self, page, settings: dict | None, log=print):
        """Get from the posting page to the form. Returns the form frame.

        The default does nothing — most providers' application URL already *is*
        the form. Overridden where a click is required.
        """
        return self.scope(page)

    # -- control mechanics (overridable per provider) -----------------
    def set_select(self, scope, handle: Handle, value: str) -> bool:
        """Choose `value` in a native <select> or a custom combobox."""
        target = locate(scope, handle)
        if target is None:
            return False
        if handle.combobox:
            return self._set_combobox(scope, handle, value)
        try:
            target.select_option(label=value, timeout=8_000)
            return True
        except Exception:  # noqa: BLE001 — some selects only match by value
            try:
                target.select_option(value, timeout=8_000)
                return True
            except Exception:  # noqa: BLE001
                return False

    def _set_combobox(self, scope, handle: Handle, value: str) -> bool:
        """Open a listbox-style combobox and click the matching option."""
        target = locate(scope, handle)
        if target is None:
            return False
        try:
            target.scroll_into_view_if_needed(timeout=5_000)
            target.click(timeout=8_000)
        except Exception:  # noqa: BLE001
            return False
        try:
            scope.page.wait_for_timeout(500)
            option = scope.get_by_role("option", name=value, exact=True).first
            if not option.count():
                option = scope.get_by_role("option", name=value).first
            if not option.count():
                # Typeahead: nothing is rendered until a query narrows the list,
                # so the option we mean to pick does not exist in the DOM yet.
                # Type it (real key events — react-select ignores a bare value
                # assignment), then look again.
                try:
                    target.fill("", timeout=4_000)
                    target.press_sequentially(value[:40], delay=25, timeout=10_000)
                    scope.page.wait_for_timeout(1_200)
                except Exception:  # noqa: BLE001
                    pass
                option = scope.get_by_role("option", name=value, exact=True).first
                if not option.count():
                    option = scope.get_by_role("option", name=value).first
            if not option.count():
                target.press("Escape")
                return False
            option.click(timeout=8_000)
            return True
        except Exception:  # noqa: BLE001
            try:
                target.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            return False

    def read_combobox_options(self, scope, handle: Handle,
                              query: str = "") -> list[str]:
        """Custom comboboxes hide their options until opened — open and read them."""
        target = locate(scope, handle)
        if target is None:
            return []
        try:
            target.scroll_into_view_if_needed(timeout=5_000)
            target.click(timeout=8_000)
            scope.page.wait_for_timeout(500)
            opts = scope.evaluate(COMBO_OPTIONS,
                                  {"mark": handle.marks[0], "listId": handle.list_id})
            opts = [o for o in (opts or []) if o]
            # Typeahead fields (Greenhouse "School", "Degree") show the full
            # unfiltered list until a query narrows it, so an unfiltered read
            # tells us nothing. Seed with the value we intend to submit.
            # The keystrokes must be genuine: react-select filters on key events,
            # and `fill()` assigns the value without firing any — which silently
            # returns the alphabetical head of the list and reads as "no match".
            if query:
                target.fill("", timeout=5_000)
                target.press_sequentially(query[:40], delay=25, timeout=10_000)
                scope.page.wait_for_timeout(1_200)
                typed = [o for o in (scope.evaluate(
                    COMBO_OPTIONS,
                    {"mark": handle.marks[0], "listId": handle.list_id}) or []) if o]
                target.fill("", timeout=5_000)
                if typed:
                    opts = typed
            target.press("Escape")
            return opts
        except Exception:  # noqa: BLE001
            try:
                target.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            return []

    def extra_questions(self, scope) -> list[dict]:
        """Provider-specific questions the generic scraper cannot see.

        Returns `{question, reason, required}` dicts. Used by Ashby, whose
        yes/no questions render as plain <button>s with no form control.
        """
        return []

    def after_fill_field(self, scope, spec: FieldSpec, value: str, log=print) -> None:
        """Hook run right after a value is written — for typeahead widgets that
        need the suggestion list confirmed. No-op for most providers."""
        return None


class GreenhouseAdapter(Adapter):
    name = "greenhouse"
    dom_markers = ("form#application-form", "#application_form", "#grnhse_app",
                   "input#first_name", "div#application")
    form_root = "form#application-form, #application_form, form"
    resume_selectors = ("input#resume", "input[type=file][id*='resume' i]",
                        "input[type=file][name='resume']", "input[type=file]")
    # Greenhouse renders every dropdown as a react-select combobox whose options
    # only exist in the DOM once it is opened — the single most fragile part of
    # this adapter. Options live in `#react-select-<id>-listbox`.
    EMBED_FRAME_RE = re.compile(r"job-boards\.(eu\.)?greenhouse\.io/embed/job_app|"
                                r"boards\.greenhouse\.io/embed/job_app", re.I)

    def scope(self, page):
        """The board page itself, or the iframe a company-hosted board embeds."""
        try:
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                if self.EMBED_FRAME_RE.search(frame.url or ""):
                    return frame
        except Exception:  # noqa: BLE001
            pass
        return page.main_frame

    def open_form(self, page, settings: dict | None, log=print):
        scope = self.scope(page)
        # Some boards keep the form behind an "Apply" toggle at the top of the page.
        try:
            if not scope.locator("input#first_name, input[id*='first_name']").count():
                btn = scope.get_by_role("button", name=re.compile(r"^apply", re.I)).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=8_000)
                    page.wait_for_timeout(2_000)
        except Exception:  # noqa: BLE001
            pass
        return self.scope(page)


class LeverAdapter(Adapter):
    name = "lever"
    dom_markers = ("form#application-form", "input#resume-upload-input",
                   "input[name='urls[LinkedIn]']", ".application-form")
    form_root = "form#application-form, form"
    resume_selectors = ("input#resume-upload-input", "input[type=file][name='resume']",
                        "input[type=file]")
    submit_selectors = ("#btn-submit", "button[type=submit]", "input[type=submit]")

    def after_fill_field(self, scope, spec: FieldSpec, value: str, log=print) -> None:
        """Lever's `location` box is a geocoder typeahead: free text alone leaves
        the hidden `selectedLocation` empty and the form rejects it. Accept the
        first suggestion — but only if it actually contains what we typed, so we
        never silently apply from a city the profile never mentioned."""
        if "location" not in normalise(spec.label + " " + spec.name):
            return
        try:
            loc = scope.locator("#location-input")
            if not loc.count():
                return
            scope.page.wait_for_timeout(1_200)
            first = scope.locator(
                ".dropdown-location-results li, [class*='dropdown'] li, "
                "[role='option']").first
            if not first.count():
                return
            text = (first.inner_text() or "").strip()
            token = (value or "").split(",")[0].strip().lower()
            if token and token in text.lower():
                first.click(timeout=5_000)
                log(f"    · accepted location suggestion {text[:50]!r}")
            else:
                log(f"    · left location as typed (suggestion {text[:40]!r} "
                    f"does not match {token!r})")
        except Exception:  # noqa: BLE001 — a typeahead miss is not fatal
            pass


class AshbyAdapter(Adapter):
    name = "ashby"
    dom_markers = ("input#_systemfield_name", "input#_systemfield_email",
                   "input#_systemfield_resume", "[class*='ashby']")
    # Ashby renders no <form> element at all — the whole page is the form.
    form_root = None
    # The *first* file input on an Ashby page is the "autofill from résumé"
    # widget, which parses the file and rewrites fields unpredictably. Always
    # target the real résumé field by id.
    resume_selectors = ("input#_systemfield_resume",
                        "input[type=file][id*='resume' i]")

    def open_form(self, page, settings: dict | None, log=print):
        scope = self.scope(page)
        try:
            if not scope.locator("input#_systemfield_email").count():
                btn = scope.get_by_role(
                    "button", name=re.compile(r"apply for this job", re.I)).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=8_000)
                    page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(2_500)
        except Exception:  # noqa: BLE001
            pass
        return self.scope(page)

    def extra_questions(self, scope) -> list[dict]:
        """Ashby yes/no questions are <button>Yes</button><button>No</button> pairs
        backed by a hidden checkbox — invisible to the field scraper. They are
        always screening questions, so surface them as unanswerable rather than
        letting them pass silently."""
        try:
            found = scope.evaluate("""
            () => {
              const txt = n => n ? (n.innerText||'').replace(/\\s+/g,' ').trim() : '';
              const out = [];
              const btns = Array.from(document.querySelectorAll('button'));
              for (let i = 0; i < btns.length - 1; i++) {
                if (txt(btns[i]).toLowerCase() !== 'yes') continue;
                if (txt(btns[i+1]).toLowerCase() !== 'no') continue;
                let box = btns[i].closest('div');
                for (let up = 0; up < 4 && box; up++) {
                  const lab = box.querySelector('label,legend,p');
                  const t = txt(lab);
                  if (t && t.length > 3) { out.push(t); break; }
                  box = box.parentElement;
                }
              }
              return out;
            }""") or []
        except Exception:  # noqa: BLE001
            return []
        # Treated as required even when no asterisk is visible: these are always
        # screening questions (sponsorship, relocation, age) and the DOM gives us
        # no reliable required flag for them, so we assume the stricter case and
        # send the job to the manual queue rather than submit it half-answered.
        return [{"question": q,
                 "reason": "yes/no screening question — must be answered by you",
                 "required": True}
                for q in found]


class WorkableAdapter(Adapter):
    name = "workable"
    dom_markers = ("input#firstname", "input#lastname",
                   "form[action*='workable.com']", "[data-ui='application-form']")
    form_root = "form"
    resume_selectors = ("input[type=file][id^='input_files_input']",
                        "input[type=file][name*='resume' i]", "input[type=file]")


class SmartRecruitersAdapter(Adapter):
    name = "smartrecruiters"
    dom_markers = ("#firstName", "#lastName", "form[name='applicationForm']",
                   "[class*='oneclick']", "[data-test='application-form']")
    form_root = "form"
    resume_selectors = ("input[type=file][name*='resume' i]",
                        "input[type=file][id*='resume' i]", "input[type=file]")

    def application_url(self, url: str, company: str | None = None) -> str:
        return url  # the posting page carries the link; open_form clicks it

    def open_form(self, page, settings: dict | None, log=print):
        """SmartRecruiters puts the form behind an "I'm interested" link that
        points at `/oneclick-ui/company/<co>/publication/<uuid>`.

        Navigating straight to the href is more reliable than clicking, because
        the posting page renders a cookie overlay over the button. Note the
        oneclick UI renders nothing at all under headless Chromium in testing —
        see NOTES.md; this provider is unverified end to end.
        """
        scope = self.scope(page)
        try:
            if scope.locator("#firstName, input[name='firstName']").count():
                return scope
            href = page.evaluate(
                "() => { const a = Array.from(document.querySelectorAll('a'))"
                ".find(a => /oneclick-ui/.test(a.href)); return a ? a.href : null; }")
            if href:
                page.goto(href, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(6_000)
                return self.scope(page)
            link = page.get_by_role(
                "link", name=re.compile(r"i'?m interested|apply", re.I)).first
            if link.count():
                link.click(timeout=10_000)
                page.wait_for_load_state("domcontentloaded", timeout=45_000)
                page.wait_for_timeout(5_000)
        except Exception as e:  # noqa: BLE001
            log(f"    (could not open the SmartRecruiters apply UI: {type(e).__name__}: {e})")
        return self.scope(page)


class RecruiteeAdapter(Adapter):
    name = "recruitee"
    dom_markers = ("form#offer-application-form", "input[name='candidate.email']",
                   "input[name='candidate.name']")
    form_root = "form#offer-application-form, form"
    resume_selectors = ("input[name='candidate.cv']", "input[type=file][id*='cv' i]",
                        "input[type=file]")


ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(),
                        WorkableAdapter(), SmartRecruitersAdapter(),
                        RecruiteeAdapter())
}
GENERIC = Adapter()


def adapter_for(source: str | None = None, url: str | None = None) -> Adapter:
    """Pick an adapter from the job's `source` column, else from its URL."""
    key = (source or "").lower()
    if key in ADAPTERS:
        return ADAPTERS[key]
    guessed = provider_from_url(url or "")
    return ADAPTERS.get(guessed or "", GENERIC)


def detect_adapter(page) -> Adapter:
    """Ask each adapter whether it recognises the live page. Falls back to generic."""
    url = ""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        pass
    guessed = provider_from_url(url)
    ordered = ([ADAPTERS[guessed]] if guessed in ADAPTERS else []) + [
        a for name, a in ADAPTERS.items() if name != guessed]
    for adapter in ordered:
        try:
            if adapter.detect(page):
                return adapter
        except Exception:  # noqa: BLE001
            continue
    return GENERIC


# ── The fill engine ──────────────────────────────────────────────────

CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#onetrust-reject-all-handler",
    ".ot-pc-refuse-all-handler",
    "button#truste-consent-button",
    "[aria-label='Accept cookies']",
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept All')",
    "button:has-text('Reject all')",
)


def dismiss_consent(scope, log=print) -> bool:
    """Close a cookie/consent banner before scanning the form.

    These banners are not cosmetic: OneTrust injects its own inputs (which the
    scraper otherwise mistakes for form fields) and lays a full-page overlay over
    everything, so real inputs fail actionability checks and time out. Dismissing
    first makes the scan see the actual application form.
    """
    for sel in CONSENT_SELECTORS:
        try:
            btn = scope.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=4_000)
                scope.wait_for_timeout(600)
                log("    · dismissed cookie banner")
                return True
        except Exception:  # noqa: BLE001 — never let a banner abort the run
            continue
    return False


def scrape_fields(scope, adapter: Adapter) -> list[dict]:
    try:
        dismiss_consent(scope)
    except Exception:  # noqa: BLE001
        pass
    try:
        fields = scope.evaluate(FIELD_SCRAPER, adapter.form_root) or []
    except Exception:  # noqa: BLE001
        return []
    # Drop consent-widget inputs that survived; they are never part of a job
    # application and answering them would be meaningless noise.
    fields = [f for f in fields
              if not re.search(r"ot-group-id|onetrust|vendor-search|truste|cookie",
                               f"{f.get('name','')} {f.get('id','')} {f.get('cls','')}", re.I)]
    return drop_decoy_inputs(fields)


def drop_decoy_inputs(fields: list[dict]) -> list[dict]:
    """Remove helper inputs that shadow the real control under the same label.

    Rich widgets render extra inputs beside the field they decorate —
    intl-tel-input puts a nameless search box next to the real `<input type=tel
    id=phone>`, and both report the label "Phone". Targeting the decoy means
    filling something the form will never submit, so the application looks
    complete while the real field stays empty.

    A control with neither `name` nor `id` is not submitted with the form, so
    when a labelled sibling *does* carry one, the anonymous input is chrome and
    is dropped. Anonymous inputs with no named sibling are kept — some
    frameworks really do wire them up through JS.
    """
    identified: set[str] = {
        (f.get("label") or "").strip().lower()
        for f in fields if f.get("name") or f.get("id")
    }
    kept = []
    for f in fields:
        label = (f.get("label") or "").strip().lower()
        anonymous = not f.get("name") and not f.get("id")
        if anonymous and label and label in identified:
            continue
        kept.append(f)
    return kept


def upload_resume(scope, adapter: Adapter, resume_path: str | None,
                  log=print) -> dict | None:
    """Attach the résumé and verify the browser actually took the file.

    Returns `{field, path, attached}` or None when there is no file input.
    """
    from pathlib import Path

    if not resume_path or not Path(resume_path).exists():
        return None
    for sel in adapter.resume_selectors:
        try:
            loc = scope.locator(sel).first
            if not loc.count():
                continue
            loc.set_input_files(resume_path, timeout=25_000)
            # Verify rather than assume: read back the input's FileList.
            attached = False
            name = ""
            try:
                info = loc.evaluate(
                    "(el) => el.files && el.files.length "
                    "? {n: el.files.length, name: el.files[0].name} : {n: 0, name: ''}")
                attached = bool(info and info.get("n"))
                name = (info or {}).get("name") or ""
            except Exception:  # noqa: BLE001
                attached = True   # some frameworks detach the input after reading it
            log(f"    ↑ résumé {'attached' if attached else 'NOT attached'}"
                f" via {sel!r}{f' ({name})' if name else ''}")
            return {"field": sel, "path": resume_path, "attached": attached}
        except Exception as e:  # noqa: BLE001
            log(f"    (résumé upload via {sel!r} failed: {type(e).__name__}: {e})")
            continue
    return {"field": None, "path": resume_path, "attached": False}


def write_answer(scope, adapter: Adapter, handle: Handle, spec: FieldSpec,
                 ans: Answer, log=print) -> bool:
    """Write one answer into the DOM. Returns True if something was set."""
    if ans.value is None:
        return False
    try:
        if spec.kind == "select":
            return adapter.set_select(scope, handle, ans.value)
        if spec.kind in ("radio", "checkbox") and len(handle.marks) >= 1:
            target = (ans.value or "").strip().lower()
            for pos, mark in enumerate(handle.marks):
                option = (spec.options[pos] if pos < len(spec.options) else "").strip().lower()
                if option and option == target:
                    loc = locate(scope, handle, mark)
                    if loc is None:
                        continue
                    loc.check(timeout=8_000, force=True)
                    return True
            return False
        loc = locate(scope, handle)
        if loc is None:
            return False
        try:
            loc.scroll_into_view_if_needed(timeout=5_000)
        except Exception:  # noqa: BLE001 — off-screen/late-hydrating is not fatal
            pass
        try:
            loc.fill(ans.value, timeout=10_000)
            return True
        except Exception:  # noqa: BLE001
            # Fields wrapped in a custom widget (phone pickers especially) can be
            # unreachable to Playwright's actionability checks while still being a
            # perfectly ordinary input underneath. Set the value directly and fire
            # the events frameworks listen for, then confirm it actually took —
            # never report success on a field we did not really populate.
            ok = loc.evaluate(
                """(el, v) => {
                    const set = Object.getOwnPropertyDescriptor(
                        el.constructor.prototype, 'value')?.set;
                    set ? set.call(el, v) : (el.value = v);
                    el.dispatchEvent(new Event('input',  {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return el.value === v;
                }""",
                ans.value,
            )
            if ok:
                log(f"    ✎ {spec.label[:40]!r} ← {ans.value!r} (direct set)")
                return True
            raise
    except Exception as e:  # noqa: BLE001
        log(f"    (could not set {spec.label[:60]!r}: {type(e).__name__}: {e})")
        return False


def find_submit(scope, adapter: Adapter):
    """(locator, label) of the submit control, or (None, '').

    Located but never pressed here — pressing is the caller's decision and in
    dry-run mode there is no code path that reaches it.
    """
    for sel in adapter.submit_selectors:
        try:
            buttons = scope.locator(sel)
            for i in range(min(buttons.count(), 40)):
                btn = buttons.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    label = (btn.inner_text() or "").strip() or \
                            (btn.get_attribute("value") or "").strip() or \
                            (btn.get_attribute("aria-label") or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if classify_button(label) == "submit":
                    return btn, label
        except Exception:  # noqa: BLE001
            continue
    return None, ""


def generic_fill(page, scope, adapter: Adapter, profile: dict,
                 resume_path: str | None, settings: dict | None,
                 log=print) -> FillResult:
    """Fill every field this adapter can honestly answer. Never submits.

    The contract: on return, either `result.ready_to_submit` is True and the form
    holds only profile-sourced values, or `blocked` / `required_unanswered`
    explain exactly which question stopped us.
    """
    from ..linkedin.session import human_delay

    cfg = portal_cfg(settings)
    pace = pacing(settings)
    answerer = PortalAnswerer(profile or {})
    result = FillResult(provider=adapter.name)
    try:
        result.url = page.url
    except Exception:  # noqa: BLE001
        pass

    uploaded = upload_resume(scope, adapter, resume_path, log=log)
    if uploaded:
        result.files.append(uploaded)
        human_delay(pace, scale=0.4)

    descriptors = scrape_fields(scope, adapter)
    if not descriptors:
        result.error = "no form fields found on the page"
        return result

    for handle, spec in build_field_specs(descriptors):
        if spec.kind == "file":
            # The résumé input is handled above; any *other* required file (a
            # cover letter, a portfolio deck) we cannot honestly produce.
            if spec.required and not (uploaded and uploaded.get("attached")
                                      and "resum" in normalise(spec.label + spec.name) + "cv"):
                if not re.search(r"resum|\bcv\b", normalise(spec.label + " " + spec.name)):
                    result.unanswered.append({
                        "question": spec.label or spec.name,
                        "reason": "requires a file we cannot generate",
                        "required": True})
            continue

        # Custom comboboxes keep their options hidden until opened. We must know
        # the options before we can decide whether an answer is honest.
        if handle.combobox and not spec.options:
            # Typeahead options only materialise once a query narrows them, so we
            # need a seed. Answering the option-less spec yields the raw profile
            # value (match_option passes values through when there is nothing to
            # match against) — that is exactly the right thing to type.
            probe = answerer.answer(spec)
            seed = probe.value if (probe.ok and probe.value) else ""
            opts = adapter.read_combobox_options(scope, handle, query=str(seed))
            if opts:
                spec = FieldSpec(label=spec.label, kind="select", options=tuple(opts),
                                 value=spec.value, required=spec.required,
                                 name=spec.name, element_id=spec.element_id)

            # A typeahead only reveals the entries matching what was typed, so a
            # seed that matches nothing returns the alphabetical head — and the
            # list's "Other" entry stays invisible. Probe for it directly before
            # concluding the question is unanswerable, but only after the seeded
            # read failed to produce a match, so a listed school always wins.
            if seed and match_option(str(seed), spec.options) is None:
                other_opts = adapter.read_combobox_options(scope, handle, query="Other")
                other = find_other_option(other_opts)
                if other:
                    spec = FieldSpec(
                        label=spec.label, kind="select",
                        options=tuple(spec.options) + (other,),
                        value=spec.value, required=spec.required,
                        name=spec.name, element_id=spec.element_id)

        ans = answerer.answer(spec)

        # A value the *site* put there that we neither replaced nor can vouch
        # for (geo-guessed address, locale phone prefix). We leave it alone —
        # clearing a field can break the form — but it must appear in the audit
        # record, because it will be submitted under the user's name.
        if (spec.value.strip() and ans.value is None
                and spec.kind in ("text", "textarea", "number", "select")):
            result.fields.append({
                "question": spec.label, "kind": spec.kind,
                "required": spec.required, "answer": spec.value.strip(),
                "source": "site-prefilled:left-as-is (NOT from your profile)"})

        if not ans.ok:
            # Record enough to actually answer it later: the key it will be
            # stored under, and the choices on offer. A dropdown you cannot see
            # the options for is unanswerable from a terminal.
            entry = {"question": spec.label or spec.name,
                     "key": question_key(spec.label, spec.element_id, spec.name),
                     "options": list(spec.options)[:40],
                     "kind": spec.kind,
                     "reason": ans.reason or "unanswerable",
                     "required": bool(spec.required)}
            result.unanswered.append(entry)
            if spec.required:
                result.blocked = entry["question"]
                log(f"    ✋ required question we cannot answer honestly: "
                    f"{entry['key'][:70]!r} — {entry['reason']}")
                if entry["options"]:
                    log(f"       options: {', '.join(entry['options'][:6])}"
                        f"{' …' if len(entry['options']) > 6 else ''}")
                break
            log(f"    · left blank (optional): {entry['question'][:70]!r}")
            continue

        # `answer_optional_questions: false` → touch nothing optional beyond
        # identity/contact details and the EEO decline.
        keep = (ans.source.startswith("profile:") or ans.source.startswith("eeo:")
                or ans.source == "prefilled")
        if not spec.required and not keep and not cfg["answer_optional_questions"]:
            continue

        if ans.value is None:
            result.fields.append({"question": spec.label, "kind": spec.kind,
                                  "required": spec.required, "answer": None,
                                  "source": ans.source})
            continue

        if write_answer(scope, adapter, handle, spec, ans, log=log):
            adapter.after_fill_field(scope, spec, ans.value, log=log)
            log(f"    ✎ {(spec.label or spec.name)[:60]!r} ← {ans.value!r} [{ans.source}]")
            result.fields.append({"question": spec.label, "kind": spec.kind,
                                  "required": spec.required, "answer": ans.value,
                                  "source": ans.source})
            human_delay(pace, scale=0.15)
        else:
            entry = {"question": spec.label or spec.name,
                     "reason": f"could not write the value {ans.value!r} into the control",
                     "required": bool(spec.required)}
            result.unanswered.append(entry)
            if spec.required:
                result.blocked = entry["question"]
                break

    # Provider-specific questions the scraper cannot see (Ashby's button yes/no).
    for extra in adapter.extra_questions(scope):
        result.unanswered.append(extra)
        if extra.get("required") and not result.blocked:
            result.blocked = extra["question"]

    submit, label = find_submit(scope, adapter)
    result.submit_found = submit is not None
    result.submit_label = label
    return result
