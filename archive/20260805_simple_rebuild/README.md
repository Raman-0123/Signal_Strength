# Speedy-Scraper 2

Speedy-Scraper is a persistent, single-node lead-intelligence service. One
scraping core is consumed by Streamlit, a command-line client, a versioned REST
API, and APScheduler. SQLite is the source of truth, so refreshing Streamlit or
restarting the process does not discard accepted leads or query progress.

The lead workflow preserves two distinct result sets:

- **All Qualified POCs** pass identity, current role, profile-evidenced
  location, current organization, export completeness, and deduplication. The
  target counts only this set.
- **Strict Matches** are the qualified subset that also passes every requested
  industry, signal, custom keyword, business-model, and GCC evidence gate.

Tab 1 CSV/JSON rows and both lead workbook sheets contain exactly `Name`,
`Designation`, `Company`, and `Location`.

## Architecture

```text
Streamlit app.py ─┐
CLI               ├─ gateway / application service ─ orchestrator ─ job runner
FastAPI /api/v1   ┘                                  │
                                                      ├─ lead engine
APScheduler ─ schedules table ────────────────────────┤  discovery → validation
                                                      ├─ company intelligence
                                                      ├─ competitor events
                                                      └─ public contacts
                                                            │
                    DDGS lead provider + workflow providers ─┤
                    retry + rate limit + sticky proxy pool ──┤
                    provider block cooldown checkpoints ─────┤
                                                            ▼
                                                   SQLite (WAL mode)
```

`speedy_scraper/` contains no Streamlit imports. `app.py` is a gateway client;
its compatibility bridge imports surviving pre-upgrade session leads into a
synthetic persisted job once.

## Installation

Python 3.11–3.13 is supported.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m playwright install chromium
speedy-scraper db migrate
```

For development:

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
ruff check .
mypy speedy_scraper
```

Normal tests use fake search providers and never access live search services.

## Starting the application

Run the API, runner, scheduler, and Streamlit together:

```bash
speedy-scraper serve
```

Or run components separately:

```bash
speedy-scraper api
speedy-scraper ui
```

The API defaults to `127.0.0.1:8000`; Streamlit defaults to
`127.0.0.1:8501`. Use one API process. Multiple Uvicorn workers would each
start an in-process scheduler.

## Configuration

Precedence is:

```text
built-in dataclass defaults
  → config/default.yaml
  → --settings user.yaml
  → SPEEDY_SCRAPER_* environment variables
  → workflow request values
```

Selectors and query templates are versioned in `config/catalog.yaml`. Runtime
delays, scoring, retry policy, query-budget assumptions, persistence, and
scheduler defaults are in `config/default.yaml`.

Example non-secret user settings:

```yaml
storage:
  retention_days: 90
retry:
  max_attempts: 3
rate_limits:
  ddgs:
    requests_per_minute: 20
    minimum_interval_seconds: 2.0
scheduler:
  timezone: Asia/Kolkata
  max_workers: 2
```

Secrets and proxy URLs are rejected in YAML. Supply sensitive/runtime paths
through the environment:

```bash
export SPEEDY_SCRAPER_DB_PATH=/srv/speedy/data.db
export SPEEDY_SCRAPER_DATA_DIR=/srv/speedy
export SPEEDY_SCRAPER_CHROME_PATH=/path/to/chrome
export SPEEDY_SCRAPER_API_KEY='replace-with-a-long-random-value'
export SPEEDY_SCRAPER_PROXY_URLS='http://user:pass@proxy:8080,socks5://proxy2:1080'
export SPEEDY_SCRAPER_LOG_LEVEL=INFO
```

Any nested non-secret field can use double underscores, for example:

```bash
export SPEEDY_SCRAPER_RETRY__MAX_ATTEMPTS=5
```

Validate before starting:

```bash
speedy-scraper --settings production.yaml config validate
```

## CLI

Create `lead-job.yaml`:

