"""Email drafting — one genuinely personal email per (job, recruiter).

Drafts are persisted with status 'drafted'. Nothing here can send; the only
send path is `sender.send_pending`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..db import get_job, latest_resume_for_job, upsert_application
from ..llm import structured_call
from ..profile import load_profile_for_matching

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = ROOT / "config"

MAX_WORDS = 165          # hard ceiling; the prompt asks for ~150
MIN_WORDS = 55


def load_positioning() -> str:
    """The candidate's direction statement — the 'why them' backbone of every email."""
    p = CONFIG / "positioning.md"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def _signature_lines(cfg: dict) -> list[str]:
    """Deterministic signature, appended AFTER quality checks (URLs would trip them)."""
    phone = cfg.get("from_phone") or "+91-9182935177"
    email = cfg.get("signature_email") or "soma.charan@mastersunion.org"
    lines = [f"{phone} · {email}"]
    if cfg.get("portfolio_link"):
        lines.append(f"Portfolio: {cfg['portfolio_link']}")
    if cfg.get("github"):
        lines.append(f"GitHub: {cfg['github']}")
    return lines

# Phrases that instantly mark an email as machine-written boilerplate.
BANNED = (
    "i am writing to express", "i am writing to apply", "keen interest",
    "i hope this email finds you well", "i hope this finds you well",
    "dear hiring manager", "to whom it may concern", "perfect fit",
    "passionate about", "i am confident that i", "esteemed", "your esteemed",
    "leverage my", "synergy", "dynamic environment", "wealth of experience",
    "seamlessly", "game-changer", "rockstar", "ninja", "value add",
    "thank you for your time and consideration", "look forward to hearing from you at your earliest",
    "as you can see from my resume", "results-driven professional",
    # abstraction / consultant-speak that the model reaches for unprompted
    "for your review", "aligns with your", "align with your", "drive adoption",
    "i specialize in", "i am interested in the", "cutting-edge", "best-in-class",
    "spearhead", "holistic", "in today's", "proven track record", "skill set",
    "would welcome a brief conversation", "at your earliest convenience",
)

SYSTEM = """You are {from_name}, writing one short, direct application email to one recruiter
for one specific role. Make the application intent clear and lead with the role — this is an
application, not a networking note. Not a cover letter, not marketing copy: a concise email a
busy person will actually read and act on.

TRUTH (non-negotiable)
- Every claim must already exist in the CANDIDATE PROFILE below. Never invent a
  company, metric, tool, title, or year, and never upgrade one ("helped with" does
  not become "led"). If a strength isn't in the profile, it does not exist.
- Never write "we" or "our" about the recruiter's company. You don't work there yet.
- If the candidate has a gap for this role, don't raise it and don't bluff past it.

SHAPE (follow exactly)
  Line 1:  Hi <recruiter's first name>,     (or "Hi there," if you weren't given one)
  Para 1:  TWO short sentences. First: state plainly that you're applying for the exact role
           by name (e.g. "I'm applying for the Product Manager, Growth role at Sarvam."). Second:
           the single most relevant thing the candidate actually did for THIS role, with its
           real number and real company name.
  Para 2:  2-3 short sentences that must NOT begin with "I". Start with the team, product
           surface, or problem named in the job description ("The activation team's push to…",
           "Building the risk tooling for…"), then connect it to one more concrete thing the
           candidate actually built, with its real number.
  Para 3:  ONE direct sentence. Point to the attached resume and ask for the next step — a
           short call, or to be considered for the role. Confident, not tentative ("I'd welcome
           a quick call to walk through…"), never "happy to send more if that's useful".
  Sign-off: "Best," then "{from_name}" on its own line. Nothing else. No phone,
           no links, no title block.

VOICE
- 110-150 words total. Under 140 is better.
- Write like you talk. Contractions are good. Short sentences are good.
- Concrete nouns only. If a phrase stacks abstractions ("prescriptive methodologies",
  "knowledge systems", "internal agility", "operational excellence", "a bridge between
  strategic intent and execution"), delete it and name the actual thing that was built
  and what changed because of it.
- Every sentence about the candidate must contain a real proper noun (a company, tool,
  team, or product name) or a real number. A sentence with neither is filler — cut it.
- No empty flattery, and no praise of brand or scale. But DO include ONE concrete
  sentence connecting what this company actually builds (a real product, team, or
  problem from the job description) to the candidate's genuine direction below —
  a specific, honest connection, never a generic mission compliment.
- Banned outright: "I am writing to express", "keen interest", "passionate about",
  "hope this email finds you well", "perfect fit", "leverage", "wealth of experience",
  "aligns with", "for your review", "I specialize in", "proven track record",
  "at your earliest convenience", "cutting-edge", "best-in-class".
- Reference the attached resume once, in the closing ask — it holds the detail; the email is the pitch.

SUBJECT
- 4-9 words. Sentence case. Names the role plus one hook the recruiter would recognise.
- No "Application for the position of…", no exclamation marks, no emoji, no brackets.

== CANDIDATE PROFILE (the only facts you may use) ==
{profile}

== CANDIDATE'S DIRECTION (what they want to build toward — use to connect honestly to THIS company; distil it, never copy it verbatim, never go generic) ==
{positioning}"""


