# Speedy-Scraper qualification and recovery methodology

## Qualification contract

Lead discovery and strict validation are deliberately separate.

### All Qualified POCs

A candidate enters the durable qualified set immediately after all hard gates
pass:

1. canonical LinkedIn `/in/` URL;
2. usable person name, current designation, and current organization;
3. selected role matches the parsed current designation;
4. selected location is supported by current-profile citation structure;
5. organization allow-list matches the parsed current employer, when supplied;
6. URL and normalized name-company identity are net-new in global and selected
   uploaded exclusion scopes.

Historical experience, arbitrary snippet mentions, domain substrings, and a
location appearing only in another result cannot satisfy these gates. The
requested target is based exclusively on this qualified set.

### Strict Matches

Strict Matches are a subset of All Qualified POCs. Separate evidence searches
must satisfy every selected industry, custom company term, business model, GCC
requirement, and attributable person/event signal. Sparse evidence can exclude
a lead from strict results but never erase a genuinely hard-qualified POC.

The canonical lead and its pending strict-validation stage are committed in
one SQLite transaction. This ordering ensures a crash or CAPTCHA after hard
qualification cannot cause deduplication to skip unfinished validation.

## Query planning

Discovery contains role and location terms only:

```text
site:linkedin.com/in
(selected exact role titles)
(canonical selected locations)
```

Industry, B2B/B2C, GCC, custom terms, organizations, and signals do not enter
person discovery. Organizations are checked against the parsed employer;
company filters use cached company evidence; event signals use cached
person-and-company evidence.

The configurable default query budget is:

```text
ceil((target / 0.15 acceptance / 30 citations) × 1.4 headroom)
```

It is capped at 180. Role and location families are interleaved so selected
personas receive coverage before secondary aliases consume the bounded plan.

A generated query may have one visible alternate-title fallback. It is used
only after primary exhaustion while the qualified target is unmet. Any plan
submitted through `edited_queries` has all generated fallbacks removed; the
engine never executes a hidden fallback for an edited plan.

## Pagination and cursor durability

Each query records:

- primary/fallback mode and fallback-once state;
- current provider page and actual next-page cursor;
- page fingerprints and seen result URLs;
- normalized pending results and current candidate index;
- partial profile fragments gathered from complementary citations.

A fetched page is committed before candidates are consumed. A rejected
candidate advances the cursor only after evaluation. A qualified candidate is
atomically committed with its pending validation stage. Google follows the
actual Next link; DDGS/Brave pagination is bounded. Repeated fingerprints and
pages containing no new profile URLs stop loops.

## Evidence caching

Company evidence is cached once per normalized company per job. Person signal
evidence is cached once per normalized person-company identity per job. A
CAPTCHA on a later evidence request does not repeat evidence already committed.
Evidence is durable across process restarts and expires according to retention
policy.

## Manual Google verification

Google runs through a CDP-controlled Chromium process with a persistent profile
directory assigned to the job. The software does not hide automation signals,
solve challenges, submit CAPTCHA answers, bypass access controls, or change
identity to avoid verification.

When Google presents a challenge:

1. the exact query, page, `linkedin_only` mode, result limit, browser metadata,
   and pending candidate are saved;
2. the job moves to `waiting_verification` without advancing its cursor;
3. a headed browser is retained or reopened when a display is available;
4. a monitor only observes whether manual completion returned to the exact
   expected results URL and offset;
5. the solved page must be stable before its extracted results are cached;
6. the job is requeued and consumes those cached results without navigation.

If the browser process was lost, only the challenged request is requeued using
the same profile. A waiting job owns the single Google execution lane so no
second Google job replaces the visible challenge. Non-Google jobs use separate
workers.

## Deduplication

Canonical URL and normalized person-company keys are stored globally. This is
the default scope for all runs and schedules. Uploaded CSV files and every sheet
in uploaded workbooks create named exclusion sets that a job may additionally
reference. Duplicate results never consume the qualified target.

Canonical leads and dedup keys are not removed by retention cleanup. Detailed
events, metrics, expired evidence, browser metadata, and completed cursor state
are pruned after the configured period.

## Reliability policy

Search and public-contact workflows share:

- provider-aware token-bucket limits;
- exponential backoff with full jitter and `Retry-After` support;
- sticky-per-job HTTP(S)/SOCKS proxy selection;
- structured, credential-redacted events and JSON logs;
- persisted jobs, artifacts, and metrics.

Transport timeouts, connection failures, HTTP 408/429/5xx, temporary DDGS
errors, and browser navigation failures are retryable. Configuration and
deterministic parsing errors, ordinary 4xx responses, and CAPTCHA are not.
Proxy rotation occurs only after retryable exhaustion/rate limits, never for a
CAPTCHA.

## Export contract

Tab 1 CSV/JSON and both lead workbook sheets always contain exactly:

```text
Name, Designation, Company, Location
```

LinkedIn URLs and evidence remain internal for validation and deduplication.
Competitor-event exports retain the established CSEV columns and `CSEV` Excel
sheet name. Company, reconciliation, and public-contact outputs are persisted as
typed job artifacts before export.
