"""Shared utilities for tile implementations."""

from __future__ import annotations


def state_from_pct(pct: float, warning: float, critical: float) -> str:
    """Map a 0-1+ ratio to a tile state colour.

    `pct` may exceed 1.0 (the tile is over the cap). Thresholds come from
    config.thresholds and default to 0.6 / 0.9 in config.example.yaml.
    """
    if pct >= critical:
        return "red"
    if pct >= warning:
        return "yellow"
    return "green"
