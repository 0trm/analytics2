"""BigQuery tile implementations. All read from INFORMATION_SCHEMA or GA4 export tables.

Per-tile payload contract (returned by every tile fn, in this module and in ga4.py):
    {
      "headline": dict[str, str|number],   # 1-3 KPI lines; first key is the big headline
      "series":   list[dict] | None,       # [{"x": str, "y": number}, ...] for the chart
      "state":    "green" | "yellow" | "red",
      "source":   str                      # SQL string or API call name (transparency)
    }

Failures raise; the orchestrator captures them as {"state": "error", ...}.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from functools import lru_cache

from google.cloud import bigquery
from google.oauth2 import service_account

from .util import state_from_pct

ONE_TIB = 1024 ** 4
ONE_GIB = 1024 ** 3
B1_DAILY_SPIKE_GIB = 100  # any single day above this forces red regardless of projection
B2_FREE_GIB = 10          # active storage free-tier
B2_RED_GIB = 100          # spec: > 100 GiB total active is red
B4_GREEN_RATE = 0.30      # spec: cache hit rate >= 30% MTD is green
B4_YELLOW_RATE = 0.10     # spec: 10–30% yellow, < 10% red
A1_DAILY_CAP = 1_000_000  # GA4 Standard free-tier daily event cap → BQ export pauses on overshoot
C1_YELLOW_GIB = 50        # any single 48h query > this GiB → yellow
C1_RED_GIB = 100          # any single 48h query > this GiB → red
C4_YELLOW_GIB = 10        # SELECT * on events_* is a known anti-pattern → stricter
C4_RED_GIB = 50           #   (a single SELECT * over events_YYYYMMDD spanning weeks can be huge)


# ---- helpers (used by every BQ tile) ----------------------------------------

@lru_cache(maxsize=8)
def _bigquery_client(project_id: str, credentials_path: str | None) -> bigquery.Client:
    # If config.gcp.credentials_path points at an existing key file, use it
    # explicitly (beats whatever stale GOOGLE_APPLICATION_CREDENTIALS the user's
    # shell may have set). Otherwise fall back to ADC, which the GH Action
    # populates via GOOGLE_APPLICATION_CREDENTIALS=/tmp/sa.json.
    if credentials_path:
        path = os.path.abspath(credentials_path)
        if os.path.exists(path):
            creds = service_account.Credentials.from_service_account_file(path)
            return bigquery.Client(project=project_id, credentials=creds)
    return bigquery.Client(project=project_id)


def _client(config: dict) -> bigquery.Client:
    gcp = config["gcp"]
    return _bigquery_client(gcp["project_id"], gcp.get("credentials_path"))


def _region_path(region: str) -> str:
    return f"region-{region.lower()}"


def _thresholds(config: dict) -> tuple[float, float]:
    t = config.get("thresholds") or {}
    return float(t.get("warning", 0.6)), float(t.get("critical", 0.9))


def _days_in_month(d: date) -> int:
    nxt = date(d.year + (d.month // 12), (d.month % 12) + 1, 1)
    return (nxt - date(d.year, d.month, 1)).days


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d = date.fromordinal(d.toordinal() + 1)


# ---- helpers exposed for use from other modules -----------------------------

def count_distinct_event_names_30d(config: dict) -> int:
    """Distinct event_name count in the GA4 export over the last 30 days.

    Used by ga4.tile_a3_collection_config to fill in the 500-event-name cap row.
    Scans only the event_name column over events_*; typically ~tens of MiB
    per refresh — small enough not to matter against the 1 TiB free tier.
    """
    project_id = config["gcp"]["project_id"]
    dataset = config["ga4"]["bq_dataset"]
    sql = f"""
SELECT COUNT(DISTINCT event_name) AS n
FROM `{project_id}.{dataset}.events_*`
WHERE _TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY))
                        AND FORMAT_DATE('%Y%m%d', CURRENT_DATE())
""".strip()
    row = next(iter(_client(config).query(sql).result()))
    return int(row.n or 0)


# ---- tiles ------------------------------------------------------------------

def tile_b1_query_bytes_mtd(config: dict) -> dict:
    """B1: BQ query bytes MTD vs 1 TiB free tier. Daily series + linear projection."""
    region = config["gcp"]["region"]
    warning, critical = _thresholds(config)

    sql = f"""
