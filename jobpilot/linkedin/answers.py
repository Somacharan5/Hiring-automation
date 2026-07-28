"""Map Easy-Apply form fields onto the master profile — and refuse when it can't.

The governing rule: **an answer we cannot source from `config/profile.yaml` is a
lie to an employer.** So this module is a whitelist, not a best-effort guesser.
A field is answered only if an explicit rule matches AND the resulting value can
be reconciled with the field's own options. Everything else returns `ok=False`
and the caller abandons that application for manual attention.

Deliberately NOT answered, ever:
  · visa / sponsorship / work-authorisation questions
  · salary expectations, notice period, availability dates
  · "years of experience with <specific tool>" (the profile lists skills, not
    per-tool tenure — inventing a number here is the classic automated-apply lie)
  · any yes/no screening question
  · consent / agreement checkboxes (the user must give consent themselves)

Everything in this module is pure: no browser, no I/O. It is the most heavily
unit-tested part of the package.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldSpec:
    """One form control, normalised out of the DOM."""
    label: str
    kind: str                       # text | textarea | number | select | radio | checkbox | file
    options: tuple[str, ...] = ()
    value: str = ""
    required: bool = False
    name: str = ""
    element_id: str = ""


@dataclass(frozen=True)
class Answer:
    ok: bool
    value: str | None = None
    source: str = ""                # where the value came from, for the audit log
    reason: str | None = None       # why we refused, when ok=False


def unanswerable(field_label: str, why: str) -> Answer:
    return Answer(ok=False, value=None, source="", reason=f"{why}: {field_label!r}")


# ── Label normalisation ──────────────────────────────────────────────

def normalise(label: str | None) -> str:
    if not label:
        return ""
    text = label.replace(" ", " ").lower()
    text = re.sub(r"\(required\)|\brequired\b|\*", " ", text)
    text = re.sub(r"[^a-z0-9+#/\s'’-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ── Question classifiers ─────────────────────────────────────────────

# Anything matching these is refused outright, even if a naive rule would match.
REFUSE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bsponsor|sponsorship|visa|work permit|work authori[sz]|"
                r"right to work|legally authori[sz]ed|require.*authori[sz]ation\b"),
     "immigration/visa question — must be answered by you"),
    (re.compile(r"\bsalary|compensation|ctc|expected pay|desired pay|pay expectation|"
                r"remuneration|hourly rate|day rate\b"),
     "compensation question — must be answered by you"),
    (re.compile(r"\bnotice period|when can you (start|join)|availability|start date|"
                r"earliest.*start\b"),
     "availability question — must be answered by you"),
    (re.compile(r"\brelocat|willing to (move|travel|commute)|comfortable commuting|"
                r"onsite|on-site|hybrid.*willing\b"),
     "relocation/commute question — must be answered by you"),
    (re.compile(r"\bdisability|veteran|gender|ethnicit|race|sexual orientation|"
                r"pronoun|age range|date of birth\b"),
     "demographic/EEO question — must be answered by you"),
    (re.compile(r"\bnotice|background check|drug (test|screen)|criminal|"
                r"security clearance|clearance level\b"),
     "screening question — must be answered by you"),
    (re.compile(r"\bhow did you hear|referr(al|ed) by|who referred\b"),
     "referral question — must be answered by you"),
    (re.compile(r"\bcurrent (salary|ctc|employer.*notice)\b"),
     "current-employment question — must be answered by you"),
)

# Yes/no screening questions. We never pick a side.
YES_NO_OPTIONS = {"yes", "no", "y", "n"}

CITY_HINTS = re.compile(r"\b(city|current location|where are you (based|located)|"
                        r"location \(city|city/town|town)\b")

RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(first|given|fore)\s*name\b"), "first_name"),
    (re.compile(r"\b(last|family|sur)\s*name\b|\bsurname\b"), "last_name"),
    (re.compile(r"^(full |legal |your |candidate )?name$|\b(full|legal) name\b"), "full_name"),
    (re.compile(r"\be-?mail\b"), "email"),
    (re.compile(r"\b(country|dial(l)?ing|area)\s*code\b|\bphone country\b"), "phone_country_code"),
    (re.compile(r"\b(mobile|phone|telephone|cell)\b"), "phone"),
    (re.compile(r"\blinked ?in\b"), "linkedin"),
    (re.compile(r"\b(portfolio|personal (web)?site|website|web site|github|"
                r"personal url)\b"), "portfolio"),
    (CITY_HINTS, "location"),
    (re.compile(r"\b(headline|current title|job title|current role)\b"), "headline"),
    (re.compile(r"\b(school|university|college|institution|alma mater)\b"), "school"),
    (re.compile(r"\b(degree|qualification)\b"), "degree"),
    (re.compile(r"\b(discipline|major|field of study|course of study|specialisation|"
                r"specialization)\b"), "discipline"),
)

# "how many years of X experience" — the segment X decides answerable vs not.
_YEARS_RE = re.compile(
    r"(?:how many |number of |total )?years?\s*(?:of\s+)?(?P<qual>[a-z0-9 +#/'’-]*?)\s*experience"
)
_YEARS_ALT_RE = re.compile(r"experience\s*\(\s*years?\s*\)|years? experience")
# Only these qualifiers still mean "your total career length".
GENERIC_QUALIFIERS = {"", "work", "working", "professional", "industry", "total",
                      "overall", "relevant", "full time", "full-time", "job",
                      "employment", "career"}
# A trailing "... experience do you have with Jira" style qualifier.
_TRAILING_QUAL_RE = re.compile(r"experience\s+(?:with|in|using|on|as|at|for|of)\s+\S")


def parse_years_question(label: str) -> str | None:
    """Return 'total', 'qualified', or None.

    'total'     → answerable from profile.years_experience
    'qualified' → asks about a specific tool/domain; we cannot know it → refuse
    """
    text = normalise(label)
    if "experience" not in text:
        return None
    if not (_YEARS_RE.search(text) or _YEARS_ALT_RE.search(text)):
        return None
    if _TRAILING_QUAL_RE.search(text):
        return "qualified"
    m = _YEARS_RE.search(text)
    qual = (m.group("qual") or "").strip() if m else ""
    return "total" if qual in GENERIC_QUALIFIERS else "qualified"


"""Escape hatches a fixed vocabulary offers when the true answer isn't listed."""
OTHER_OPTION_RE = re.compile(
    r"^(other|others|other\b.*|not listed|none of the (above|these)|"
    r"prefer not to say|n/?a)$", re.I)


