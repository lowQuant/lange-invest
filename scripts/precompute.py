#!/usr/bin/env python3
"""Precompute task — write chart-ready + stats JSON snapshots for public pages.

Runs on a schedule (cron / PythonAnywhere scheduled task). Reads ONLY through
``app.public_access`` (allowlisted, read-only) — never ArcticDB directly, never
a protected/IBKR library. All heavy work (downsampling, stats) happens here so
the public read path does almost no per-request compute.

Snapshot schema matches what the templates + LangeChart module consume; see
``scripts/gen_sample_data.py`` for the same shape with synthetic data.

This module is import-safe without ArcticDB installed: the driver is only
touched inside ``main()``.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import SNAPSHOT_DIR, get_config  # noqa: E402

# Cap points written to each public snapshot — bounds payload + render cost.
MAX_POINTS = 1500


def _connect_from_env() -> None:
    """Connect the shared ConnectionManager from .env S3 vars."""
    from core.connection import ConnectionManager, get_manager

    env = ConnectionManager.detect_env_connection()
    if not env:
        raise SystemExit(
            "No ArcticDB connection in .env (need AWS_*/BUCKET_NAME). "
            "Precompute reads the live engine to build public snapshots."
        )
    get_manager().connect(env["uri"], name=env["name"])


def _downsample(values: list, cap: int = MAX_POINTS) -> list:
    n = len(values)
    if n <= cap:
        return values
    step = math.ceil(n / cap)
    return values[::step]


def _equity_and_drawdown(series) -> tuple[list[str], list[float], list[float]]:
    """Turn a returns/price series (pandas Series, datetime index) into an
    equity curve + drawdown, downsampled for the public payload."""
    import pandas as pd  # local import

    s = series.dropna()
    # Treat as a price/equity level if strictly positive & smooth; else as returns.
    if (s <= 0).any():
        equity = (1 + s).cumprod()
    else:
        equity = s / s.iloc[0]
    peak = equity.cummax()
    dd = (equity / peak - 1.0) * 100.0

    idx = [str(getattr(i, "date", lambda: i)()) for i in equity.index]
    if isinstance(equity.index, pd.DatetimeIndex):
        idx = [i.date().isoformat() for i in equity.index]

    idx = _downsample(idx)
    eq = _downsample([round(float(v), 4) for v in equity.tolist()])
    ddv = _downsample([round(float(v), 2) for v in dd.tolist()])
    return idx, eq, ddv


def _stats(equity: list[float], dd: list[float]) -> list[dict]:
    if len(equity) < 2:
        return []
    total = equity[-1] / equity[0] - 1
    years = max(len(equity) / 252.0, 1e-6)
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1
    maxdd = min(dd) if dd else 0.0
    return [
        {"label": "CAGR", "value": f"{cagr*100:.1f}%", "dir": "up" if cagr > 0 else "down"},
        {"label": "Total", "value": f"{total*100:.1f}%", "dir": "up" if total > 0 else "down"},
        {"label": "Max DD", "value": f"{maxdd:.1f}%", "dir": "down"},
    ]


def _build_strategy_snapshot(ac_slug: str, variant) -> dict | None:
    """Read the variant's stats series from the allowlisted stats library and
    build a snapshot. Returns None if the symbol isn't present."""
    from app import public_access as pa

    stats_lib = f"{ac_slug}_stats"
    signals_lib = f"{ac_slug}_signals"

    try:
        df = pa.read_data(stats_lib, variant.stats_symbol)
    except pa.AccessDenied:
        raise
    except Exception:
        return None
    if df is None or df.empty:
        return None

    # Prefer an explicit equity/return column; else first numeric column.
    col = next((c for c in ("equity", "nav", "cum_return", "returns", "ret") if c in df.columns), None)
    if col is None:
        numeric = df.select_dtypes("number").columns
        if len(numeric) == 0:
            return None
        col = numeric[0]

    idx, eq, dd = _equity_and_drawdown(df[col])

    # Universe = current signal symbols, if published.
    universe: list[str] = []
    try:
        sdf = pa.read_data(signals_lib, variant.signals_symbol, row_range=(0, 200))
        sym_col = next((c for c in ("symbol", "ticker", "ibkr_symbol", "name") if c in sdf.columns), None)
        if sym_col is not None:
            universe = [str(x) for x in sdf[sym_col].dropna().unique().tolist()][:50]
    except Exception:
        pass

    return {
        "asset_class": ac_slug,
        "variant": variant.slug,
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "chart": {
            "title": "Equity curve",
            "chart_type": "line", "x_label": "Date", "y_label": "Growth of $1",
            "x_values": idx,
            "datasets": [{"label": variant.name, "data": eq, "fill": True}],
        },
        "subplots": [{
            "chart_type": "line", "y_label": "Drawdown %", "x_values": idx,
            "datasets": [{"label": "Drawdown", "data": dd, "color": "#ef4444"}],
        }],
        "stats": _stats(eq, dd),
        "universe": universe,
    }


def _write(rel: str, payload: dict) -> None:
    path = SNAPSHOT_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    print(f"wrote {path.relative_to(ROOT)}")


def main() -> None:
    _connect_from_env()
    cfg = get_config()

    for ac in cfg.asset_classes:
        for variant in ac.variants:
            snap = _build_strategy_snapshot(ac.slug, variant)
            if snap is None:
                print(f"skip {ac.slug}/{variant.slug} (no data)")
                continue
            _write(f"strategies/{ac.slug}/{variant.slug}.json", snap)

    # Model portfolio snapshot (combined curve from the model_portfolio library).
    from app import public_access as pa

    try:
        mdf = pa.read_data("model_portfolio", "combined")
        col = next((c for c in ("equity", "nav", "returns") if c in mdf.columns), mdf.select_dtypes("number").columns[0])
        idx, eq, dd = _equity_and_drawdown(mdf[col])
        model = {
            "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "stats": _stats(eq, dd),
            "chart": {"title": "Combined backtest", "chart_type": "line", "x_label": "Date",
                      "y_label": "Growth of $1", "x_values": idx,
                      "datasets": [{"label": "Model portfolio", "data": eq, "fill": True}]},
            "subplots": [{"chart_type": "line", "y_label": "Drawdown %", "x_values": idx,
                          "datasets": [{"label": "Drawdown", "data": dd, "color": "#ef4444"}]}],
            "allocations": [],
        }
        # Allocation weights, if stored as a one-row symbol.
        try:
            adf = pa.read_data("model_portfolio", "allocations")
            for _, row in adf.iterrows():
                model["allocations"].append({"name": str(row.get("name", "")), "weight": float(row.get("weight", 0))})
        except Exception:
            pass
        _write("model_portfolio.json", model)
    except Exception as e:  # noqa: BLE001
        print(f"skip model_portfolio ({e})")

    print("precompute complete.")


if __name__ == "__main__":
    main()