SELECT
  DATE(creation_time)                       AS day,
  COUNT(*)                                  AS jobs,
  SUM(total_bytes_billed)                   AS bytes_billed,
  SUM(IF(cache_hit, 0, total_bytes_billed)) AS bytes_billed_no_cache
FROM `{_region_path(region)}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND job_type = 'QUERY'
  AND state = 'DONE'
GROUP BY day
ORDER BY day
""".strip()

    rows = list(_client(config).query(sql).result())

    today = date.today()
    by_day = {r.day: int(r.bytes_billed or 0) for r in rows}
    mtd_bytes = sum(by_day.values())
    today_bytes = by_day.get(today, 0)

    days_elapsed = today.day
    days_in_month = _days_in_month(today)
    projected_bytes = (
        int(mtd_bytes / days_elapsed * days_in_month) if days_elapsed > 0 else mtd_bytes
    )

    pct_mtd = mtd_bytes / ONE_TIB
    pct_projected = projected_bytes / ONE_TIB

    state = state_from_pct(pct_projected, warning=warning, critical=critical)
    if any(b > B1_DAILY_SPIKE_GIB * ONE_GIB for b in by_day.values()):
        state = "red"

    series = [
        {"x": d.isoformat(), "y": round(by_day.get(d, 0) / ONE_GIB, 2)}
        for d in _date_range(today.replace(day=1), today)
    ]

    return {
        "headline": {
            "of 1 TiB MTD": f"{pct_mtd * 100:.1f}%",
            "MTD billed": f"{mtd_bytes / ONE_TIB:.3f} TiB",
            "projected": f"{projected_bytes / ONE_TIB:.2f} TiB ({pct_projected * 100:.0f}%)",
            "today": f"{today_bytes / ONE_GIB:.2f} GiB",
        },
        "series": series,
        "state": state,
        "source": " ".join(sql.split()),
    }


# ---- stubs (still NotImplementedError until their PRs land) -----------------

def tile_a1_events_per_day(config: dict) -> dict:
    """A1: GA4 events/day vs 1M BQ-export cap (Standard GA4). Last 30 days + intraday today.

    Implementation note: spec.md §A1's SQL does COUNT(*) over events_*. That's accurate
    but bills bytes per refresh — over a 30-day window of GA4 events tables it can be
    several GiB, so a daily refresh would burn 100+ GiB/month against the 1 TiB free
    tier just for monitoring. Instead we read row_count from `<dataset>.__TABLES__`,
    which is metadata only and free. (We tried INFORMATION_SCHEMA.TABLE_STORAGE first
    but its collection lags for projects that just opted in — fresh tables can take
    days to appear. __TABLES__ is current-state.) Requires bigquery.dataViewer on the
    dataset (or project).
    """
    project_id = config["gcp"]["project_id"]
    dataset = config["ga4"]["bq_dataset"]

    sql = f"""
SELECT table_id, row_count
FROM `{project_id}.{dataset}.__TABLES__`
WHERE STARTS_WITH(table_id, 'events_')
ORDER BY table_id DESC
""".strip()

    rows = list(_client(config).query(sql).result())

    today = date.today()
    cutoff = today - timedelta(days=29)
    by_day: dict[date, int] = {}
    intraday_today = 0

    for r in rows:
        tid = r.table_id
        is_intraday = tid.startswith("events_intraday_")
        date_str = tid[len("events_intraday_"):] if is_intraday else tid[len("events_"):]
        try:
            d = date(int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8]))
        except (ValueError, IndexError):
            continue
        if d < cutoff:
            continue
        n = int(r.row_count or 0)
        if is_intraday and d == today:
            intraday_today = n
        else:
            by_day[d] = by_day.get(d, 0) + n

    # If both events_YYYYMMDD (finalized) and events_intraday_YYYYMMDD exist for today,
    # prefer whichever is larger (finalized typically wins once it lands).
    if intraday_today:
        by_day[today] = max(by_day.get(today, 0), intraday_today)

    today_events = by_day.get(today, 0)
    last_7_vals = [by_day.get(today - timedelta(days=i), 0) for i in range(7)]
    last_30_vals = [by_day.get(today - timedelta(days=i), 0) for i in range(30)]
    week_max = max(last_7_vals) if last_7_vals else 0
    nonzero_30 = [v for v in last_30_vals if v > 0]
    month_mean = int(sum(nonzero_30) / len(nonzero_30)) if nonzero_30 else 0

    pct_week_max = week_max / A1_DAILY_CAP
    if pct_week_max >= 0.75:
        state = "red"
    elif pct_week_max >= 0.50:
        state = "yellow"
    else:
        state = "green"

    series = [
        {"x": (today - timedelta(days=i)).isoformat(), "y": int(by_day.get(today - timedelta(days=i), 0))}
        for i in range(29, -1, -1)
    ]

    return {
        "headline": {
            "of 1M cap": f"{(week_max / A1_DAILY_CAP) * 100:.0f}%",
            "7d max": f"{week_max:,} events",
            "30d mean": f"{month_mean:,}",
        },
        "series": series,
        "state": state,
        "source": " ".join(sql.split()),
    }


def tile_b2_active_storage(config: dict) -> dict:
    """B2: BQ active logical storage vs 10 GiB free tier. Per-dataset breakdown."""
    project_id = config["gcp"]["project_id"]
    region = config["gcp"]["region"]

    sql = f"""
SELECT
  table_schema                                      AS dataset_id,
  ROUND(SUM(active_logical_bytes) / POW(1024,3), 3) AS active_gib
FROM `{_region_path(region)}`.INFORMATION_SCHEMA.TABLE_STORAGE
WHERE project_id = '{project_id}'
GROUP BY dataset_id
ORDER BY active_gib DESC
""".strip()

    rows = list(_client(config).query(sql).result())
    by_dataset = [(r.dataset_id, float(r.active_gib or 0)) for r in rows]
    total_gib = sum(g for _, g in by_dataset)

    # Hard thresholds per spec.md §B2: green ≤ 10, yellow 10–100, red > 100
    if total_gib > B2_RED_GIB:
        state = "red"
    elif total_gib > B2_FREE_GIB:
        state = "yellow"
    else:
        state = "green"

    series = [{"x": d, "y": round(g, 3)} for d, g in by_dataset]

    return {
        "headline": {
            "of 10 GiB free": f"{total_gib / B2_FREE_GIB * 100:.0f}%",
            "active": f"{total_gib:.2f} GiB",
            "datasets": str(len(by_dataset)),
        },
        "series": series,
        "indexAxis": "y",
        "state": state,
        "source": " ".join(sql.split()),
    }


def tile_b4_cache_hit_rate(config: dict) -> dict:
    """B4: BQ cache hit rate % MTD. Daily line chart.

    State is inverted vs other tiles: high cache hit rate is GOOD
    (green ≥ 30%, yellow 10–30%, red < 10%).

    Note: there's no honest 'bytes saved by cache' metric here. BQ reports
    total_bytes_processed = 0 for cache-hit jobs (not a counterfactual
    estimate), so any sum like SUM(IF(cache_hit, total_bytes_processed, 0))
    returns 0 even when the cache is genuinely doing work. Headline shows
    the rate + raw counts; that's what's actionable.
    """
    region = config["gcp"]["region"]

    sql = f"""
SELECT
  DATE(creation_time)      AS day,
  COUNT(*)                 AS jobs,
  SUM(IF(cache_hit, 1, 0)) AS cached_jobs
FROM `{_region_path(region)}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND job_type = 'QUERY'
  AND state = 'DONE'
