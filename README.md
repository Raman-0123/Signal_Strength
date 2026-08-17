# Speedy Scraper [Signal Strength] 

This is a checkpointed lead-research system focused on verified LinkedIn POCs
from public Google result pages. The main app uses one visible Google Chrome
session and no fallback search engine. No API key is used. The app never logs in
to LinkedIn or fetches LinkedIn profile pages directly.

## What remains active

- Streamlit UI: `streamlit run app.py`
- CLI: `speedy-scraper run --preset bengaluru_fintech_tech_cx`
- Default source: a persistent, visible Google Chrome browser
- Exports: CSV/XLSX in `exports/`

The previous large implementation was archived under
`archive/20260805_simple_rebuild/`.

## Install

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m playwright install chromium
```

No credentials or search API setup are required. Queries are sent to Google one
at a time through a persistent, visible Chrome profile, with a 6–9 second request
interval and real result-page offsets. CAPTCHA images load normally and the worker
waits up to 60 seconds for you to solve a verification page before saving the
failed page for an exact retry.

Python 3.11–3.13 is required. Verify the active environment with
`python --version` before starting the app.

## Run the app

```bash
streamlit run app.py
```

The main page keeps the search brief intentionally small:

- target roles/personas;
- target locations;
- optional target companies and a strict current-company match;
- optional industry evidence terms;
- a lead target up to 500 and one to five Google pages per search;
- prior CSV/XLSX exports or a public Google Sheet tab for URL deduplication.

The main harvest runs outside the Streamlit request in a background process.
It checkpoints after every public search and every candidate verification, so
closing the browser does not stop it. Paused, failed, or stale workers can be
relaunched from the operations ledger without repeating completed work. Job
state and exports live under `exports/jobs/lead_harvest/`.

The Streamlit sidebar has a separate generic URL-ingestion page:

- **URL People LinkedIn Finder**
- Local route: `http://localhost:8501/URL_LinkedIn_Finder`
- Paste any public event/speaker/team/people-list URL
- Click **Start URL People Job**
- Use **Stop after current person** and **Resume from checkpoint** at any time

It auto-detects embedded `speakers`/`people`/`participants` JSON,
JSON-LD `Person` records, and visible personal LinkedIn profile links. It
exports `Name`, `Designation`, `Company`, `Country`, `LinkedIn ID`,
`LinkedIn URL`, `Match Status`, `Confidence`, `Match Evidence`, and `Source URL`.
The extracted list is saved immediately, then enrichment is checkpointed after
each person under `exports/jobs/url_people/`. Closing the page does not stop the
background worker.

The sidebar also includes a separate **Company + Designation POC Finder** at
`http://localhost:8501/Company_Designation_POC_Finder`. Enter company names and
designations one per line. It searches each exact company-role pair, exports only
evidence-backed personal LinkedIn profiles, and supports the same Stop/Resume
workflow. Its checkpoints live under `exports/jobs/company_pocs/`.

Company/role matching is candidate-scoped: a company or designation mentioned
only in a related-result snippet does not count. Expanded roles preserve their
function and seniority (for example, `VP Customer Success` does not match a CX
manager, and `Chief Data Officer` does not match `Chief Digital Officer`).

Google can present a verification page. It is reported as a recoverable Google
error instead of an empty successful search; the app never changes to Bing or
DuckDuckGo. The page cursor is saved after every search unit for exact retry.

## Run from CLI

```bash
speedy-scraper run \
  --preset bengaluru_fintech_tech_cx \
  --target 150 \
  --output exports/bengaluru_fintech_leads.xlsx
```

To use a YAML job:

```yaml
preset: bengaluru_fintech_tech_cx
target_count: 150
business_model: Any
sources: [google_browser]
google_manual_challenge_seconds: 60
require_target_company: true
minimum_confidence: 85
minimum_sources: 1
max_queries: 90
max_results_per_query: 30
max_pages_per_query: 3
source_failure_limit: 3
candidate_pool_multiplier: 4
```

```bash
speedy-scraper run --config job.yaml --output exports/leads.xlsx
```

## Validation

A row counts toward the target only when the scraper has:

- a clean personal LinkedIn `/in/` URL;
- a clean person name;
- a non-junk designation and company;
- selected role evidence;
- selected target-company evidence when hard company filtering is enabled;
- selected location evidence;
- selected industry/company evidence.
- the selected source-count and confidence thresholds.

Rejected candidates carry a reason code. The audit workbook contains verified
leads, rejected candidates, metrics, the exact query plan, the committed filter
contract, and any source errors.
