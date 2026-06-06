"""Precomputed snapshot reader for the public read path.

Public pages read chart-ready + stats JSON written by the precompute task
(``scripts/precompute.py``) — never ArcticDB at request time. This keeps the
public path's per-request compute near zero, which is required under the host's
metered-CPU constraint.

Snapshot layout under ``data/snapshots``::

    strategies/<asset_class>/<variant>.json
    model_portfolio.json

The reader is defensive: a missing snapshot returns ``None`` so pages can show
a graceful "not yet published" state instead of erroring.
"""
from __future__ import annotations

import json
from typing import Any

from app.config import SNAPSHOT_DIR


def _read_json(rel_path: str) -> dict[str, Any] | None:
    path = SNAPSHOT_DIR / rel_path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def strategy_snapshot(asset_class: str, variant: str) -> dict[str, Any] | None:
    """Chart + stats snapshot for one asset-class / variant strategy page."""
    return _read_json(f"strategies/{asset_class}/{variant}.json")


def model_portfolio_snapshot() -> dict[str, Any] | None:
    """Allocations + combined backtest curve for the logged-out portfolio view."""
    return _read_json("model_portfolio.json")
