# analytics² — GA4 + BigQuery Free-Tier Dashboard

## Purpose
A single static HTML dashboard that answers, for any company on the GA4 + BigQuery free tier:
1. How close am I to each free-tier limit, day by day?
2. Where is the consumption going (so I know where to optimize)?

Pluggable: drop in a `config.yaml`, the same dashboard renders against any GCP project + GA4 property.

## Architecture
- **Refresh layer (Python)**: pulls metrics from BigQuery `INFORMATION_SCHEMA`, GA4 Admin API, and GA4 Data API; writes `web/data.json`.
- **Dashboard (static)**: `web/index.html` + Chart.js renders 13 tiles from `data.json`. No build step, no framework.
- **Refresh trigger**: GitHub Action on a daily cron commits the updated `data.json`. `python refresh/main.py` also works locally.
- **Auth**: GCP service account JSON, stored as a GitHub Secret in CI; same key path referenced by `config.yaml` locally.

## Configuration
`config.example.yaml` is committed; real `config.yaml` is gitignored.

```yaml
gcp:
  project_id: my-gcp-project
  region: EU                            # drives `region-eu`.INFORMATION_SCHEMA
  credentials_path: ./creds/sa.json     # ignored in CI; CI uses GOOGLE_APPLICATION_CREDENTIALS

ga4:
  property_id: '123456789'              # numeric, no 'properties/' prefix
  bq_dataset: analytics_123456789       # the GA4 export dataset

thresholds:
  warning: 0.60                         # yellow at 60% of cap
  critical: 0.90                        # red at 90% of cap
```

Service account roles required per deployment:
- `roles/bigquery.jobUser` (run queries)
- `roles/bigquery.metadataViewer` (read project-wide INFORMATION_SCHEMA)
- `roles/analyticsadmin.viewer` (GA4 Admin API)
- The SA email also needs `Viewer` access on the GA4 property (Property Access Management).

---

## Tiles

13 tiles in 3 sections. Each tile payload in `data.json` contains: `headline` (1–3 numbers), `series` (optional time series for the chart), `state` (`green`|`yellow`|`red`), and `source` (SQL string or API call name) for transparency.

### Section A — GA4 limits

#### A1. Events/day vs 1M BQ-export cap
**Headline**: today's intraday events, 7-day max, 30-day mean — each as absolute and % of 1M.
**Chart**: bar chart, daily events last 30 days, horizontal line at 1M, optional warning line at 750k.
**State**: red if any day in last 7 ≥ 75% of cap; yellow if 50–75%; else green.
**Source**: BigQuery
```sql
SELECT
  PARSE_DATE('%Y%m%d', REPLACE(_TABLE_SUFFIX, 'intraday_', '')) AS event_date,
  STARTS_WITH(_TABLE_SUFFIX, 'intraday_')                       AS is_intraday,
  COUNT(*)                                                      AS events
FROM `{project_id}.{bq_dataset}.events_*`
WHERE _TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY))
                        AND FORMAT_DATE('%Y%m%d', CURRENT_DATE())
   OR _TABLE_SUFFIX = CONCAT('intraday_', FORMAT_DATE('%Y%m%d', CURRENT_DATE()))
GROUP BY event_date, is_intraday
ORDER BY event_date;
```

#### A2. GA4 Data API token quotas
**Headline**: tokens consumed today / per-day cap; tokens consumed this hour / per-hour cap; concurrent requests / cap.
**Chart**: simple horizontal bars, one per quota dimension.
**State**: red if any quota ≥ 90%; yellow if ≥ 60%; else green.
**Source**: GA4 Data API. Issue one minimal `properties.runReport` call with `returnPropertyQuota: true`. The response's `propertyQuota` field contains the four quota objects (`tokensPerDay`, `tokensPerHour`, `concurrentRequests`, `serverErrorsPerProjectPerHour`). Read `consumed` and `remaining` for each.

