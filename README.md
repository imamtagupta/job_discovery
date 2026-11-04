# Job Discovery

A production-minded, zero-paid-API job discovery service. It collects directly from verified public ATS endpoints, preserves collection history in SQLite or PostgreSQL, scores jobs with deterministic rules, and incrementally synchronizes Google Sheets.

## What is implemented

- Async Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Oracle Recruiting, and iCIMS collectors
- First-party India collectors for Amazon Jobs, Google Careers, Apple Jobs, and NVIDIA Careers
- Extensible collector registry and collector-level failure isolation
- Per-host concurrency, timeouts, bounded exponential retries, and HTTP 429 `Retry-After` handling
- Normalized companies, sources, jobs, observations, scores, applications, runs, and errors
- Stable logical identity `(source, company_id, external_job_id)` and deterministic sheet IDs
- Conservative cross-source duplicate linking (requisition ID, canonical URL, or exact title/location); ambiguous matches stay separate
- New/updated/seen history and missing status only after a successful source response
- Explainable configurable scoring with explicit unknown salary and work-authorization states
- Incremental Google Sheets sync with targeted views that preserves application status and notes and neutralizes formula injection
- Console notifications only for newly discovered high-scoring jobs
- Daily GitHub Actions schedule at approximately 08:00 IST
- Daily refresh and validation of public ATS sources before job collection
- ATS registry discovery from 150 curated company career-page candidates
- Greenhouse, Lever, Ashby, and modular Workday source detection and validation
- Resume-verified multi-stage matching with role-family, eligibility, freshness, and priority metrics

Custom endpoints still require a tested adapter. A failed adapter is recorded without stopping healthy sources.

## Setup

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,postgres]'
cp .env.example .env
```

Edit `config/companies.yaml` with ATS identifiers verified from each company’s careers page. Never guess board tokens or tenant IDs. The checked-in example is disabled.

```bash
job-discovery validate-config
job-discovery init-db
job-discovery discover
job-discovery rescore
job-discovery sync-sheets
python -m src.main discover-sources
python -m src.main validate-sources
python -m src.main list-sources
python -m src.main report --output discovery_report.md
python -m src.main export-jobs --output fetched-jobs.csv
python -m src.main export-jobs --output fetched-jobs.csv --since-hours 24
job-discovery prepare-applications --output application-queue.csv --since-hours 24
job-discovery faq-check "How many years of experience do you have?"
job-discovery faq-add notice_period_days "What is your notice period in days?" "30"
pytest
ruff check .
```

`discover-sources` fetches the official career-page candidates in
`config/target_companies.yaml`, extracts supported ATS URLs, and validates each identifier
against its public job endpoint. Only identifiers with both official-page evidence and a
valid provider response are enabled in `config/companies.yaml`. Uncertain, conflicting,
unsupported, and failed validations are retained in `config/pending_sources.yaml`; per-company
fetch diagnostics are written to `config/source_health.yaml`. Existing verified/manual provider
identifiers are never replaced by a conflicting discovery.

The structured candidate profile in `config/profile.yaml` is derived from the supplied resume.
Scoring performs hard eligibility checks, role-family classification, evidence-based weighted
relevance, and separate application eligibility, posting freshness, and application-priority
metrics. The Sheets sync maintains `High Priority`, `Python Backend`, `Product Companies`,
`Fintech - Quant`, `Global Opportunities`, and `Needs Review` views in addition to the complete jobs,
applications, and source-health tabs.

## Google Sheets

Create a Google Cloud service account, enable the Google Sheets API, create a workbook, and share the workbook with the service-account email. Set:

```dotenv
GOOGLE_SHEETS_SPREADSHEET_ID=the_id_from_the_sheet_url
GOOGLE_SERVICE_ACCOUNT_JSON=/absolute/path/to/service-account.json
```

The sync creates `New Matches`, `All Jobs`, `Applications`, and `Source Health`. Existing application statuses and notes are retained by stable job ID. Values are sent with `RAW` input mode.

## Scheduled persistence

GitHub-hosted runners are ephemeral. A persistent PostgreSQL `DATABASE_URL` is recommended for
durable scheduled history. When that secret is absent, the workflow uses SQLite restored through
the GitHub Actions cache so zero-credential test runs retain recent discovery state. Cache storage
can be evicted and is not a database backup, so use PostgreSQL for reliable long-term operation.
Free PostgreSQL tiers may sleep or change terms, so verify the provider’s current limits. Add these
repository secrets:

- `DATABASE_URL` (optional for testing; required for durable scheduled history)
- `GOOGLE_SHEETS_SPREADSHEET_ID` (optional)
- `GOOGLE_SERVICE_ACCOUNT_JSON` (optional, full JSON document)

The workflow does not commit databases or credentials. GitHub Actions cron is UTC and may start later during scheduler load.
Each run uploads `fetched-jobs.csv` and `discovery-report.md` as a downloadable GitHub
Actions artifact retained for 30 days. The scheduled CSV and report contain jobs first discovered
within the preceding 24 hours, preventing the same unchanged jobs from appearing every day. ATS
collection still scans complete boards so updates and missing jobs can be tracked correctly. A
first-discovered timestamp is not presented as the employer's actual posting date.
The workflow also creates `application-queue.csv` containing only new, active, eligible matches
scoring at least 80. Queue creation never submits an application; every application remains subject
to explicit review and approval.

## Application question knowledge base

Approved reusable form answers live in `config/application_faq.yaml`. Matching is deliberately
strict: known wording is answered, while unseen wording is appended to
`config/application_question_backlog.yaml` for review. Questions involving work authorization,
notice period or compensation remain `needs_review` until an explicit answer is provided. Work
authorization and sponsorship answers are conditional on whether the role is in India. The system
never guesses an answer and never treats demographic questions as required.

## Adding an ATS

Implement `Collector.collect()` under `src/job_discovery/collectors`, register the class in `collectors/registry.py`, and add mocked response tests. Adapters return `NormalizedJob`; ingestion, scoring, history, and synchronization require no changes. Respect public access rules and never bypass authentication, CAPTCHA, or anti-bot measures.

## Accuracy boundaries

`first_seen_at` means first discovered by this service, not posting time. Source timestamps are stored separately when supplied. Salary, sponsorship, and remote eligibility remain unknown unless the listing explicitly provides evidence. No application submission is performed.