```yaml
workflow: lead
location_ids: ["6"]
role_ids: ["22"]
industry_ids: ["1"]
signal_ids: ["1"]
target_count: 25
discovery_provider: ddgs
validation_provider: ddgs
business_model: B2B only
browser_mode: interactive
```

Run locally against SQLite:

```bash
speedy-scraper scrape run --job-config lead-job.yaml
speedy-scraper jobs list
speedy-scraper jobs show JOB_ID
speedy-scraper jobs pause JOB_ID
speedy-scraper jobs resume JOB_ID
speedy-scraper jobs cancel JOB_ID
speedy-scraper jobs retry JOB_ID
speedy-scraper export JOB_ID --format xlsx --kind qualified
```

To extract an event speaker roster and enrich missing personal LinkedIn
profiles from public search results, create `event-speakers.yaml`:

```yaml
workflow: event_speakers
source_url: https://globalfintechfest.com/speakers
enrich_missing: true
search_provider: ddgs
```

Run and export it through the same persistent job interface:

```bash
speedy-scraper scrape run --job-config event-speakers.yaml
speedy-scraper export JOB_ID --format csv --output event-speakers.csv
speedy-scraper export JOB_ID --format xlsx --output event-speakers.xlsx
```

Event-speaker rows keep the source name, designation, company, and country,
plus the canonical LinkedIn ID/URL, match status, confidence, and evidence.
Only `ddgs` and `brave` are supported for this workflow. Ambiguous and missing
profiles remain in the export with blank LinkedIn fields.

Use a remote service by adding `--api-url http://127.0.0.1:8000`. The remote
API key is read from `SPEEDY_SCRAPER_API_KEY`, not a command-line argument.
To start a background job through the persistent API worker:

```bash
speedy-scraper serve
speedy-scraper --api-url http://127.0.0.1:8000 scrape run --job-config lead-job.yaml --no-wait
```

CLI exit codes are `0` for success, `2` for invalid configuration/request,
`3` for a failed job, and `4` when provider recovery is required.

## REST API

Interactive documentation is served at `/docs`. Core routes include:

```text
POST /api/v1/jobs
GET  /api/v1/jobs
GET  /api/v1/jobs/{id}
POST /api/v1/jobs/{id}/pause|resume|cancel|retry
POST /api/v1/jobs/{id}/verification/check
GET  /api/v1/jobs/{id}/results|events|metrics
GET  /api/v1/jobs/{id}/exports/{csv|xlsx|json}

GET|POST          /api/v1/schedules
PATCH|DELETE      /api/v1/schedules/{id}
POST              /api/v1/schedules/{id}/run-now
GET|POST           /api/v1/dedup-imports
DELETE             /api/v1/dedup-imports/{id}
GET                /api/v1/catalog
POST               /api/v1/plans/preview
GET                /api/v1/metrics
GET                /health/live
GET                /health/ready
GET                /metrics
```

Job creation returns `202 Accepted`. Send `Idempotency-Key` to make retries
return the original job. Errors consistently use:

```json
{"code": "invalid_request", "message": "...", "details": null}
```

The server binds to loopback by default. `speedy-scraper api` refuses a
non-loopback bind without `SPEEDY_SCRAPER_API_KEY`; authenticated requests use
`X-API-Key`. CORS is disabled unless explicitly added by a deployment owner.

## Scheduling

The service uses stable APScheduler 3.x and keeps its own `schedules` table as
the authoritative store. Enabled definitions are reconstructed on startup;
Python callables are never pickled into the database. Supported triggers are:

- cron, with either a five-field `expression` or named CronTrigger fields;
- interval, using fields such as `minutes`, `hours`, or `days`;
- one-time `date`, using an ISO `run_date`.

