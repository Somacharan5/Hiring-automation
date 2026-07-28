# JobPilot — automated job search pipeline

End-to-end pipeline: **discover** PM roles globally → **match** against your master
profile → **tailor** resume → **apply** (email + LinkedIn) → **track**.

## Phase 1 (current): discovery + matching

```
DISCOVER (ATS APIs · JobSpy · remote boards) → HARD FILTER → CLAUDE SCORER → SHORTLIST
```

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Put your Alibaba Cloud Model Studio (DashScope) key in .env:
#   DASHSCOPE_API_KEY=sk-...
```

### Usage

```bash
# 1. One-time: build your master profile from your resume
python -m jobpilot init-profile ~/path/to/resume.pdf

# 2. Daily driver: collect → filter → score → shortlist
python -m jobpilot run

# Or step by step
python -m jobpilot collect --no-jobspy   # ATS + boards only (fast)
python -m jobpilot match
python -m jobpilot report --min-score 75
```

### Configuration

| File | What |
|---|---|
| `config/settings.yaml` | Search terms, locations, hard-filter rules, score threshold, cost caps |
| `config/companies.yaml` | Target companies polled via free ATS APIs (Greenhouse/Lever/Ashby/…) |
| `config/profile.yaml` | Your master profile (generated from resume; edit freely) |
| `jobpilot.db` | SQLite store of every job seen + match verdicts |

### Roadmap

- **Phase 2** — resume tailoring (master profile → ATS-safe per-role PDF)
- **Phase 3** — recruiter email finding (Hunter/Apollo/Snov) + Gmail outreach
- **Phase 4** — LinkedIn Easy Apply via Playwright
- **Phase 5** — monitoring dashboard
