# JobPilot — autonomous PM job-search pipeline

Discovers fresher/new-grad Product Manager roles, scores them against your profile,
tailors your resume per role, writes a personalised email, sends it, follows up, and
reads the replies — all visible in a live dashboard. Email-only, Neon-backed, built
to run 24/7 on an Oracle Always-Free VM.

```
08:00 daily
  scrape (ATS + boards) → hard filter (fresher / work-auth) → LLM score (0–100)
    → for each ≥60:  find address → tailor resume (exact kit format)
                     → compose email (positioning-aware) → send (guarded)
  → follow up every 3 days (max 5) → scan inbox → classify replies → 🔔 bell
```

Sending is gated by `outreach.dry_run` — nothing transmits until you set it `false`.

## Architecture

Each folder is one pipeline stage; shared infra sits at the package root.

| Path | Role |
|---|---|
| `jobpilot/db.py` | Neon (Postgres) data layer — every table, one module |
| `jobpilot/llm.py` | provider-agnostic LLM client (Gemini) + schema-validated calls |
| `jobpilot/profile.py` | the matcher profile (facts the scorer/email truth-check use) |
| `jobpilot/cli.py` · `scheduler.py` | CLI entry · the 24/7 APScheduler loop |
| **`collectors/`** | **Discover** — ATS APIs (Greenhouse/Lever/Ashby), remote boards, JobSpy |
| **`matching/`** | **Filter + score** — cheap `hard_filter` then the LLM `llm_scorer` |
| **`resume/`** | **Tailor** — `kit.py` renders the locked `Resume_creation/` kit; `tailor.py` re-emphasises content per job (no fabrication) |
| **`outreach/`** | **Email** — `find_addresses` · `composer` · `send` · `followups` · `inbox_reader` · `pipeline` (orchestrates) |
| **`dashboard/`** | **Monitor** — FastAPI + Jinja, 7 pages + notification bell |
| `Resume_creation/` | the locked resume kit (content `resume.json`, geometry `template.css`, `build.mjs`) |
| `config/` | `settings.yaml` (targeting, gates, caps), `companies.yaml`, `positioning.md` |
| `migrations/` | Postgres schema + idempotent runner |
| `deploy/` | systemd units + Oracle VM setup guide |

## Setup

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium         # resume PDF rendering
# Node + Carlito font are needed for the resume kit — see deploy/README.md

# .env (never committed):
#   LLM_PROVIDER=gemini
#   GEMINI_API_KEY=...
#   DATABASE_URL=postgresql://...neon.tech/...   (Neon connection string)
#   GMAIL_APP_PASSWORD=...                        (iamsomacharan@gmail.com)

python migrations/run.py                       # create the schema on Neon
```

## Usage

```bash
python -m jobpilot run              # collect → match → report (shortlist)
python -m jobpilot prepare          # discover address → tailor resume → compose email
python -m jobpilot send             # dry-run: lists what would send; sends only if dry_run: false
python -m jobpilot followups        # 3-day bumps
python -m jobpilot scan-replies     # inbox → dashboard + bell
python -m jobpilot agent-run        # the whole daily cycle (what the scheduler runs)
python -m jobpilot dashboard        # http://127.0.0.1:8000
python -m jobpilot.resume           # render the master resume via the kit
```

## Configuration

| File | What |
|---|---|
| `config/settings.yaml` | countries, search terms, hard-filter rules, apply gate (60), caps, follow-up cadence, schedule |
| `config/positioning.md` | your career narrative — the backbone of every personalised email |
| `config/companies.yaml` | ATS company slugs polled via free APIs |
| `Resume_creation/resume.json` | your master resume content (the tailor inherits from this) |

## Going live

Everything runs in dry-run. When ready: review prepared emails on the dashboard
**Applied** page → set `outreach.dry_run: false` (keep `first_run_draft_only: true`
for one review pass) → then `false`. Guardrails stay on: 20 emails/day, dedup,
follow-ups stop the moment a company replies. Deploy: see [deploy/README.md](deploy/README.md).