#### A3. Collection config caps
**Headline**: custom dimensions count / 50; custom metrics count / 50; distinct event_name count last 30d / 500.
**Chart**: 3 horizontal bars.
**State**: red if any ≥ 90%; yellow if ≥ 60%; else green.
**Source**:
- `properties.customDimensions.list` → count
- `properties.customMetrics.list` → count
- BigQuery for distinct events (no clean Admin API for "configured" custom events; use observed events as proxy):
```sql
SELECT COUNT(DISTINCT event_name) AS distinct_events
FROM `{project_id}.{bq_dataset}.events_*`
WHERE _TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY))
                        AND FORMAT_DATE('%Y%m%d', CURRENT_DATE());
```

#### A4. Conversions vs 30 cap
**Headline**: conversions configured / 30.
**Chart**: single bar.
**State**: red ≥ 27, yellow ≥ 18, else green.
**Source**: GA4 Admin API `properties.conversionEvents.list`, count results.

#### A5. Audiences vs 100 cap
**Headline**: audiences configured / 100.
**Chart**: single bar.
**State**: red ≥ 90, yellow ≥ 60, else green.
**Source**: GA4 Admin API `properties.audiences.list`, count results.

---

### Section B — BigQuery limits

#### B1. Query bytes MTD vs 1 TiB
**Headline**: MTD bytes billed (TiB and % of 1 TiB); projected month-end (linear: `mtd / days_elapsed * days_in_month`); today's bytes.
**Chart**: stacked bar of daily bytes billed for the current month, horizontal line at on-pace daily allowance (1 TiB / days_in_month).
**State**: red if projected ≥ 90% or any day > 100 GiB; yellow 60–90%; else green.
**Source**:
```sql
SELECT
  DATE(creation_time)                              AS day,
  COUNT(*)                                         AS jobs,
  SUM(total_bytes_billed)                          AS bytes_billed,
  SUM(IF(cache_hit, 0, total_bytes_billed))        AS bytes_billed_no_cache
FROM `region-{region_lower}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND job_type = 'QUERY'
  AND state = 'DONE'
GROUP BY day
ORDER BY day;
```
Fall back to `JOBS_BY_USER` if the SA lacks project-wide read.

#### B2. Active storage vs 10 GiB
**Headline**: total active logical GiB across project / 10 GiB; top 3 datasets by size.
**Chart**: horizontal bars, one per dataset, ordered desc; horizontal line at 10 GiB.
**State**: green ≤ 10, yellow 10–50, red > 100.
**Source**:
```sql
SELECT
  table_schema                                              AS dataset_id,
  ROUND(SUM(active_logical_bytes) / POW(1024,3), 3)         AS active_gib
FROM `region-{region_lower}`.INFORMATION_SCHEMA.TABLE_STORAGE
WHERE project_id = '{project_id}'
GROUP BY dataset_id
ORDER BY active_gib DESC;
```

#### B3. Long-term storage GiB
**Headline**: total long-term logical GiB across project; top 3 datasets.
**Chart**: same shape as B2 but for `long_term_logical_bytes`.
**State**: informational only; no cap on free tier (long-term storage is just cheaper paid storage), but flag yellow if > 50 GiB.
**Source**: same query as B2 with `long_term_logical_bytes`.

#### B4. Cache hit rate % MTD
**Headline**: cached jobs / total jobs MTD; bytes saved by cache MTD (GiB).
**Chart**: small line — daily cache hit rate % across the month.
**State**: green ≥ 30%, yellow 10–30%, red < 10%.
**Source**:
```sql
SELECT
  DATE(creation_time)                                     AS day,
  COUNT(*)                                                AS jobs,
  SUM(IF(cache_hit, 1, 0))                                AS cached_jobs,
  SAFE_DIVIDE(SUM(IF(cache_hit, 1, 0)), COUNT(*))         AS cache_hit_rate
FROM `region-{region_lower}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND job_type = 'QUERY'
  AND state = 'DONE'
GROUP BY day
ORDER BY day;
```

---

### Section C — Optimizer (where the spend is going)