# Fields whose options are a closed list of institutions/qualifications, where
# "Other" is a real answer. Deliberately excludes anything about eligibility,
# consent, or demographics — "Other" is never a safe guess there.
VOCABULARY_KEYS = frozenset({"school", "degree", "discipline"})

# Labels that are chrome, not questions. A combobox rendered by a framework
# often reports its placeholder, so "Select..." arrives as the label for a
# dozen unrelated fields — useless as an answer key and actively dangerous,
# since one stored answer would then apply to all of them.
PLACEHOLDER_LABEL_RE = re.compile(
    r"^(select\.*|select an option|choose\.*|please select|—|-|)$", re.I)


def question_key(label: str, element_id: str = "", name: str = "") -> str:
    """A stable, human-meaningful key for one form question.

    Prefers the visible label. When that is only a placeholder, falls back to
    the control's own id/name — Greenhouse names them `degree--0`,
    `discipline--0`, so stripping the positional suffix recovers the real
    question the label failed to give us.
    """
    text = (label or "").strip()
    if text and not PLACEHOLDER_LABEL_RE.match(text):
        return text.rstrip("*").strip()
    for raw in (element_id, name):
        slug = (raw or "").strip()
        if not slug:
            continue
        slug = re.sub(r"[-_]{1,2}\d+$", "", slug)          # degree--0 → degree
        slug = re.sub(r"[-_]+", " ", slug).strip()
        if slug and not PLACEHOLDER_LABEL_RE.match(slug):
            return slug
    return text or "unlabelled question"


def find_other_option(options: tuple[str, ...] | list[str]) -> str | None:
    """The list's own "Other" entry, if it has one.

    Only meaningful once a real match has been ruled out. Selecting "Other"
    because the school genuinely is not in the vocabulary is a true statement;
    selecting it because matching was sloppy would hide a wrong answer, so
    callers must try `match_option` first.
    """
    for opt in options or ():
        if opt and OTHER_OPTION_RE.match(opt.strip()):
            return opt
    return None