class EmailDraft(BaseModel):
    subject: str = Field(description="4-9 words, specific to the role, no filler")
    body: str = Field(description="110-150 word plain-text email, greeting through sign-off")


# ── quality gate ─────────────────────────────────────────────────────

def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


# Acronyms a PM legitimately writes in caps; anything else in caps is suspect.
OK_ACRONYMS = {
    "AI", "API", "APIS", "PRD", "PRDS", "GTM", "KPI", "KPIS", "SOP", "SOPS", "CRM",
    "MIS", "HR", "GPT", "ROI", "CAC", "B2B", "B2C", "D2C", "SAAS", "UX", "UI", "ML",
    "LLM", "LLMS", "SQL", "PM", "OKR", "OKRS", "MVP", "NPS", "ATS", "CTO", "CEO",
    "COO", "CPO", "VP", "USA", "UK", "UAE", "EU", "IT", "QA", "AWS", "GCP", "RICE",
    "JTBD", "AARRR", "EDTECH", "IPO", "SDK", "CI", "CD", "A/B", "AB",
}


def _gibberish_issues(text: str) -> list[str]:
    """Catch model artifacts — base64 blobs, stray tokens, shouted nonsense.

    A generation glitch that reaches a real recruiter's inbox is unrecoverable,
    so anything that doesn't look like written English gets the draft rejected.
    """
    issues: list[str] = []
    for tok in re.findall(r"\b[A-Za-z0-9+/=_-]{15,}\b", text or ""):
        if not re.fullmatch(r"[A-Za-z]+", tok):        # letters-only long words are fine
            issues.append(f"body contains a non-word token (model artifact): {tok!r}")
    shouty = [w for w in re.findall(r"\b[A-Z]{3,}\b", text or "")
              if w not in OK_ACRONYMS]
    if shouty:
        issues.append(f"body contains unexplained ALL-CAPS token(s): {shouty[:3]}")
    if re.search(r"[^\x09\x0a\x0d\x20-\x7e£€₹’‘“”–—…]", text or ""):
        issues.append("body contains unexpected control/binary characters")
    return issues


def quality_issues(draft: EmailDraft, contact_name: str | None = None) -> list[str]:
    """Deterministic checks the LLM output must pass before we keep it."""
    issues: list[str] = []
    body_l = (draft.body or "").lower()
    subj_l = (draft.subject or "").lower()

    for phrase in BANNED:
        if phrase in body_l or phrase in subj_l:
            issues.append(f"contains banned boilerplate phrase: {phrase!r}")

    n = _word_count(draft.body)
    if n > MAX_WORDS:
        issues.append(f"body is {n} words — cut it to under {MAX_WORDS}")
    if n < MIN_WORDS:
        issues.append(f"body is only {n} words — too thin to be credible")

    if len(draft.subject.split()) > 12:
        issues.append("subject line is too long (max ~9 words)")
    if "!" in draft.subject or "[" in draft.subject:
        issues.append("subject contains an exclamation mark or bracket template")

    if re.search(r"\{\{?\s*\w+\s*\}?\}|\[(name|company|role|title)\]", draft.body, re.I):
        issues.append("body contains an unfilled template placeholder")

    issues += _gibberish_issues(draft.body)
    issues += _gibberish_issues(draft.subject)

    if not re.search(r"\d", draft.body or ""):
        issues.append("body cites no concrete number — it reads as generic claims")

    first = (contact_name or "").split()[0] if contact_name else None
    if first and first.lower() not in body_l:
        issues.append(f"the recruiter's name ({first}) is not used in the greeting")

    return issues


# ── prompt assembly ──────────────────────────────────────────────────

def _match_context(job) -> str:
    """Pull the useful bits out of jobs.match_json (JSONB dict on Postgres, or NULL)."""
    raw = job["match_json"]
    if isinstance(raw, dict):
        v = raw
    else:
        try:
            v = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            v = {}
    lines = []
    if v.get("matched_strengths"):
        lines.append("Strengths this recruiter will care about (use ONE, the strongest):")
        lines += [f"  - {s}" for s in v["matched_strengths"][:5]]
    if v.get("tailoring_hints"):
        lines.append("Angles worth emphasising:")
        lines += [f"  - {h}" for h in v["tailoring_hints"][:3]]
    if v.get("gaps"):
        lines.append("Known gaps — do NOT mention or bluff around these: "
                     + "; ".join(v["gaps"][:4]))
    if not lines:
        lines.append("(No match analysis available — pick the strongest relevant "
                     "proof point from the profile yourself.)")
    return "\n".join(lines)


