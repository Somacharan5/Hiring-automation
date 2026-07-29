# Global PM Job Scraper — Sources, Schema & Build Spec

**Goal:** feed your hiring automation a steady stream of global Product Manager roles that offer visa sponsorship, structured enough that Claude Code can build it directly from this doc.

---

## 0. The one decision that matters most: don't scrape LinkedIn/Indeed/Naukri directly

This is almost certainly the source of your "not confident about scraping" feeling — and you're right to hesitate.

- LinkedIn and Indeed actively fingerprint and block scrapers, and both prohibit it in their ToS. Third-party scrapers (Apify, Bright Data, etc.) work *until* they don't — your IP or your LinkedIn account gets flagged, which is a bad trade for a personal job search.
- Naukri is the same story for India — you flagged this correctly.
- **The good news: you don't need them.** Every company that posts on LinkedIn *also* posts the same job on their own careers page, which is almost always powered by a public, unauthenticated ATS API. That's the legal, stable, higher-signal source — and it's actually less code to maintain, not more.

Everything below is either an official public API or a company's own published job feed. No login walls, no ToS violations, nothing that gets an account banned.

---

## 1. Source stack (in priority order)

### Tier 1 — ATS public job-board APIs (highest signal, zero legal risk)
Every major ATS publishes an unauthenticated, read-only JSON feed of a company's live job postings. No API key needed to read.

| ATS | Endpoint pattern | Notes |
|---|---|---|
| **Greenhouse** | `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true` | Most widely used by startups/scale-ups. `content=true` returns full HTML job description. |
| **Lever** | `GET https://api.lever.co/v0/postings/{site_name}?mode=json` | Supports filtering by `team`, `location`, `commitment` at the source. |
| **Ashby** | Public job board API, per-org | Best compensation-field support of any ATS — useful since you're filtering on pay. |
| **Workable** | Public account/job endpoints | Common with mid-size European companies. |
| **Recruitee / Personio** | Public JSON/XML feeds | Personio shows up a lot for German/DACH companies — relevant for your Switzerland/EU angle. |

**The one real bottleneck:** there's no master directory of `board_token`s — you have to know which company uses which ATS and what its token is. Two ways to solve this:
1. **Build a seed list** from the sponsorship-registry company names (Section 2) — search `"{company} careers greenhouse.io"` / `"{company} jobs lever.co"` to resolve tokens, cache them.
2. **Buy the aggregation layer** — services like Fantastic Jobs or JobsPipe already crawl and normalize postings across Greenhouse/Lever/Ashby/Workday/etc. into one API. Worth pricing out if you don't want to maintain token discovery yourself — this is the single most annoying part of a DIY build.

### Tier 2 — Legitimate aggregator APIs (broad country coverage)
| Source | Coverage | Why it matters for you |
|---|---|---|
| **Adzuna API** | 16–19 countries incl. UK, US, Germany, France, Australia, Canada, India, UAE-adjacent markets | Free tier (~1,000 calls/mo), real salary data, and a `top_companies` endpoint — directly useful for finding *who's hiring the most PMs* per country, which is literally what you asked for ("top hiring"). |
| **RemoteOK / Arbeitnow / Jobicy / Himalayas** | Remote-first roles, global | All have free, public, unauthenticated JSON APIs. Remote-friendly companies also tend to be sponsorship-friendly for the right role. |
| **Wellfound (AngelList)** | Startup jobs, global | No official public API — only available via third-party scraping, which carries the same ToS risk as LinkedIn. Treat as optional/lower-priority. |

### Tier 3 — PM-specific boards (lower volume, higher relevance)
- **Mind the Product Jobs** — curated, product-culture-serious companies, filterable by seniority.
- **Product Hunt Jobs**, **Products That Count / Exponent** — smaller volume but low noise.
- These generally allow polite scraping (check `robots.txt`); low request volume so low risk either way.

---

## 2. Sponsorship cross-reference — the actual unlock

This is the part that turns "PM jobs" into "PM jobs I can actually take." Match scraped company names against these **official, public, government-published** sponsor lists:

| Country | Registry | Access |
|---|---|---|
| **UK** | Register of Licensed Sponsors (Home Office) | Public CSV, updated ~daily, direct download from gov.uk — no scraping needed |
| **US** | USCIS H-1B Employer Data Hub | Public, queryable + downloadable CSV back to FY2009, by employer/NAICS/city |
| **US (deeper)** | DOL LCA Disclosure Data | Public quarterly files — gives wage level + specific job title per filing, useful for PM-specific filtering |
| **Canada** | Positive LMIA Employers List (ESDC / open.canada.ca) | Public, quarterly, by NOC code and employer |
| **Australia** | Approved sponsor list (Skills in Demand visa, ex-482) | Public via Department of Home Affairs |
| **Germany / EU** | No equivalent registry — any employer can sponsor an EU Blue Card if the role clears the salary threshold | Filter by salary instead of a company list |
| **Switzerland** | No company registry — quota-based per canton, non-EU/EFTA is genuinely hard | Filter practically by targeting multinationals in Zurich/Basel/Geneva (pharma, banking) rather than a list |
| **UAE / Gulf** | N/A — employer sponsorship is a mandatory, automatic part of any UAE employment contract | No filtering needed; if the job exists, it's sponsored |

**Practical approach:** fuzzy-match (`rapidfuzz` in Python) the `company` field from every scraped job against these lists. Where there's no registry (Germany, Switzerland), fall back to a keyword scan of the job description for phrases like *"visa sponsorship available," "relocation support," "work permit assistance"* — treat this as a weaker signal than a registry hit.

