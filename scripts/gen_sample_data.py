#!/usr/bin/env python3
"""Generate synthetic snapshot fixtures so the public site renders without S3.

This is a DEV convenience only. The real snapshots are written by
``scripts/precompute.py`` (Phase 3) from ArcticDB via ``public_access``. The
schema here is the contract the templates + LangeChart module consume.

Deterministic (seeded) so output is stable across runs.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "snapshots"

# Mirror config/site.toml taxonomy (kept in sync by hand for the fixture).
TAXONOMY = {
    "equities": {
        "mean-reversion": (0.16, 0.11, ["AAPL", "MSFT", "NVDA", "AMZN", "META", "JPM", "XOM", "UNH"]),
        "short": (0.07, 0.18, ["GME", "AMC", "CVNA", "PTON", "BYND", "W"]),
        "long": (0.13, 0.14, ["GOOGL", "AVGO", "LLY", "V", "COST", "HD", "PG"]),
        "base": (0.12, 0.09, ["Blended sub-strategies"]),
    },
    "futures": {
        "trend": (0.14, 0.16, ["ES", "NQ", "CL", "GC", "ZN", "6E", "ZC", "HG"]),
        "carry": (0.10, 0.10, ["GC", "SI", "CL", "NG", "ZN", "ZB"]),
    },
}


def curve(seed: int, mu: float, sigma: float, n: int = 520):
    rng = random.Random(seed)
    dates, equity, dd = [], [], []
    val, peak = 1.0, 1.0
    day = dt.date(2024, 1, 2)
    daily_mu = mu / 252.0
    daily_sig = sigma / math.sqrt(252.0)
    for _ in range(n):
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        shock = rng.gauss(daily_mu, daily_sig)
        val *= (1.0 + shock)
        peak = max(peak, val)
        dates.append(day.isoformat())
        equity.append(round(val, 4))
        dd.append(round((val / peak - 1.0) * 100, 2))
        day += dt.timedelta(days=1)
    return dates, equity, dd


def stats_from(equity, dd, mu, sigma):
    total = equity[-1] / equity[0] - 1
    years = len(equity) / 252.0
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1
    sharpe = mu / sigma
    maxdd = min(dd)
    return [
        {"label": "CAGR", "value": f"{cagr*100:.1f}%", "dir": "up" if cagr > 0 else "down"},
        {"label": "Total", "value": f"{total*100:.1f}%", "dir": "up" if total > 0 else "down"},
        {"label": "Sharpe", "value": f"{sharpe:.2f}", "dir": ""},
        {"label": "Max DD", "value": f"{maxdd:.1f}%", "dir": "down"},
        {"label": "Vol", "value": f"{sigma*100:.0f}%", "dir": ""},
    ]


def strategy_snapshot(ac, variant, mu, sigma, universe, seed):
    dates, equity, dd = curve(seed, mu, sigma)
    return {
        "asset_class": ac,
        "variant": variant,
        "generated_at": dt.date.today().isoformat(),
        "chart": {
            "title": "Equity curve",
            "chart_type": "line",
            "x_label": "Date",
            "y_label": "Growth of $1",
            "x_values": dates,
            "datasets": [{"label": variant, "data": equity, "fill": True}],
        },
        "subplots": [{
            "chart_type": "line",
            "y_label": "Drawdown %",
            "x_values": dates,
            "datasets": [{"label": "Drawdown", "data": dd, "color": "#ef4444"}],
        }],
        "stats": stats_from(equity, dd, mu, sigma),
        "universe": universe,
    }


def main():
    seed = 1
    for ac, variants in TAXONOMY.items():
        for variant, (mu, sigma, universe) in variants.items():
            seed += 1
            snap = strategy_snapshot(ac, variant, mu, sigma, universe, seed)
            path = OUT / "strategies" / ac / f"{variant}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snap, indent=2))
            print(f"wrote {path.relative_to(ROOT)}")

    # Model portfolio: blended curve + allocations.
    dates, equity, dd = curve(99, 0.11, 0.08)
    model = {
        "generated_at": dt.date.today().isoformat(),
        "stats": stats_from(equity, dd, 0.11, 0.08),
        "chart": {
            "title": "Combined backtest",
            "chart_type": "line", "x_label": "Date", "y_label": "Growth of $1",
            "x_values": dates,
            "datasets": [{"label": "Model portfolio", "data": equity, "fill": True}],
        },
        "subplots": [{
            "chart_type": "line", "y_label": "Drawdown %", "x_values": dates,
            "datasets": [{"label": "Drawdown", "data": dd, "color": "#ef4444"}],
        }],
        "allocations": [
            {"name": "Equities · Base", "weight": 0.45},
            {"name": "Futures · Trend", "weight": 0.30},
            {"name": "Futures · Carry", "weight": 0.15},
            {"name": "Cash", "weight": 0.10},
        ],
    }
    (OUT / "model_portfolio.json").write_text(json.dumps(model, indent=2))
    print(f"wrote {(OUT / 'model_portfolio.json').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
