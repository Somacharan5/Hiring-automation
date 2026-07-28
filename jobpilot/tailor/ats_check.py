"""ATS scoring — how well a tailored resume covers the job description's keywords.

Real ATS systems (Greenhouse, Lever, Workday, Taleo) rank a resume mostly on
keyword overlap with the requisition plus their ability to parse the file at
all. This module models both halves:

    score_ats()             → weighted keyword coverage, 0-100
    verify_pdf_extractable()→ can a machine actually read the text back out?

Keyword extraction is deliberately dumb-but-honest: n-grams from the JD,
stopword/boilerplate filtered, weighted by frequency, phrase length, whether
the term is a recognisable skill/tool, and whether it appears in the job title.
"""

from __future__ import annotations

import re
from collections import Counter
from math import log

# ── Vocabulary -------------------------------------------------------

STOPWORDS = {
    "a", "about", "above", "across", "after", "against", "all", "also", "am", "an", "and",
    "any", "are", "around", "as", "at", "be", "because", "been", "before", "being", "below",
    "between", "both", "but", "by", "can", "could", "did", "do", "does", "doing", "down",
    "during", "each", "either", "else", "etc", "even", "ever", "every", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers", "him", "his",
    "how", "however", "i", "if", "in", "into", "is", "it", "its", "just", "like", "made",
    "make", "makes", "many", "may", "me", "might", "more", "most", "much", "must", "my",
    "no", "nor", "not", "now", "of", "off", "on", "once", "one", "only", "or", "other",
    "others", "our", "ours", "out", "over", "own", "per", "same", "shall", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "under", "until", "up", "upon",
    "us", "use", "used", "using", "very", "via", "was", "we", "well", "were", "what", "when",
    "where", "whether", "which", "while", "who", "whom", "why", "will", "with", "within",
    "without", "would", "you", "your", "yours",
}

# Words that are everywhere in every JD and carry no ranking signal on their own.
NOISE = {
    "ability", "able", "applicant", "applicants", "application", "apply", "base", "benefits",
    "candidate", "candidates", "career", "colleague", "colleagues", "company", "compensation",
    "day", "days", "description", "different", "employee", "employees", "employer",
    "employment", "environment", "equal", "excellent", "experience", "experiences", "great",
    "group", "help", "high", "hire", "hiring", "including", "individual", "job", "join",
    "know", "large", "level", "location", "look", "looking", "love", "new", "office",
    "opportunity", "organization", "part", "pay", "people", "person", "place", "position",
    "posting", "range", "recruiter", "recruiting", "requirement", "requirements", "responsibilities",
    "responsibility", "role", "roles", "salary", "skill", "skills", "strong", "team", "teams",
    "thing", "things", "time", "want", "week", "work", "working", "world", "year", "years",
    "you'll", "we're", "us", "please", "note", "role's", "plus", "nice", "must", "good",
    "successful", "ideal", "self", "highly", "proven", "track", "record", "demonstrated",
    # HTML entity names — artifacts of imperfectly-unescaped JD markup, never real keywords
    "nbsp", "amp", "quot", "apos", "rsquo", "lsquo", "ldquo", "rdquo", "mdash", "ndash",
    "hellip", "bull", "middot", "gt", "lt",
    # Generic verbs/nouns that appear in every JD and rank nothing
    "solve", "solving", "understand", "understanding", "iterate", "iterating", "space",
    "field", "thrive", "passionate", "impact", "drive", "driving", "deliver", "delivering",
    "ensure", "ensuring", "support", "supporting", "partner", "partnering", "collaborate",
    "collaborating", "contribute", "contributing", "leverage", "leveraging", "focus",
    "across", "within", "problems", "problem", "solutions", "solution", "best", "better",
    "complex", "critical", "key", "core", "various", "multiple", "several", "etc",
    "find", "finding", "first", "second", "third", "step", "steps", "come", "back",
    "backed", "works", "way", "ways", "need", "needs", "get", "give", "keep", "put",
    "take", "start", "started", "end", "ends", "top", "bottom", "next", "last",
}

# Common place-name tokens. A resume isn't expected to echo the office location,
# so these would only ever depress a score.
LOCATION_NOISE = {
    "remote", "hybrid", "onsite", "on-site", "usa", "us", "uk", "eu", "emea", "apac",
    "india", "america", "american", "canada", "singapore", "london", "york", "francisco",
    "san", "bellevue", "seattle", "chicago", "austin", "boston", "denver", "atlanta",
    "mountain", "view", "palo", "alto", "bangalore", "bengaluru", "mumbai", "delhi",
    "gurgaon", "noida", "hyderabad", "pune", "chennai", "dublin", "berlin", "paris",
    "amsterdam", "toronto", "sydney", "tokyo", "ca", "ny", "wa", "tx", "ma", "il",
}

