"""Orchestrator: load config, run each tile, write web/data.json.

Each tile function returns a dict matching the payload shape in docs/spec.md.
Tile failures are captured per-tile and do not crash the run.
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import bq, ga4

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
OUT_PATH = REPO_ROOT / "web" / "data.json"


TILES = [
    ("A1", bq.tile_a1_events_per_day),
    ("A2", ga4.tile_a2_data_api_quotas),
    ("A3", ga4.tile_a3_collection_config),
    ("A4", ga4.tile_a4_conversions),
    ("A5", ga4.tile_a5_audiences),
    ("B1", bq.tile_b1_query_bytes_mtd),
    ("B2", bq.tile_b2_active_storage),
    ("B4", bq.tile_b4_cache_hit_rate),
    ("C1", bq.tile_c1_top_queries_48h),
    ("C2", bq.tile_c2_top_tables_mtd),
    ("C3", bq.tile_c3_top_users_mtd),
    ("C4", bq.tile_c4_select_star_events),
]


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"config.yaml not found at {CONFIG_PATH}. "
            "Copy config.example.yaml and fill it in."
        )
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def run_tile(tile_id: str, fn, config: dict) -> dict:
    try:
        return fn(config)
    except Exception as exc:
        return {
            "state": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def main() -> int:
    config = load_config()
    payload = {
        "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "project_id": config["gcp"]["project_id"],
            "property_id": config["ga4"]["property_id"],
            "region": config["gcp"]["region"],
        },
        "tiles": {tid: run_tile(tid, fn, config) for tid, fn in TILES},
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"Wrote {OUT_PATH} with {len(payload['tiles'])} tiles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