#### C1. Top 10 queries last 48h by bytes billed
**Headline**: count of jobs in last 48h; total GiB billed in last 48h.
**Table**: 10 rows: day, time, user_email, GiB billed, cache_hit, query_preview (200 chars), job_id.
**State**: red if any single row > 100 GiB; yellow > 50 GiB; else green.
**Source**:
```sql
SELECT
  DATE(creation_time)                              AS day,
  TIME(creation_time)                              AS started_utc,
  user_email,
  ROUND(total_bytes_billed / POW(1024,3), 3)       AS gib_billed,
  cache_hit,
  SUBSTR(REGEXP_REPLACE(query, r'\s+', ' '), 1, 200) AS query_preview,
  job_id
FROM `region-{region_lower}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)
  AND job_type = 'QUERY'
  AND state = 'DONE'
ORDER BY total_bytes_billed DESC
LIMIT 10;
```

#### C2. Top tables scanned MTD
**Headline**: total distinct tables touched MTD.
**Table**: top 10 rows: `project.dataset.table`, total GiB scanned MTD, job count.
**State**: informational.
**Source**:
```sql
SELECT
  CONCAT(t.project_id, '.', t.dataset_id, '.', t.table_id)  AS table_ref,
  ROUND(SUM(j.total_bytes_billed) / POW(1024,3), 3)         AS gib_billed_attributed,
  COUNT(*)                                                  AS jobs
FROM `region-{region_lower}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT j,
     UNNEST(j.referenced_tables) t
WHERE j.creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND j.job_type = 'QUERY'
  AND j.state = 'DONE'
GROUP BY table_ref
ORDER BY gib_billed_attributed DESC
LIMIT 10;
```
(Note: bytes are attributed per-table by repetition, not split — interpret as "this table appeared in queries totalling X GiB", not "this table cost X GiB". Documented in the tile.)

#### C3. Top users by bytes billed MTD
**Headline**: distinct users running queries MTD.
**Table**: top 10 rows: user_email, GiB billed MTD, job count, cache hit %.
**State**: informational.
**Source**:
```sql
SELECT
  user_email,
  ROUND(SUM(total_bytes_billed) / POW(1024,3), 3)         AS gib_billed,
  COUNT(*)                                                AS jobs,
  SAFE_DIVIDE(SUM(IF(cache_hit, 1, 0)), COUNT(*))         AS cache_hit_rate
FROM `region-{region_lower}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND job_type = 'QUERY'
  AND state = 'DONE'
GROUP BY user_email
ORDER BY gib_billed DESC
LIMIT 10;
```

#### C4. `SELECT *` on `events_*` detector
**Headline**: count of offending jobs MTD; total GiB billed by offending jobs.
**Table**: top 10 offenders: day, user_email, GiB billed, query_preview, job_id.
**State**: red if any offender > 50 GiB; yellow if any > 10 GiB; else green.
**Source**:
```sql
SELECT
  DATE(creation_time)                              AS day,
  user_email,
  ROUND(total_bytes_billed / POW(1024,3), 3)       AS gib_billed,
  SUBSTR(REGEXP_REPLACE(query, r'\s+', ' '), 1, 200) AS query_preview,
  job_id