# Sentences containing any of these are legal/benefits boilerplate — dropped whole.
BOILERPLATE_MARKERS = (
    "equal opportunity", "equal employment", "without regard to", "regardless of race",
    "reasonable accommodation", "accommodations", "affirmative action", "e-verify",
    "background check", "drug screen", "protected veteran", "gender identity",
    "sexual orientation", "disability status", "criminal histories", "fair chance",
    "salary range", "compensation range", "base pay", "401", "pto", "paid time off",
    "medical, dental", "dental and vision", "life insurance", "parental leave",
    "we are committed to diversity", "diverse and inclusive", "privacy policy",
    "applicant privacy", "visa sponsorship is not", "recruitment agencies",
    "third-party recruiters", "unsolicited resumes",
)

# Terms that are recognisably a skill / tool / methodology → weighted up.
SKILL_HINTS = {
    "a/b", "aarrr", "agile", "airflow", "amplitude", "analytics", "api", "apis", "aws",
    "b2b", "b2c", "backlog", "bigquery", "churn", "ci/cd", "cloud", "cohort", "crm", "css",
    "customer", "d2c", "dashboard", "dashboards", "data", "discovery", "docker", "ecommerce",
    "e-commerce", "experimentation", "figma", "fintech", "forecasting", "funnel", "gcp",
    "genai", "gmv", "go-to-market", "gtm", "growth", "html", "jira", "jtbd", "kpi", "kpis",
    "kubernetes", "llm", "llms", "looker", "machine", "marketplace", "metrics",
    "mixpanel", "ml", "mobile", "monetization", "moscow", "mvp", "n8n", "notion", "okr",
    "okrs", "onboarding", "payments", "personalization", "pricing", "prd", "prds", "product",
    "python", "qualitative", "quantitative", "rest", "retention", "rice", "roadmap",
    "roadmapping", "roi", "saas", "scrum", "segmentation", "seo", "shopify", "sql", "sprint",
    "stakeholder", "stakeholders", "strategy", "tableau", "ux", "wireframe", "wireframes",
    "wireframing", "zapier",
}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#/&.\-']*")
_SENT_SPLIT = re.compile(r"[.!?\n\r;•·|]+")


# ── Text helpers -----------------------------------------------------

def _normalize(text: str) -> str:
    text = (text or "").lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("–", "-").replace("—", "-")
    return text


def _tokens(text: str) -> list[str]:
    return [t.strip(".'-") for t in _WORD_RE.findall(_normalize(text)) if t.strip(".'-")]


