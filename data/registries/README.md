# Visa-sponsor registries

Drop the official government sponsor CSVs here — the classifier fuzzy-matches scraped
company names against them to tag jobs `registry_confirmed`. The files are large and
gitignored; download once and refresh ~weekly. **Everything still works without them**
(the classifier falls back to JD-keyword + Gulf-automatic signals).

| Save as | Source |
|---|---|
| `uk_sponsors.csv` | gov.uk → "Register of licensed sponsors: workers" (CSV download) |
| `us_h1b.csv` | USCIS H-1B Employer Data Hub (CSV export, latest FY) |
| `ca_lmia.csv` | open.canada.ca → positive LMIA employers list |
| `au_sponsors.csv` | Home Affairs → approved sponsor list (Skills in Demand / ex-482) |

The loader auto-detects the company-name column; if a file uses an unusual header,
add it to `REGISTRY_FILES` in `jobpilot/sponsorship/registries.py`.