FROM `region-{region_lower}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND job_type = 'QUERY'
  AND state = 'DONE'
  AND REGEXP_CONTAINS(query, r'(?i)select\s+\*\s+from\s+`?[^`]*\.events_')
ORDER BY total_bytes_billed DESC
LIMIT 10;
```

---

## `data.json` shape
```json
{
  "refreshed_at": "2026-05-15T06:00:00Z",
  "config": {
    "project_id": "your-gcp-project",
    "property_id": "123456789",
    "region": "EU"
  },
  "tiles": {
    "A1": { "headline": {...}, "series": [...], "state": "green", "source": "..." },
    "A2": { ... },
    "...": "...",
    "C4": { ... }
  }
}
```

---

## Refresh pipeline (`refresh/main.py`)
1. Load `config.yaml`.
2. Auth via `google.auth.default()` — picks up `GOOGLE_APPLICATION_CREDENTIALS` (CI) or falls back to `credentials_path` from config (local).
3. Run each tile function from `bq.py` and `ga4.py`. Each returns the tile payload dict. Failures in one tile must not crash the run — capture as `{"state": "error", "error": "..."}` and continue.
4. Compose into `data.json` and write to `web/data.json`.
5. Exit 0; the GitHub Action commits the diff.

---

## Dashboard (`web/`)
- `index.html`: header (project_id + last_refreshed timestamp) + 3 sections, each a CSS grid of cards.
- Each card: title, headline metric(s), mini chart (Chart.js from CDN), state pill, expandable "source" block.
- `app.js`: `fetch('./data.json')`, render each tile by id, render charts.
- `styles.css`: simple, clean, no framework. State pill colors green/yellow/red driven by `state` field.
- No bundler. Open `index.html` directly or serve with `python -m http.server`.

---

## GitHub Action (`.github/workflows/refresh.yml`)
```yaml
name: Refresh dashboard data
on:
  schedule:
    - cron: '0 6 * * *'           # 06:00 UTC daily
  workflow_dispatch:
jobs:
  refresh:
    runs-on: ubuntu-latest
    permissions:
      contents: write             # commit data.json
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r refresh/requirements.txt
      - name: Write SA key
        env:
          GCP_SA_KEY: ${{ secrets.GCP_SA_KEY }}
        run: printf '%s' "$GCP_SA_KEY" > /tmp/sa.json
      - name: Refresh
        env:
          GOOGLE_APPLICATION_CREDENTIALS: /tmp/sa.json
        run: python refresh/main.py
      - name: Commit data.json
        run: |
          git config user.name 'analytics2-bot'
          git config user.email 'analytics2-bot@users.noreply.github.com'
          git add web/data.json
          git diff --cached --quiet || git commit -m "chore: refresh dashboard data"
          git push
```

Required secrets per deployment:
- `GCP_SA_KEY` — full JSON contents of the service account key.
- `ANALYTICS2_CONFIG` — full contents of `config.yaml`, written to `./config.yaml` in a step before `Refresh` (so configs aren't committed).

---

## File structure
```
analytics2/
├── README.md                       setup + run + deploy
├── config.example.yaml
├── .gitignore                      config.yaml, creds/, __pycache__, .venv
├── refresh/
│   ├── main.py                     orchestrator
│   ├── ga4.py                      Admin + Data API tiles (A2–A5, plus A3 distinct events helper in bq.py)
│   ├── bq.py                       INFORMATION_SCHEMA tiles (A1, A3 distinct events, B1–B4, C1–C4)
│   └── requirements.txt            google-cloud-bigquery, google-analytics-admin, google-analytics-data, pyyaml
├── web/
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   └── data.json
└── .github/workflows/refresh.yml
```

---

## Done = verifiable when
1. `python refresh/main.py` against a real `config.yaml` produces a valid `web/data.json` containing payloads for all 13 tiles.
2. Opening `web/index.html` in a browser renders all 13 tiles with correct headline numbers, charts, and state pills.
3. The GitHub Action runs end-to-end on the repo with `GCP_SA_KEY` + `ANALYTICS2_CONFIG` secrets set, and commits an updated `web/data.json`.
4. Replacing `config.yaml` with a different project + property re-renders the same dashboard against that data, no code changes.

---

## Out of scope (v1)
- Email/Slack alerts → use GCP Billing Budget alerts instead.
- Auth on the dashboard itself → host privately or behind a reverse proxy if data is sensitive.
- Multiple properties/projects per deployment → one config = one dashboard. Run multiple deployments for multiple companies.
- Slot reservations / capacity pricing (assumes on-demand BigQuery).
- Dropped tile candidates for v2: data retention setting, linked BQ export health check, streaming inserts volume, cross-region egress, per-dataset active vs long-term storage split, wide-day-range scan detector.