def match_option(value: str, options: tuple[str, ...] | list[str]) -> str | None:
    """Reconcile a profile-derived value with a select/radio's own options.

    Returns the exact option string to pick, or None if we can't map it — in
    which case the caller refuses rather than picking something plausible.
    """
    if not options:
        return value
    val = (value or "").strip().lower()
    if not val:
        return None
    opts = [o for o in options if o and o.strip()]

    def norm(o: str) -> str:
        """Strip the decoration option lists carry: dial codes, ISO codes, punctuation."""
        s = o.strip().lower()
        s = re.sub(r"[+(]\s*\d[\d\s)-]*", " ", s)       # "+246", "(+91)"
        s = re.sub(r"[^a-z0-9\s]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    for opt in opts:                                        # exact, raw
        if opt.strip().lower() == val:
            return opt
    for opt in opts:                                        # exact once decoration is stripped
        if norm(opt) == val:
            return opt

    # Substring matching must respect word boundaries. Plain `in` maps "India"
    # onto "British Indian Ocean Territory" — a wrong country submitted on a real
    # application. Require a whole-word hit, and among several take the tightest
    # (shortest) option rather than whichever happened to come first.
    pattern = re.compile(rf"\b{re.escape(val)}\b")
    hits = [o for o in opts if pattern.search(norm(o))]
    if hits:
        return min(hits, key=lambda o: len(norm(o)))

    hits = [o for o in opts
            if len(norm(o)) > 2 and re.search(rf"\b{re.escape(norm(o))}\b", val)]
    if hits:
        return min(hits, key=lambda o: len(norm(o)))
    return None


# ── The answerer ─────────────────────────────────────────────────────

@dataclass
class ProfileAnswerer:
    """Answers form fields from the master profile — or declines."""
    profile: dict = field(default_factory=dict)

    # -- derived profile values ---------------------------------------
    def _get(self, key: str) -> str:
        val = self.profile.get(key)
        return "" if val is None else str(val).strip()

    @property
    def full_name(self) -> str:
        return self._get("name")

    @property
    def first_name(self) -> str:
        parts = self.full_name.split()
        return parts[0] if parts else ""

    @property
    def last_name(self) -> str:
        parts = self.full_name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    @property
    def phone_country_code(self) -> str:
        m = re.match(r"\s*\+(\d{1,3})", self._get("phone"))
        return f"+{m.group(1)}" if m else ""

    @property
    def phone_local(self) -> str:
        """National number: country code and separators stripped."""
        raw = self._get("phone")
        digits = re.sub(r"\D", "", raw)
        cc = self.phone_country_code.lstrip("+")
        if cc and digits.startswith(cc) and len(digits) > len(cc):
            digits = digits[len(cc):]
        return digits

    @property
    def total_years(self) -> str:
        val = self.profile.get("years_experience")
        if val in (None, ""):
            return ""
        try:
            years = float(val)
        except (TypeError, ValueError):
            return ""
        # Floor, never round up — overstating tenure to an employer is the exact
        # failure mode this module exists to prevent.
        return str(int(math.floor(years)))

    @staticmethod
    def _answer_key(text: str) -> str:
        """Normalise a question so 'School*', 'school', 'School' are one key."""
        return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()

    def _override_for(self, spec: FieldSpec) -> str:
        """Override matched on the visible label, or on the derived key.

        A placeholder-labelled control ("Select…") can only be answered by its
        derived key, so try both — label first, since it is what the user sees
        and what they most likely typed into answers.yaml.
        """
        return (self._override(spec.label or spec.name)
                or self._override(question_key(spec.label, spec.element_id, spec.name)))

    def _override(self, label: str) -> str:
        """User-supplied answer for a question the profile cannot cover.

        Lives in config/answers.yaml. This is not a licence to invent: the file
        is the user asserting facts about themselves, exactly as the resume does.
        It exists so a question answered once is not asked forever.
        """
        overrides = self.profile.get("_answer_overrides") or {}
        if not overrides:
            return ""
        want = self._answer_key(label)
        for key, val in overrides.items():
            if val in (None, ""):
                continue
            if self._answer_key(str(key)) == want:
                return str(val).strip()
        return ""

    def _education(self, field_name: str) -> str:
        """First non-empty value for `field_name` across education entries.

        Entries are stored most-recent-first, and earlier ones can be sparse (a
        college row may carry no degree), so fall through rather than reporting
        blank when the answer exists lower down.
        """
        entries = self.profile.get("education") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            val = entry.get(field_name)
            if val not in (None, ""):
                return str(val).strip()
        return ""

    def _value_for(self, key: str) -> str:
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "email": self._get("email"),
            "phone": self.phone_local,
            "phone_country_code": self.phone_country_code,
            "linkedin": self._get("linkedin"),
            "portfolio": self._get("portfolio"),
            "location": self._get("location"),
            "headline": self._get("headline"),
            "years_experience": self.total_years,
            "school": self._education("institution"),
            "degree": self._education("degree"),
            "discipline": self._education("field"),
        }.get(key, "")

    # -- the decision -------------------------------------------------
    def answer(self, spec: FieldSpec) -> Answer:
        label = spec.label or spec.name
        text = normalise(label)

        if spec.kind == "file":
            return Answer(ok=True, value=None, source="handled:file-upload")

        # 1. Already filled in by LinkedIn from the user's own profile — keep it.
        #    Reusing what the user already told LinkedIn is not inventing anything.
        if spec.kind in ("text", "textarea", "number", "select") and spec.value.strip():
            return Answer(ok=True, value=spec.value.strip(), source="prefilled")
        if spec.kind in ("radio", "checkbox") and spec.value.strip():
            return Answer(ok=True, value=spec.value.strip(), source="prefilled")

        # 2. An answer the user wrote themselves in config/answers.yaml wins over
        #    everything below, including the refusal patterns. Those patterns stop
        #    *this code* from guessing at visa status or salary; they are not a
        #    reason to withhold an answer the user has explicitly stated. Ticking
        #    a required consent box stays excluded — agreeing to terms is an act
        #    the user performs, not a value they configure.
        consent_checkbox = spec.kind == "checkbox" and spec.required
        if not consent_checkbox:
            override = self._override_for(spec)
            if override:
                picked = match_option(override, spec.options) if spec.options else override
                if picked:
                    return Answer(ok=True, value=picked, source="answers.yaml")

        # 3. Hard refusals, checked before any positive rule can fire.
        for pattern, why in REFUSE_PATTERNS:
            if pattern.search(text):
                return unanswerable(label, why)

        # 3. Consent / agreement checkboxes are the user's to give.
        if spec.kind == "checkbox":
            if spec.required:
                return unanswerable(label, "requires your explicit consent")
            return Answer(ok=True, value=None, source="skipped:optional-checkbox")

        # 4. Yes/no screening questions — never guess a side.
        opts_lower = {o.strip().lower() for o in spec.options if o and o.strip()}
        if opts_lower and opts_lower <= YES_NO_OPTIONS | {"select an option", "", "-"}:
            return unanswerable(label, "yes/no screening question")

        # 5. Years-of-experience.
        years_kind = parse_years_question(label)
        if years_kind == "qualified":
            return unanswerable(
                label, "asks tenure with a specific tool/domain, which the profile "
                       "does not record")
        if years_kind == "total":
            value = self.total_years
            if not value:
                return unanswerable(label, "profile has no years_experience")
            picked = match_option(value, spec.options)
            if picked is None:
                return unanswerable(label, "cannot map total experience to the options")
            return Answer(ok=True, value=picked, source="profile:years_experience")

        # 6. Whitelisted identity/contact fields.
        for pattern, key in RULES:
            if not pattern.search(text):
                continue
            value = self._value_for(key)
            if not value:
                return unanswerable(label, f"profile has no {key}")
            picked = match_option(value, spec.options)
            if picked is None and key in VOCABULARY_KEYS:
                # Fixed vocabularies (school/degree lists) legitimately omit
                # smaller or newer institutions. Once a real match is ruled out,
                # the list's own "Other" entry is the truthful answer — better
                # than blocking the application, and never a wrong university.
                other = find_other_option(spec.options)
                if other:
                    return Answer(ok=True, value=other,
                                  source=f"profile:{key}→other (not in this list)")
            if picked is None:
                return unanswerable(label, f"cannot map profile {key} to the options")
            return Answer(ok=True, value=picked, source=f"profile:{key}")

        # 7. Optional free text (cover letter, "anything else?") — leave blank.
        if not spec.required and spec.kind in ("textarea", "text", "number"):
            return Answer(ok=True, value=None, source="skipped:optional-blank")

        # 8. Anything left is a required question we have no honest answer for.
        return unanswerable(label, "no profile answer for this question")


def summarise_payload(answered: list[tuple[FieldSpec, Answer]]) -> list[dict]:
    """Flatten answers into a loggable audit record (what WOULD be submitted)."""
    out = []
    for spec, ans in answered:
        out.append({
            "question": spec.label,
            "kind": spec.kind,
            "required": spec.required,
            "answer": ans.value,
            "source": ans.source,
        })
    return out