GROUP BY day
ORDER BY day
""".strip()

    rows = list(_client(config).query(sql).result())

    total_jobs = sum(int(r.jobs or 0) for r in rows)
    total_cached = sum(int(r.cached_jobs or 0) for r in rows)
    rate = (total_cached / total_jobs) if total_jobs > 0 else 0.0

    if rate >= B4_GREEN_RATE:
        state = "green"
    elif rate >= B4_YELLOW_RATE:
        state = "yellow"
    else:
        state = "red"

    series = [
        {
            "x": r.day.isoformat(),
            "y": round((int(r.cached_jobs or 0) / int(r.jobs)) * 100, 1) if r.jobs else 0,
        }
        for r in rows
    ]

    return {
        "headline": {
            "cache hit MTD": f"{rate * 100:.1f}%",
            "cached jobs": f"{total_cached:,} of {total_jobs:,}",
        },
        "series": series,
        "chartType": "line",
        "state": state,
        "source": " ".join(sql.split()),
    }


def tile_c1_top_queries_48h(config: dict) -> dict:
    """C1: Top 10 queries last 48h by bytes billed. Table render (not chart)."""
    region = config["gcp"]["region"]

    sql = f"""
SELECT
  DATE(creation_time)                                AS day,
  FORMAT_TIME('%H:%M:%S', TIME(creation_time))       AS started_utc,
  user_email,
  ROUND(total_bytes_billed / POW(1024,3), 3)         AS gib_billed,
  cache_hit,
  SUBSTR(REGEXP_REPLACE(query, r'\\s+', ' '), 1, 200) AS query_preview,
  job_id
