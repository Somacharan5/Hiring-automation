# Free recruiter-email discovery + verification

Replaces Hunter.io as the *primary* path with sources that cost nothing and have
no monthly quota. Hunter is still wired up, but demoted to a last-resort adapter
that only runs when everything free came up empty.

Nothing in `finder.py` was modified. Its public functions (`find_domain`,
`pattern_guess`, `generic_guess`, `verify_smtp`, `discover_contacts`,
`hunter_domain_search`, `apollo_people_search`, `finder_availability`) all still
import and behave exactly as before — verified by regression check.

---

## The one idea this rests on

An SMTP `RCPT TO` returning **250 does not mean the mailbox exists.** Most Google
Workspace tenants accept every recipient and bounce it internally later. Measured
live against this project's own job list:

| domain | RCPT to a random local part | meaning |
|---|---|---|
| `stripe.com` | **250 accepted** | catch-all — SMTP proves nothing |
| `databricks.com` | **250 accepted** | catch-all |
| `atlan.com` | **250 accepted** | catch-all |
| `postman.com` | **250 accepted** | catch-all |
| `snapdeal.com` | **250 accepted** | catch-all |
| `figma.com` | **550 rejected** | strict — probes are meaningful |
| `sarvam.ai` | **550 rejected** | strict |
| `gmail.com` | **550 rejected** | strict |

So `detect_catch_all()` runs **before** any positive probe is believed. Two
independent random local parts must *both* be accepted before a domain is called
catch-all, so one greylisting fluke cannot mislabel a strict domain. On a
catch-all domain every address is capped at `CATCH_ALL_CAP = 65`, which is
deliberately one notch below the auto-send bar — **nothing on a catch-all domain
can ever auto-send.** The verdict is cached per domain in `domain_intel`
(30-day TTL), so each domain costs one probe, not one per address.

Concrete payoff, both real: `careers@figma.com` **does not exist** (550) — the
old `generic_guess` fallback would have mailed it and bounced. `careers@sarvam.ai`
**does** exist (250 on a strict domain) and is genuinely safe to send to.

---

## Files added (all under `jobpilot/outreach/`)

| file | role |
|---|---|
| `verify.py` | syntax → disposable/role → MX → **catch-all** → RCPT probe |
| `free_sources.py` | site crawl, GitHub, Google CSE, LinkedIn names |
| `discovery.py` | orchestrator, confidence bands, pattern learning |
| `bounce.py` | Gmail IMAP DSN harvesting → feedback into discovery |
| `store.py` | local tables: `domain_intel`, `dead_patterns`, `bounce_log` |

`__init__.py` re-exports all of the below (added to `__all__`; existing exports
untouched).

### Public signatures for CLI wiring

```python
# discovery.py — the entry points a CLI should call
discover_and_verify(conn, company, domain=None, job_url=None, settings=None,
                    use_smtp=True, use_linkedin=False, max_verify=12) -> list[dict]
best_contact_for(conn, company, require_auto_sendable=True) -> dict | None
review_queue(conn, company=None) -> list[dict]
tier_for(confidence: int, verified: bool) -> str
can_auto_send(contact_row_or_dict) -> bool          # accepts a sqlite3.Row

# verify.py
verify_email(email, conn=None, use_smtp=True) -> VerificationResult
detect_catch_all(domain, conn=None, force=False, ttl_days=30) -> bool | None
smtp_probe(email) -> bool | None
check_syntax(email) -> bool ; check_mx(domain, conn=None) -> list[str]
is_disposable(domain) -> bool ; is_role_address(local) -> bool
role_kind(local) -> 'recruiting'|'generic'|'misdirected'|'undeliverable'|None

# free_sources.py
crawl_site_for_emails(domain) -> list[ContactCandidate]
github_emails(company, domain=None, token=None) -> list[ContactCandidate]
google_cse_search(company, domain, key, cx) -> list[ContactCandidate]
linkedin_recruiter_names(company, settings=None) -> list[ContactCandidate]  # names, no emails
free_source_availability() -> dict[str, bool]

# bounce.py
scan_bounces(mailbox=None, app_password=None, since_days=30) -> list[Bounce]
apply_bounce_feedback(conn, bounces) -> dict
run_bounce_loop(conn, since_days=30) -> dict        # scan + apply, one call
```

### The send gate (the contract callers must enforce)

```python
CONF_VERIFIED = 85   # SMTP-proved mailbox on a recipient-validating domain
CONF_HIGH     = 70   # strong evidence, not SMTP-proved
CONF_REVIEW   = 40   # plausible — human must approve
CATCH_ALL_CAP = 65   # hard ceiling on any catch-all domain (< CONF_HIGH by design)
AUTO_SENDABLE_TIERS = ("verified", "high")
```

`can_auto_send(row)` is the only check the sender needs. Everything else is a
review-queue item.

---

## What was actually run and proven

**Live-verified (real network, real domains):**

