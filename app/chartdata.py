"""Server-side DataFrame → chart spec (consumed by the shared LangeChart module).

Detects OHLC data and emits a candlestick spec (with an optional volume subplot);
otherwise emits a line spec over the numeric columns. Kept small and pure so it
is easy to test and reuse for both the Database tab and precompute.
"""
from __future__ import annotations

from typing import Any

# Cap points sent to the browser for a single chart.
MAX_POINTS = 1000

_OHLC = {"open": ["open", "o"], "high": ["high", "h"], "low": ["low", "l"], "close": ["close", "c", "last", "settle", "price"]}


def _find(columns_lower: dict[str, str], names: list[str]) -> str | None:
    for n in names:
        if n in columns_lower:
            return columns_lower[n]
    return None


def _index_labels(df) -> list[str]:
    import pandas as pd

    if isinstance(df.index, pd.MultiIndex):
        return [" / ".join(str(p) for p in tup) for tup in df.index]
    if isinstance(df.index, pd.DatetimeIndex):
        return [d.date().isoformat() if d == d.normalize() else str(d) for d in df.index]
    return [str(i) for i in df.index]


def build_chart(df, title: str = "") -> dict[str, Any] | None:
    """Return {"chart": {...}, "subplots": [...]} or None if nothing chartable."""
    if df is None or df.empty:
        return None

    df = df.tail(MAX_POINTS)
    cols_lower = {str(c).lower(): c for c in df.columns}
    x = _index_labels(df)

    o = _find(cols_lower, _OHLC["open"])
    h = _find(cols_lower, _OHLC["high"])
    low = _find(cols_lower, _OHLC["low"])
    c = _find(cols_lower, _OHLC["close"])
    vol = cols_lower.get("volume") or cols_lower.get("vol")

    subplots: list[dict] = []

    if o and h and low and c:
        data = [
            {"o": float(r[o]), "h": float(r[h]), "l": float(r[low]), "c": float(r[c])}
            if r[[o, h, low, c]].notna().all() else None
            for _, r in df.iterrows()
        ]
        chart = {
            "title": title or "Price", "chart_type": "candlestick",
            "x_label": "Date", "x_values": x,
            "datasets": [{"label": "OHLC", "data": data}],
        }
        if vol is not None:
            subplots.append({
                "chart_type": "bar", "y_label": "Volume", "x_values": x,
                "datasets": [{"label": "Volume", "data": [None if v != v else float(v) for v in df[vol]]}],
            })
        return {"chart": chart, "subplots": subplots}

    # Fallback: line over numeric columns (prefer a single close-like column).
    numeric = df.select_dtypes("number")
    if numeric.shape[1] == 0:
        return None
    if c and c in numeric.columns:
        numeric = numeric[[c]]
    elif numeric.shape[1] > 4:
        numeric = numeric.iloc[:, :4]

    datasets = [
        {"label": str(col), "data": [None if v != v else float(v) for v in numeric[col]]}
        for col in numeric.columns
    ]
    return {
        "chart": {"title": title or "Series", "chart_type": "line",
                  "x_label": "Date", "x_values": x, "datasets": datasets},
        "subplots": subplots,
    }
