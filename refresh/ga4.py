"""GA4 tile implementations. Admin API for config/inventory, Data API for quotas.

Per-tile payload contract (same as bq.py):
    {"headline": {...}, "series": [...] | None, "table": {...} | None,
     "state": "green"|"yellow"|"red", "source": "..."}

Failures raise; the orchestrator captures them as {"state": "error", ...}.
"""

from __future__ import annotations

import os
from functools import lru_cache

from google.analytics.admin_v1alpha import AnalyticsAdminServiceClient
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Metric, RunReportRequest
from google.oauth2 import service_account

# Caps per GA4 Standard tier.
A3_DIMS_CAP = 50      # custom dimensions per property
A3_METRICS_CAP = 50   # custom metrics per property
A3_EVENTS_CAP = 500   # distinct event names per property
A4_CAP = 30           # key events / conversion events
A5_CAP = 100          # audiences


# ---- helpers ----------------------------------------------------------------

@lru_cache(maxsize=4)
def _sa_credentials(credentials_path: str | None):
    """SA credentials if a key file exists at the configured path, else None
    (lets the API client fall back to ADC — the CI path)."""
    if credentials_path:
        path = os.path.abspath(credentials_path)
        if os.path.exists(path):
            return service_account.Credentials.from_service_account_file(path)
    return None


@lru_cache(maxsize=4)
def _admin_client(credentials_path: str | None) -> AnalyticsAdminServiceClient:
    creds = _sa_credentials(credentials_path)
    return AnalyticsAdminServiceClient(credentials=creds) if creds else AnalyticsAdminServiceClient()


@lru_cache(maxsize=4)
def _data_client(credentials_path: str | None) -> BetaAnalyticsDataClient:
    creds = _sa_credentials(credentials_path)
    return BetaAnalyticsDataClient(credentials=creds) if creds else BetaAnalyticsDataClient()


def _client(config: dict) -> AnalyticsAdminServiceClient:
    return _admin_client(config["gcp"].get("credentials_path"))


def _property_parent(config: dict) -> str:
    return f"properties/{config['ga4']['property_id']}"


def _state_count(n: int, yellow_at: int, red_at: int) -> str:
    if n >= red_at:
        return "red"
    if n >= yellow_at:
        return "yellow"
    return "green"


# ---- tiles ------------------------------------------------------------------

def tile_a2_data_api_quotas(config: dict) -> dict:
    """A2: GA4 Data API token quotas (per-day, per-hour, concurrent).

    One minimal runReport with returnPropertyQuota=true. The response's
    property_quota field carries consumed + remaining for each dimension —
    pct comes from consumed / (consumed + remaining), so we don't need to
    hard-code the GA4 Standard caps (200k/day, 40k/hr, 10 concurrent).
    The call itself consumes ~1 token/day, which is the noise floor.

    State: red if any dimension >= 90%, yellow >= 60%, else green.
    """
    property_id = config["ga4"]["property_id"]
    creds_path = config["gcp"].get("credentials_path")
    client = _data_client(creds_path)

    req = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date="today", end_date="today")],
        metrics=[Metric(name="activeUsers")],
        return_property_quota=True,
        limit=1,
    )
    q = client.run_report(req).property_quota

    def pct(quota_dim) -> float:
        total = (quota_dim.consumed or 0) + (quota_dim.remaining or 0)
        return (quota_dim.consumed / total) if total > 0 else 0.0

    pct_day = pct(q.tokens_per_day)
    pct_hour = pct(q.tokens_per_hour)
    pct_conc = pct(q.concurrent_requests)
    max_pct = max(pct_day, pct_hour, pct_conc)

    if max_pct >= 0.90:
        state = "red"
    elif max_pct >= 0.60:
        state = "yellow"
    else:
        state = "green"

    def line(quota_dim) -> str:
        total = (quota_dim.consumed or 0) + (quota_dim.remaining or 0)
        return f"{quota_dim.consumed:,} of {total:,}"

    return {
        "headline": {
            "peak": f"{max_pct * 100:.1f}%",
            "tokens / day": line(q.tokens_per_day),
            "tokens / hour": line(q.tokens_per_hour),
            "concurrent": line(q.concurrent_requests),
        },
        "series": [
            {"x": "tokens / day", "y": round(pct_day * 100, 2)},
            {"x": "tokens / hour", "y": round(pct_hour * 100, 2)},
            {"x": "concurrent", "y": round(pct_conc * 100, 2)},
        ],
        "indexAxis": "y",
        "state": state,
        "source": "GA4 Data API: properties.runReport with returnPropertyQuota=true",
    }