- **Catch-all detection** — the table above. Distinguishes catch-all from strict
  correctly on 8/8 real domains.
- **`verify_email` end-to-end**, the three required cases:
  - real known-good on a strict domain → `careers@sarvam.ai` = **verified** (88)
  - obvious fake on a strict domain → `zzq9fakeuser1234@figma.com` = **invalid** (0, SMTP 550)
  - obvious fake on a **catch-all** domain → `zzq9fakeuser1234@stripe.com` =
    **`catch_all`** (50, routed to review) — *not* verified. This is the case that
    would make the whole thing unsafe if it were wrong.
- **`crawl_site_for_emails`** — real results: `stripe.com` → 4 (`careers@`,
  `accommodations@`, `candidatefeedback-applications@`, `jane.diaz@`);
  `postman.com` → 3 (`accommodations@`, `info-jp@`, `info@`); `snapdeal.com` → 2;
  `figma.com` → 1 (`press@`). Nothing on `sarvam.ai`, `atlan.com`,
  `databricks.com` (JS-rendered pages, see limitations).
- **`github_emails`** (anonymous, no token) — `stripe` → 18 on-domain addresses,
  `figma` → 18, `postman` → 5 (via org-search fallback `postman`→`postmanlabs`),
  `atlan`→`atlanhq` resolved. Two GitHub-found Figma addresses were then
  SMTP-**verified** as real (`ckalmar@figma.com`, `ilin@figma.com`).
- **Pattern learning** — inferred `flast` for figma.com and `first.last` for
  postman.com from real commit data, both at 100% agreement. Proof it matters:
  `ckalmar@figma.com` verifies (250) while `chris.kalmar@figma.com` is rejected
  (550) — the wrong convention would have bounced.
- **Bounce feedback** — DSN parsing (hard 5.x.x / soft 4.x.x / non-bounce) and
  `apply_bounce_feedback` against the real DB: contact zeroed, pattern demoted,
  and a follow-up discovery run confirmed to stop emitting the burned pattern.
- **Graceful degradation** — missing LinkedIn session, unkeyed CSE, no domain for
  GitHub, GitHub rate-limit exhaustion, and dnspython-absent (falls back to `dig`).

**NOT live-verified — do not assume these work:**

- **`linkedin_recruiter_names`** — only the *graceful-skip* path was exercised.
  The scraping path was **never run against live LinkedIn**: doing so opens a
  visible browser on the user's authenticated account and risks a checkpoint/
  restriction, which is not mine to spend. The DOM selectors are best-effort and
  LinkedIn changes them often — **expect to fix selectors on first real run.**
  It is off by default (`use_linkedin=False`).
- **`google_cse_search`** — no `GOOGLE_CSE_KEY`/`CX` present, so it was only
  tested returning `[]` when unkeyed.
- **`scan_bounces` IMAP** — `GMAIL_APP_PASSWORD` in `.env` is still the literal
  placeholder `PASTE_HERE`, so no live IMAP session was possible. Parsing and the
  DB feedback half are proven against a synthetic Gmail DSN; the *connection*
  half is unproven. `is_placeholder_password()` now detects this and skips with a
  clear message instead of a confusing `AUTHENTICATIONFAILED`.
- **Hunter adapter** — unchanged and unkeyed; not re-tested.

**Environment note:** outbound **port 25 is OPEN** on this machine/network — all
SMTP results above are genuine. On networks where it is blocked (many home ISPs,
most cloud hosts), `detect_catch_all` returns `None`, `verify_email` returns
`unknown` capped at 55, and **everything routes to review**. That degradation is
safe but makes the tool much less useful; worth calling out in the README for
open-source users.

---

## Config this layer reads

`config/settings.yaml` already grew an `email_discovery:` block during this work
(added by the integrator, not by me). **`discovery.py` now honours it:**

```yaml
email_discovery:
  sources: ["site_crawl", "github", "linkedin_names", "cse", "hunter"]
  min_confidence_to_autosend: 80   # stricter than my default 70 — respected
  review_catch_all: true
  bounce_scan_days: 30
  # additional keys this code understands (both optional):
  max_verify_per_company: 12
  smtp_verify: true                # set false where port 25 is blocked
```

Read via `discovery.email_discovery_cfg(settings)`; every key is optional and
defaults safely. `sources` gates which finders run, and `min_confidence_to_autosend`
is applied by `can_auto_send(row, settings=...)`, `best_contact_for(...,
settings=...)` and `review_queue(..., settings=...)`.

Two deliberate safety properties: a malformed value falls back to the **stricter**
default rather than the looser one, and `min_confidence_to_autosend` is clamped to
`CATCH_ALL_CAP + 1` (66) — **config cannot lower the bar far enough to make
catch-all addresses auto-sendable.** Setting it to `10` yields `66`.

Note `review_catch_all: true` is structurally guaranteed rather than merely
honoured: the catch-all cap sits below the auto-send floor by construction, so
catch-all addresses always land in review even if the flag were flipped off.

