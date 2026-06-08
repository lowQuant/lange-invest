"""Data layer for the /futures overview page.

Reads the public ``futures`` library and the metadata symbol ``universe/Futures``,
computes per-symbol chart data (continuous back-adjusted close with EMA50/100
and the latest forward curve), plus a simple trend metric (last close vs EMA100
in %). Everything is built through the existing ``arctic_charting`` helpers so
the gallery charts share the symbol page's chart vocabulary.

Cached in-process — invalidate via ``invalidate_cache()`` after a fresh write.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from app import arctic_charting as ac
from app import public_access
from app.engine import ensure_connected


_OVERVIEW_CACHE: dict[str, Any] = {}


def invalidate_cache() -> None:
    _OVERVIEW_CACHE.clear()


# ── universe/Futures metadata ────────────────────────────────────────────────

def _read_universe_futures() -> dict[str, dict[str, Any]]:
    """Return ``{SYMBOL: {sector, name, exchange, currency, multiplier, tick_size, …}}``.

    Tries the canonical key column ``symbol`` first, then a few common
    alternatives (``ibkr_symbol`` / ``ticker`` / ``name``). Returns ``{}`` on any
    failure — the page degrades to a metadata-less view.
    """
    try:
        usyms = public_access.list_symbols("universe")
    except Exception:  # noqa: BLE001
        return {}
    target = next((s for s in usyms if s.lower() == "futures"), None)
    if target is None:
        return {}
    try:
        df = public_access.read_data("universe", target)
    except Exception:  # noqa: BLE001
        return {}

    key_col = next((c for c in ("symbol", "ibkr_symbol", "ticker", "name") if c in df.columns), None)
    if key_col is None:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        sym = str(row[key_col]).strip().upper()
        if not sym:
            continue
        out[sym] = {str(k): (None if pd.isna(v) else v) for k, v in row.items() if k != key_col}
    return out


# ── Per-symbol compute ───────────────────────────────────────────────────────

def _compute_for_symbol(symbol: str) -> dict[str, Any] | None:
    """Continuous curve (+ EMA50/100) + term structure + trend metric for one root.

    Returns ``None`` if the symbol is not a MultiIndex (date, contract) future or
    the read fails. Never raises — bad symbols are skipped silently.
    """
    try:
        df = public_access.read_data("futures", symbol)
    except Exception:  # noqa: BLE001
        return None
    if not ac.detect_multiindex_contracts(df):
        return None
    col = "close" if "close" in df.columns else next(
        (c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])), None)
    if col is None:
        return None

    curve_main = term_main = None
    try:
        curve_main, _, curve_err = ac.build_chart(df, symbol, {
            "contract_mode": "single",
            "contract_col": col,
            "continuous_method": "back_diff",
            "roll_rule": "expiry",
            "contract_rank": 1,
            "chart_type": "line",
            "studies": '[{"type":"ema","period":50},{"type":"ema","period":100}]',
        })
        if curve_err:
            curve_main = None
    except Exception:  # noqa: BLE001
        curve_main = None

    try:
        term_main, _, term_err = ac.build_chart(df, symbol, {
            "contract_mode": "term_structure",
            "contract_col": col,
            "chart_type": "line",
        })
        if term_err:
            term_main = None
    except Exception:  # noqa: BLE001
        term_main = None

    last_close = trend_pct = None
    if curve_main and curve_main.get("datasets"):
        raw = curve_main["datasets"][0].get("data", [])
        ema100 = next((d.get("data", []) for d in curve_main["datasets"]
                       if str(d.get("label", "")).upper() == "EMA(100)"), [])
        last_close = next((v for v in reversed(raw) if v is not None), None)
        last_ema100 = next((v for v in reversed(ema100) if v is not None), None)
        if last_close is not None and last_ema100 not in (None, 0):
            trend_pct = (last_close - last_ema100) / last_ema100

    return {
        "symbol": symbol,
        "curve_chart": curve_main,
        "term_chart": term_main,
        "last": last_close,
        "trend_pct": trend_pct,
    }


# ── Top-level builder ────────────────────────────────────────────────────────

def build_overview() -> dict[str, Any]:
    """Sector-grouped view model for the /futures page. Cached in-process."""
    if "data" in _OVERVIEW_CACHE:
        return _OVERVIEW_CACHE["data"]

    if not ensure_connected():
        return {"sectors": [], "rows": [], "error": "The data engine is not connected in this environment."}

    try:
        symbols = sorted(public_access.list_symbols("futures"))
    except Exception as e:  # noqa: BLE001
        return {"sectors": [], "rows": [], "error": f"Could not list the futures library: {e}"}

    uni = _read_universe_futures()
    rows: list[dict[str, Any]] = []
    for s in symbols:
        data = _compute_for_symbol(s)
        if data is None:
            continue
        meta = uni.get(s.upper(), {})
        rows.append({
            "symbol": s,
            "name": str(meta.get("name") or s),
            "sector": str(meta.get("sector") or "Other"),
            "exchange": str(meta.get("exchange") or ""),
            "currency": str(meta.get("currency") or ""),
            "last": data["last"],
            "trend_pct": data["trend_pct"],
            "curve_chart": data["curve_chart"],
            "term_chart": data["term_chart"],
        })

    sectors: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sectors.setdefault(r["sector"], []).append(r)
    for items in sectors.values():
        items.sort(key=lambda r: (r["trend_pct"] if r["trend_pct"] is not None else -math.inf), reverse=True)

    out = {
        "sectors": [{"name": sec, "items": sectors[sec]} for sec in sorted(sectors)],
        "rows": rows,
        "error": None,
    }
    _OVERVIEW_CACHE["data"] = out
    return out