def tile_a3_collection_config(config: dict) -> dict:
    """A3: Custom dimensions / custom metrics / distinct events vs caps (50/50/500).

    Three sub-measurements:
      - Admin API: properties.customDimensions.list → count
      - Admin API: properties.customMetrics.list → count
      - BQ:       COUNT(DISTINCT event_name) over events_* last 30d (proxy for
                  "configured events" since there's no Admin API for that;
                  delegated to bq.count_distinct_event_names_30d)

    State: red if any single sub-measurement is >= 90% of its cap, yellow if
    >= 60%, else green.
    """
    parent = _property_parent(config)
    client = _client(config)

    n_dims = len(list(client.list_custom_dimensions(parent=parent)))
    n_metrics = len(list(client.list_custom_metrics(parent=parent)))

    # BQ query lives in bq.py; import inline to avoid a top-level circular ref
    # (bq.py never imports from ga4.py, so this stays one-directional).
    from .bq import count_distinct_event_names_30d
    n_events = count_distinct_event_names_30d(config)

    pct_dims = n_dims / A3_DIMS_CAP
    pct_metrics = n_metrics / A3_METRICS_CAP
    pct_events = n_events / A3_EVENTS_CAP
    max_pct = max(pct_dims, pct_metrics, pct_events)

    if max_pct >= 0.90:
        state = "red"
    elif max_pct >= 0.60:
        state = "yellow"
    else:
        state = "green"

    return {
        "headline": {
            "peak": f"{max_pct * 100:.0f}%",
            "events (30d)": f"{n_events} of {A3_EVENTS_CAP}",
            "metrics": f"{n_metrics} of {A3_METRICS_CAP}",
            "dims": f"{n_dims} of {A3_DIMS_CAP}",
        },
        "series": [
            {"x": f"events (of {A3_EVENTS_CAP})", "y": round(pct_events * 100, 1)},
            {"x": f"metrics (of {A3_METRICS_CAP})", "y": round(pct_metrics * 100, 1)},
            {"x": f"dims (of {A3_DIMS_CAP})", "y": round(pct_dims * 100, 1)},
        ],
        "indexAxis": "y",
        "state": state,
        "source": "GA4 Admin API: customDimensions.list + customMetrics.list; "
                  "BQ: COUNT(DISTINCT event_name) over events_* last 30d",
    }


def tile_a4_conversions(config: dict) -> dict:
    """A4: Key events count vs 30 cap (GA4 renamed 'conversion events' to
    'key events' in 2024 — same underlying entity, same 30 cap)."""
    parent = _property_parent(config)
    items = list(_client(config).list_key_events(parent=parent))
    n = len(items)
    state = _state_count(n, yellow_at=18, red_at=27)

    return {
        "headline": {
            "of 30 cap": f"{(n / A4_CAP) * 100:.0f}%",
            "key events": f"{n} of {A4_CAP}",
        },
        "state": state,
        "source": f"GA4 Admin API: properties.keyEvents.list (parent={parent})",
    }


def tile_a5_audiences(config: dict) -> dict:
    """A5: Audiences count vs 100 cap."""
    parent = _property_parent(config)
    items = list(_client(config).list_audiences(parent=parent))
    n = len(items)
    state = _state_count(n, yellow_at=60, red_at=90)

    return {
        "headline": {
            "of 100 cap": f"{(n / A5_CAP) * 100:.0f}%",
            "audiences": f"{n} of {A5_CAP}",
        },
        "state": state,
        "source": f"GA4 Admin API: properties.audiences.list (parent={parent})",
    }