## Requests for shared files (I did not edit these)

**`requirements.txt`** — ✅ **already done** by the integrator during this work
(`dnspython>=2.6` is now declared). It was installed in `.venv` (2.8.0) but not
declared and not transitive, so a fresh install would have missed it. The code
falls back to `dig`/`nslookup` when it is absent — I tested that path by
simulating the import failure, and MX resolution still works.

**`jobpilot/db.py`** — two things:

1. `add_contact` is `INSERT OR IGNORE`, so **re-running discovery can never lower
   a previously-stored confidence.** A row written optimistically by the old
   finder stays optimistic forever, and re-verification cannot correct it. I hit
   this live (a `press@figma.com` row stored at 92 before the misdirected-inbox
   rule existed had to be reconciled by hand). Suggested: an `ON CONFLICT(company,
   email) DO UPDATE` path, or an `update_contact_confidence(conn, contact_id,
   confidence, verified)` helper.
2. Optionally fold `store.DDL` (`domain_intel`, `dead_patterns`, `bounce_log`)
   into `db.SCHEMA`. Not required — `store.ensure_tables()` is idempotent and is
   called on every entry point — but it would keep all schema in one place.

**`config/settings.yaml`** — the `email_discovery:` block is already in place and
wired (see "Config this layer reads"). Two optional additions I did **not** add:
```yaml
email_discovery:
  catch_all_ttl_days: 30                     # re-probe cadence (currently a code default)
  probe_sender: "verify-probe@example.com"   # MAIL FROM used for probes
```
Also worth noting: `outreach.finders: ["hunter", "apollo", "pattern"]` still
drives the **old** `finder.discover_contacts` path. The new pipeline uses
`email_discovery.sources` instead. Both work; they are just separate entry points,
and the old one has no catch-all protection.

**`.env`** — optional additions: `GITHUB_TOKEN` (strongly recommended, see
limitations), `GOOGLE_CSE_KEY`, `GOOGLE_CSE_CX`. And `GMAIL_APP_PASSWORD` needs a
real value before the bounce loop can run.

---

## Honest limitations

1. **GitHub's anonymous 60 req/hr is the binding constraint.** One company costs
   ~10-25 calls, so roughly **3 companies per hour** before exhaustion — I burned
   it during testing. At the user's 300 applications/month this is *not*
   optional: set `GITHUB_TOKEN` for 5000/hr. A circuit breaker now backs off
   until the reset time instead of hammering.
2. **Catch-all domains are the common case among large employers.** 5 of 8
   domains tested were catch-all, including Stripe, Databricks and Postman. For
   those, this layer can honestly only ever produce review-queue items — no
   verification technique (free *or* paid) can prove a mailbox on a catch-all
   domain. The bounce loop is the only thing that ever resolves them, and only
   after the fact.
3. **Site crawling misses JS-rendered pages.** `sarvam.ai` and `atlan.com` return
   a shell with no addresses because content is client-rendered; we fetch HTML
   only and do not run a browser. Playwright could fix this at a large speed cost.
4. **Crawled addresses can be marketing fictions.** `jane.diaz@stripe.com` was
   harvested from Stripe's homepage — almost certainly a demo persona in a
   product screenshot, not a person. On a catch-all domain it cannot be
   disproved, which is exactly why site-crawl hits are capped at review.
5. **Deliverability ≠ appropriateness.** `press@figma.com` verifies perfectly and
   is the wrong place to send a resume. `role_kind()` now classifies
   `misdirected` inboxes (press/legal/security/billing/investors…) and caps them
   at 60 so they never auto-send. The list is hand-maintained and certainly
   incomplete.
6. **Commit-author emails are stale by nature.** A `@company.com` address in git
   history belongs to whoever pushed it, who may have since left. On strict
   domains the RCPT probe catches this; on catch-all domains it does not.
7. **GitHub finds engineers, not recruiters.** Its real value here is *inferring
   the naming convention*, which then turns a LinkedIn recruiter name into a
   targeted guess. That chain depends on the LinkedIn source, which is the least
   proven piece.
8. **SMTP probing is a grey area.** We connect, `EHLO`, `MAIL FROM`, `RCPT TO`,
   `QUIT` — **`DATA` is never issued anywhere in this module**, so no mail is
   ever transmitted. Still, some providers rate-limit or blocklist probing IPs,
   and a few tarpit deliberately. Connections are paced ≥1.5s per MX host and
   capped at 2 MX hosts per address. Probing is done from the user's own IP.
9. **`detect_catch_all` can be fooled by aggressive greylisting** — a server that
   4xx's everything returns `None` (inconclusive), which correctly routes to
   review rather than guessing.
10. **`best_contact_for` returns `None` far more often than the old code returned
    an address.** That is the intended behaviour change: the old path always had
    a `careers@` to hand back, and some of those did not exist. Fewer, truer
    contacts and a review queue is the trade.