---

## 3. Output schema (what actually gets fed to Claude Code / your pipeline)

Normalize every source into one shape before it hits your automation:

```json
{
  "job_id": "greenhouse_stripe_1234",
  "source": "greenhouse | lever | ashby | adzuna | remoteok | mtp | ...",
  "source_url": "https://...",
  "company": "Stripe",
  "company_domain": "stripe.com",
  "title": "Senior Product Manager, Payments",
  "role_family": "Product Manager",
  "seniority": "senior | mid | lead | group | director",
  "country": "United Kingdom",
  "city": "London",
  "remote_type": "onsite | hybrid | remote | remote-global",
  "salary_min": 95000,
  "salary_max": 130000,
  "currency": "GBP",
  "salary_period": "year",
  "posted_date": "2026-07-20",
  "application_url": "https://...",
  "description_raw": "...",
  "description_hash": "sha256...",
  "visa_sponsorship_signal": "registry_confirmed | jd_mentioned | unknown | likely_no",
  "sponsorship_registry_match": "UK_sponsor_register | US_H1B | CA_LMIA | AU_sponsor | none",
  "ats_platform": "greenhouse",
  "scraped_at": "2026-07-29T10:00:00Z"
}
```

If your existing hiring automation already expects a specific sheet layout or DB table, keep these field *names* but map them into that structure — don't build a second schema Claude Code has to reconcile.

---

## 4. Architecture (same shape as your Xads scraper — reuse it)

This is functionally the same problem as the Xads competition/jobs scraper you already have running: scheduled pull → normalize → dedup → write to sheet → notify. Recommend reusing that exact skeleton rather than designing a new one:

```
scraper/
├── sources/
│   ├── ats_greenhouse.py      # per-source pull, resolves board tokens from a seed list
│   ├── ats_lever.py
│   ├── ats_ashby.py
│   ├── adzuna.py
│   ├── remote_apis.py         # RemoteOK / Arbeitnow / Jobicy / Himalayas
│   └── pm_boards.py           # Mind the Product / Product Hunt
├── sponsorship/
│   ├── registries.py          # loads + refreshes UK/US/CA/AU CSVs
│   └── classifier.py          # fuzzy company match + JD keyword fallback
├── normalizer.py              # maps every source into the schema above
├── dedup.py                   # hash-based, same pattern as seen_hashes.json
├── sheets_writer.py           # gspread, writes into your intake tab
└── main.py                    # orchestration, run via GitHub Actions cron
```

- **Cadence:** daily (jobs go stale faster than competition listings — your 3-day Xads cadence is too slow here).
- **Dedup key:** hash of `company + title + location`, not just `description_hash` — postings get re-published with edited copy.
- **Sponsorship registries:** cache locally, refresh weekly (UK updates ~daily but weekly is plenty for your use case).

---

## 5. Build order (MVP first)

1. **Adzuna + UK/US/CA registries** — broadest coverage, least engineering, gets you a working pipeline fastest.
2. **Greenhouse + Lever** for a seed list of ~30–50 known sponsors (pull company names straight from the UK/US registries, resolve their board tokens).
3. **RemoteOK/Arbeitnow/Jobicy/Himalayas** — cheap to add, widens remote coverage.
4. **PM-specific boards** — lowest volume, add last for signal quality.
5. Wellfound / anything requiring third-party scraping — optional, evaluate ToS risk before adding.

---

## 6. Paste this into Claude Code

```
I'm building a job-scraping pipeline that feeds an existing hiring automation. Build a Python
project with this structure:

1. sources/ats_greenhouse.py and sources/ats_lever.py — pull jobs from a list of company board
   tokens (I'll provide a seed CSV of company names + tokens), filter for titles matching
   "Product Manager" variants (APM, PM, Senior PM, Group PM, Director of Product), normalize
   into the schema below.

2. sources/adzuna.py — call the Adzuna API (I'll provide App ID/Key) across UK, US, Germany,
   Canada, Australia, India indexes, query "product manager", normalize into the same schema.

3. sources/remote_apis.py — pull from RemoteOK, Arbeitnow, Jobicy, and Himalayas public JSON
   APIs (no key needed), filter for PM roles.

4. sponsorship/registries.py — download and cache the UK Register of Licensed Sponsors CSV
   (gov.uk) and the USCIS H-1B Employer Data Hub CSV, refreshed weekly.

5. sponsorship/classifier.py — fuzzy-match each job's company name against the cached
   registries (rapidfuzz, threshold 90+), and as a fallback, regex-scan the job description
   for sponsorship-related phrases. Tag each job with visa_sponsorship_signal.

6. normalizer.py — every source maps into this exact schema: [paste JSON schema from section 3]

7. dedup.py — hash of company+title+location, persisted store, skip already-seen jobs.

8. sheets_writer.py — gspread, write new jobs into [my target sheet/tab — I'll give you the ID].

9. main.py — orchestrate all of the above, runnable via GitHub Actions cron (daily).

Start by showing me the exact API response shape for Greenhouse and Adzuna before writing the
normalizer, so we don't build on wrong field assumptions.
```

---

## What I didn't cover in depth

Country-by-country sponsorship rules for the full EU (each has its own Blue Card salary threshold), Singapore/Australia's occupation shortage lists, and a fully-priced build-vs-buy comparison of the unified ATS aggregator APIs (Fantastic Jobs, JobsPipe) would need deeper, dedicated research if you want to expand past the MVP markets above.