IANA time zones are supported and default to `Asia/Kolkata`. Jobs coalesce to
the latest missed run, use a 15-minute misfire grace period, and do not overlap
another active run from the same schedule. See the
[APScheduler project](https://pypi.org/project/APScheduler/).

```yaml
name: Weekday talent scan
workflow: lead
trigger:
  type: cron
  expression: "0 8 * * 1-5"
timezone: Asia/Kolkata
enabled: true
request:
  location_ids: ["6"]
  role_ids: ["22"]
  target_count: 20
  discovery_provider: ddgs
  validation_provider: ddgs
```

```bash
speedy-scraper schedule add --schedule-config schedule.yaml
speedy-scraper schedule list
speedy-scraper schedule disable SCHEDULE_ID
speedy-scraper schedule enable SCHEDULE_ID
speedy-scraper schedule run-now SCHEDULE_ID
speedy-scraper schedule delete SCHEDULE_ID
```

## Provider recovery

- New lead jobs use DuckDuckGo for both discovery and validation.
- If DuckDuckGo appears rate-limited, blocked, challenged, or repeatedly
  refused, the exact query, page, result mode, and pending validation candidate
  are checkpointed before the job enters `waiting_verification`.
- The application does not solve or bypass challenges. Wait for the cooldown or
  change network/proxy, then run `verification check`, `jobs resume`, or
  `jobs retry` to requeue the same saved request.
- Legacy persisted Google jobs can still use the existing manual browser
  verification path, but Google is not exposed for new lead jobs.

Use:

```bash
speedy-scraper verification status JOB_ID
speedy-scraper verification check JOB_ID
speedy-scraper verification cancel JOB_ID
```

Scheduled completion is guaranteed only for providers that do not require
manual browser work.

## Retry, rate limits, and proxies

Provider requests use a shared token bucket with both requests-per-minute and
minimum-interval controls. Retry defaults are three attempts, a 1-second base,
×2 exponential growth, full jitter, and a 30-second ceiling. Numeric
`Retry-After` is honored.

Timeouts, connection failures, HTTP 408/429/5xx, temporary DDGS errors, and
browser navigation failures are retried. DDGS block-like failures pause the job
with a cooldown checkpoint. Invalid configuration, deterministic parsing
failures, and ordinary 4xx responses are not retried.

Proxy assignment is sticky per job. A proxy rotates only after retryable
transport/rate-limit failures exhaust retries and the configured failure
threshold is reached. Failed proxies enter cooldown. Credentials are redacted
from logs and removed before any browser checkpoint is persisted.

## Persistence, backup, and retention

SQLite enables WAL, foreign keys, a busy timeout, UTC timestamps, and atomic
`BEGIN IMMEDIATE` job claims. A qualified lead and its pending-validation
checkpoint commit in one transaction. Pages, evidence, and cursor advances are
also persisted incrementally.

Global lead deduplication is the default, so each scheduled run produces
net-new profiles. CSV and every Excel sheet uploaded as an exclusion source
remain separately cataloged. `paused` and `cancelled` jobs both retain results;
only cancellation is terminal.

For an online backup:

```bash
sqlite3 data/speedy_scraper.db ".backup '/safe/path/speedy-scraper-backup.db'"
```

Keep the database and `data/browser_profiles/` on local durable storage. Do not
copy only the `-wal` file. On startup, expired `running` leases are requeued at
their saved checkpoints. Detailed events, metrics, expired evidence, and old
completed checkpoints are pruned after 90 days by default; canonical leads and
global dedup keys are retained.

## Observability

- structured JSON logs: `data/logs/speedy-scraper.jsonl`;
- per-job ordered events: `/api/v1/jobs/{id}/events`;
- per-job metrics: `/api/v1/jobs/{id}/metrics`;
- aggregate JSON metrics: `/api/v1/metrics`;
- Prometheus text exposition: `/metrics`;
- liveness and SQLite readiness probes under `/health`.

Metrics include discovery/validation queries, provider attempts, retries,
provider block events, candidates processed, qualified/strict matches,
duplicate rejections, evidence-cache hits, repeated pages, and filter rejection
reasons.

## Legacy archive

Superseded Streamlit/browser/debug files are preserved under `legacy/` because
this workspace has no Git history. They are excluded from packaging and
runtime imports. The Google challenge HTML used by tests is under
`tests/fixtures/`.