def _stem(tok: str) -> str:
    """Very conservative suffix stripping so 'roadmaps' matches 'roadmap'."""
    for suf in ("ies",):
        if tok.endswith(suf) and len(tok) > 5:
            return tok[: -len(suf)] + "y"
    for suf in ("ing", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            return tok[: -len(suf)]
    return tok


def _key(ngram: tuple[str, ...]) -> str:
    return " ".join(_stem(t) for t in ngram)


def _strip_boilerplate(text: str) -> str:
    kept = []
    for sentence in _SENT_SPLIT.split(_normalize(text)):
        if any(marker in sentence for marker in BOILERPLATE_MARKERS):
            continue
        kept.append(sentence)
    return ". ".join(kept)


def _is_junk(tok: str) -> bool:
    return (
        tok in STOPWORDS
        or tok in NOISE
        or len(tok) < 2
        or tok.isdigit()
        or not any(c.isalpha() for c in tok)
    )


# ── Keyword extraction ------------------------------------------------

def extract_keywords(job_description: str, job_title: str = "", company: str = "",
                     location: str = "", top_n: int = 32) -> list[tuple[str, float]]:
    """Return [(keyword, weight)] — the terms an ATS would rank this req on.

    `company` is excluded: a resume is not expected to contain the hiring
    company's own name, so counting it would just depress every score.
    """
    body = _strip_boilerplate(job_description or "")
    title_terms = {_stem(t) for t in _tokens(job_title) if not _is_junk(t)}
    company_terms = {_stem(t) for t in _tokens(company) if len(t) > 2}
    # Place names rank nothing on a resume — a candidate isn't expected to
    # write "San Francisco" to match a req. Excluded like the company name.
    location_terms = {_stem(t) for t in _tokens(location) if len(t) > 1} | LOCATION_NOISE

    counts: Counter[str] = Counter()
    surface: dict[str, str] = {}          # stem-key → nicest surface form
    for sentence in _SENT_SPLIT.split(body):
        toks = [t for t in _tokens(sentence)]
        for n in (1, 2, 3):
            for i in range(len(toks) - n + 1):
                gram = tuple(toks[i:i + n])
                if _is_junk(gram[0]) or _is_junk(gram[-1]):
                    continue
                if n > 1 and any(t in STOPWORDS for t in gram[1:-1]):
                    continue
                if n > 1 and all(t in NOISE or t in STOPWORDS for t in gram):
                    continue
                k = _key(gram)
                counts[k] += 1
                surface.setdefault(k, " ".join(gram))

    scored: dict[str, float] = {}
    for k, freq in counts.items():
        gram = k.split()
        n = len(gram)
        if any(t in company_terms for t in gram):
            continue                       # the hiring company's own name
        if any(t in location_terms for t in gram):
            continue                       # office locations, not skills
        if any("'" in t for t in gram):
            continue                       # possessives ("world's", "company's")
        if n == 1 and freq < 2 and gram[0] not in SKILL_HINTS:
            continue                       # one-off single words are noise
        if n > 1 and freq < 2:
            continue                       # a phrase must recur to matter
        weight = 1.0 + log(freq)
        if n == 2:
            weight *= 1.35                 # phrases are stronger ATS signals
        elif n == 3:
            weight *= 1.5
        if any(t in SKILL_HINTS for t in gram):
            weight *= 1.4
        if any(_stem(t) in title_terms for t in gram):
            weight *= 2.0                  # title-relevant terms dominate
        scored[k] = weight

    # Drop n-grams fully contained in a higher-weighted longer phrase.
    ordered = sorted(scored.items(), key=lambda kv: -kv[1])
    chosen: list[tuple[str, float]] = []
    for k, w in ordered:
        if any(k != other and f" {k} " in f" {other} " for other, _ in chosen):
            continue
        chosen.append((k, w))
        if len(chosen) >= top_n:
            break
    return [(surface.get(k, k), w) for k, w in chosen]


# ── Resume side -------------------------------------------------------

def tailored_text(tailored) -> str:
    """Flatten a TailoredResume into the plain text an ATS would index."""
    parts = [tailored.summary or "", " ".join(tailored.selected_skills or [])]
    for exp in tailored.experiences or []:
        parts.append(f"{exp.title} {exp.company} {exp.location or ''}")
        parts.extend(exp.bullets or [])
    return "\n".join(parts)


def _resume_ngram_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for sentence in _SENT_SPLIT.split(_normalize(text)):
        toks = _tokens(sentence)
        for n in (1, 2, 3):
            for i in range(len(toks) - n + 1):
                keys.add(_key(tuple(toks[i:i + n])))
    return keys


def score_ats(tailored, job_description: str, job_title: str = "",
              company: str = "", location: str = "") -> tuple[int, list[str], list[str]]:
    """Score a tailored resume against a JD.

    Returns (score 0-100, keywords_matched, keywords_missing). The score is the
    share of total keyword *weight* the resume covers, so hitting the handful of
    title-relevant, repeated terms matters far more than hitting a long tail.
    `job_title` and `company` are optional but improve the weighting a lot.
    """
    keywords = extract_keywords(job_description, job_title, company, location)
    if not keywords:
        return 0, [], []

    resume_keys = _resume_ngram_keys(tailored_text(tailored))
    matched: list[str] = []
    missing: list[str] = []
    hit_weight = total_weight = 0.0
    for kw, weight in keywords:
        total_weight += weight
        if _key(tuple(kw.split())) in resume_keys:
            hit_weight += weight
            matched.append(kw)
        else:
            missing.append(kw)

    score = int(round(100 * hit_weight / total_weight)) if total_weight else 0
    return max(0, min(100, score)), matched, missing


# ── Rendered-file sanity check ---------------------------------------

def verify_pdf_extractable(pdf_path: str, min_chars: int = 400) -> bool:
    """True if pypdf can pull real text out of the PDF.

    A resume whose text can't be extracted (outlined fonts, image-only export)
    scores zero with every real ATS regardless of how good the content is.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return False
    return len(text.strip()) >= min_chars


def pdf_page_count(pdf_path: str) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf_path)).pages)


def pdf_text(pdf_path: str) -> str:
    from pypdf import PdfReader

    return "\n".join(p.extract_text() or "" for p in PdfReader(str(pdf_path)).pages)