FROM `{_region_path(region)}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)
  AND job_type = 'QUERY'
  AND state = 'DONE'
ORDER BY total_bytes_billed DESC
LIMIT 10
""".strip()

    rows = list(_client(config).query(sql).result())
    table_rows = [
        {
            "day": r.day.isoformat(),
            "time": r.started_utc,
            "user": r.user_email or "—",
            "gib": float(r.gib_billed or 0),
            "cache": "✓" if r.cache_hit else "",
            "query": r.query_preview or "",
            "job": r.job_id,
        }
        for r in rows
    ]

    biggest = max((row["gib"] for row in table_rows), default=0.0)
    total_gib = sum(row["gib"] for row in table_rows)

    if biggest > C1_RED_GIB:
        state = "red"
    elif biggest > C1_YELLOW_GIB:
        state = "yellow"
    else:
        state = "green"

    return {
        "headline": {
            "biggest (48h)": f"{biggest:.2f} GiB",
            "top-10 total": f"{total_gib:.2f} GiB",
            "queries": f"top {len(table_rows)} of last 48h",
        },
        "table": {
            "columns": [
                {"key": "day", "label": "day"},
                {"key": "time", "label": "time UTC"},
                {"key": "user", "label": "user"},
                {"key": "gib", "label": "GiB", "fmt": "gib"},
                {"key": "cache", "label": "cache"},
                {"key": "query", "label": "query"},
                {"key": "job", "label": "job id", "fmt": "mono-short"},
            ],
            "rows": table_rows,
        },
        "state": state,
        "source": " ".join(sql.split()),
    }


def tile_c2_top_tables_mtd(config: dict) -> dict:
    """C2: Top 10 tables scanned MTD by attributed bytes billed.

    Caveat (per spec.md §C2): bytes are attributed per-table by repetition, not
    split. A 10 GiB query that touches 3 tables shows 10 GiB attributed to each
    of those 3 tables. Read this tile as "this table appeared in queries totalling
    X GiB", not "this table cost X GiB" — useful for finding which tables are
    most-scanned, not for blame allocation.

    State is informational per spec (always green).
    """
    region = config["gcp"]["region"]

    list_sql = f"""
SELECT
  CONCAT(t.project_id, '.', t.dataset_id, '.', t.table_id) AS table_ref,
  ROUND(SUM(j.total_bytes_billed) / POW(1024,3), 3)        AS gib_billed_attributed,
  COUNT(*)                                                 AS jobs
FROM `{_region_path(region)}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT j,
  UNNEST(j.referenced_tables) t
WHERE j.creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND j.job_type = 'QUERY'
  AND j.state = 'DONE'
GROUP BY table_ref
ORDER BY gib_billed_attributed DESC
LIMIT 10
""".strip()

    # Separate query for the distinct-tables headline number (cheap, one row).
    count_sql = f"""
SELECT COUNT(DISTINCT CONCAT(t.project_id, '.', t.dataset_id, '.', t.table_id)) AS n
FROM `{_region_path(region)}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT j,
  UNNEST(j.referenced_tables) t
WHERE j.creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND j.job_type = 'QUERY'
  AND j.state = 'DONE'
""".strip()

    client = _client(config)
    rows = list(client.query(list_sql).result())
    n_distinct = next(iter(client.query(count_sql).result())).n

    table_rows = [
        {
            "table": r.table_ref,
            "gib": float(r.gib_billed_attributed or 0),
            "jobs": int(r.jobs or 0),
        }
        for r in rows
    ]
    top_gib = table_rows[0]["gib"] if table_rows else 0.0

    return {
        "headline": {
            "distinct tables MTD": str(n_distinct),
            "top table": f"{top_gib:.2f} GiB attributed",
            "note": "attribution by repetition, not split",
        },
        "table": {
            "columns": [
                {"key": "table", "label": "table"},
                {"key": "gib", "label": "GiB attrib.", "fmt": "gib"},
                {"key": "jobs", "label": "jobs"},
            ],
            "rows": table_rows,
        },
        "state": "green",
        "source": " ".join(list_sql.split()),
    }


def tile_c3_top_users_mtd(config: dict) -> dict:
    """C3: Top 10 users by bytes billed MTD. Informational, always green."""
    region = config["gcp"]["region"]

    sql = f"""
SELECT
  user_email,
  ROUND(SUM(total_bytes_billed) / POW(1024,3), 3)         AS gib_billed,
  COUNT(*)                                                AS jobs,
  SAFE_DIVIDE(SUM(IF(cache_hit, 1, 0)), COUNT(*))         AS cache_hit_rate
FROM `{_region_path(region)}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND job_type = 'QUERY'
  AND state = 'DONE'