def _user_prompt(job, contact, from_name: str) -> str:
    jd = (job["description"] or "").strip()
    if len(jd) > 12_000:
        jd = jd[:12_000] + "\n[…truncated]"

    recipient = (contact.get("name") if isinstance(contact, dict) else contact["name"]) if contact else None
    title = (contact.get("title") if isinstance(contact, dict) else contact["title"]) if contact else None
    greeting = (f"Greet them by first name: {recipient.split()[0]}"
                if recipient else
                "You do NOT know the recipient's name — open with 'Hi there,' (never "
                "'Dear Hiring Manager'). Keep it warm and direct anyway.")

    return f"""RECIPIENT
Name: {recipient or '(unknown)'}
Title: {title or '(unknown — assume recruiting/talent)'}
Company: {job['company']}
{greeting}

ROLE
Title: {job['title'].strip()}
Location: {job['location'] or 'unspecified'}
Posting: {job['url'] or '(no link)'}

{_match_context(job)}

JOB DESCRIPTION
{jd if jd else '(none available — rely on the title and keep it short)'}

Write the email. Sign it "{from_name}". Return subject and body only."""


# ── public API ───────────────────────────────────────────────────────

def compose_email(conn, job_id: str, contact=None, settings: dict | None = None,
                  persist: bool = True, model: str | None = None) -> EmailDraft:
    """Draft a personalised email for one (job, contact) and persist it as 'drafted'.

    `contact` is a contacts row (or dict), or None for a nameless role address.
    Returns the validated EmailDraft. Sends nothing, ever.
    """
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"No job with id {job_id!r}")

    cfg = (settings or {}).get("outreach", {}) if settings else {}
    from_name = cfg.get("from_name") or "Me"
    model = model or ((settings or {}).get("matching", {}) or {}).get("model")

    system = SYSTEM.format(profile=load_profile_for_matching(),
                           positioning=load_positioning(), from_name=from_name)
    user = _user_prompt(job, contact, from_name)

    draft = structured_call(system, user, EmailDraft, model=model, max_tokens=4000)

    contact_name = None
    if contact is not None:
        contact_name = contact["name"] if not isinstance(contact, dict) else contact.get("name")

    issues = quality_issues(draft, contact_name)
    if issues:
        # One self-correction round with the concrete failures fed back.
        retry_user = (user + "\n\nA previous attempt was rejected for these reasons:\n"
                      + "\n".join(f"- {i}" for i in issues)
                      + "\n\nRewrite it from scratch, fixing every point. Same rules apply.")
        try:
            retry = structured_call(system, retry_user, EmailDraft, model=model, max_tokens=4000)
            if len(quality_issues(retry, contact_name)) < len(issues):
                draft, issues = retry, quality_issues(retry, contact_name)
        except Exception as e:  # noqa: BLE001 — a failed retry keeps the first draft
            print(f"  ⚠ draft retry failed ({type(e).__name__}) — keeping first attempt")
    if issues:
        print(f"  ⚠ draft for {job['company']} still has quality issues: {'; '.join(issues)}")

    # Append the deterministic signature AFTER the quality gate — its phone and
    # portfolio URL would otherwise trip the gibberish/ALL-CAPS checks.
    draft = EmailDraft(subject=draft.subject,
                       body=draft.body.rstrip() + "\n" + "\n".join(_signature_lines(cfg)))

    if persist:
        contact_id = None
        if contact is not None:
            contact_id = contact["id"] if not isinstance(contact, dict) else contact.get("id")
        resume = latest_resume_for_job(conn, job_id)
        resume_path = resume["pdf_path"] if resume and resume["pdf_path"] else str(master_resume() or "")
        # Unresolved quality issues ride along in `error` as a review note. The row
        # sits in 'preparing_resume' (prepared, not yet sent) — the sender flips it
        # to 'emailed' on a real send, and nothing sends while dry_run is on.
        note = ("REVIEW: " + "; ".join(issues)) if issues else None
        upsert_application(conn, job_id=job_id, channel="email", status="preparing_resume",
                           contact_id=contact_id, resume_path=resume_path or None,
                           subject=draft.subject, body=draft.body,
                           portfolio_link=cfg.get("portfolio_link"), error=note)
    return draft


def master_resume() -> Path | None:
    """The fallback CV PDF in the project root (used when no tailored resume exists)."""
    candidates = sorted(ROOT.glob("*.pdf"))
    for p in candidates:
        name = p.name.lower()
        if "cv" in name or "resume" in name:
            return p
    return None


def render_draft(subject: str, body: str, to: str | None = None,
                 attachments: list[str] | None = None) -> str:
    """Human-readable rendering used by dry-run output and review."""
    lines = [f"To:      {to or '(no verified address)'}", f"Subject: {subject}"]
    if attachments:
        lines.append(f"Attach:  {', '.join(Path(a).name for a in attachments)}")
    lines += ["-" * 68, body.strip(), "-" * 68]
    return "\n".join(lines)