GROUP BY user_email
ORDER BY gib_billed DESC
LIMIT 10
""".strip()

    rows = list(_client(config).query(sql).result())
    table_rows = [
        {
            "user": r.user_email or "—",
            "gib": float(r.gib_billed or 0),
            "jobs": int(r.jobs or 0),
            "cache": f"{(float(r.cache_hit_rate or 0)) * 100:.0f}%",
        }
        for r in rows
    ]
    total_gib = sum(row["gib"] for row in table_rows)
    top_user_gib = table_rows[0]["gib"] if table_rows else 0.0

    return {
        "headline": {
            "users MTD": str(len(table_rows)),
            "top user": f"{top_user_gib:.2f} GiB",
            "top-10 total": f"{total_gib:.2f} GiB",
        },
        "table": {
            "columns": [
                {"key": "user", "label": "user"},
                {"key": "gib", "label": "GiB MTD", "fmt": "gib"},
                {"key": "jobs", "label": "jobs"},
                {"key": "cache", "label": "cache hit"},
            ],
            "rows": table_rows,
        },
        "state": "green",
        "source": " ".join(sql.split()),
    }


def tile_c4_select_star_events(config: dict) -> dict:
    """C4: Detect `SELECT * FROM events_*` offenders MTD. The single biggest
    free-tier killer on GA4 + BigQuery — wildcards a full row from sharded
    events tables and bills the union of column widths × rows.

    State per spec.md §C4 (stricter than C1's general top-queries thresholds
    since this is a known anti-pattern):
      - red if any offender > 50 GiB
      - yellow if any offender > 10 GiB
      - else green
    """
    region = config["gcp"]["region"]
    region_path = _region_path(region)
    regex = r"(?i)select\s+\*\s+from\s+`?[^`]*\.events_"

    summary_sql = f"""
SELECT COUNT(*) AS n, SUM(total_bytes_billed) AS total_bytes
FROM `{region_path}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND job_type = 'QUERY'
  AND state = 'DONE'
  AND REGEXP_CONTAINS(query, r'{regex}')
""".strip()

    list_sql = f"""
SELECT
  DATE(creation_time)                                AS day,
  user_email,
  ROUND(total_bytes_billed / POW(1024,3), 3)         AS gib_billed,
  SUBSTR(REGEXP_REPLACE(query, r'\\s+', ' '), 1, 200) AS query_preview,
  job_id
FROM `{region_path}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
  AND job_type = 'QUERY'
  AND state = 'DONE'
  AND REGEXP_CONTAINS(query, r'{regex}')
ORDER BY total_bytes_billed DESC
LIMIT 10
""".strip()

    client = _client(config)
    summary = next(iter(client.query(summary_sql).result()))
    n_offenders = int(summary.n or 0)
    total_bytes = int(summary.total_bytes or 0)
    rows = list(client.query(list_sql).result())

    table_rows = [
        {
            "day": r.day.isoformat(),
            "user": r.user_email or "—",
            "gib": float(r.gib_billed or 0),
            "query": r.query_preview or "",
            "job": r.job_id,
        }
        for r in rows
    ]
    biggest = table_rows[0]["gib"] if table_rows else 0.0

    if biggest > C4_RED_GIB:
        state = "red"
    elif biggest > C4_YELLOW_GIB:
        state = "yellow"
    else:
        state = "green"

    return {
        "headline": {
            "offenders MTD": str(n_offenders),
            "biggest": f"{biggest:.2f} GiB",
            "total billed": f"{total_bytes / ONE_GIB:.2f} GiB",
        },
        "table": {
            "columns": [
                {"key": "day", "label": "day"},
                {"key": "user", "label": "user"},
                {"key": "gib", "label": "GiB", "fmt": "gib"},
                {"key": "query", "label": "query"},
                {"key": "job", "label": "job id", "fmt": "mono-short"},
            ],
            "rows": table_rows,
        },
        "state": state,
        "source": " ".join(list_sql.split()),
    }
