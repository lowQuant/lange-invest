"""Data layer for the /futures overview page.

Reads the public ``futures`` library and the metadata symbol ``universe/Futures``,
computes per-symbol chart data (continuous back-adjusted close with EMA50/100
and the latest forward curve), plus a trend signal and an ATR(100) estimate
used by the position-sizing tab.

Sector is taken from the ``asset_class`` column of ``universe/Futures``
(``sector`` / ``category`` as fallbacks). Point value is read from
``multiplier`` (``point_value`` / ``contract_size`` as fallbacks).

Trend signal:
    +1  uptrend    — EMA(50) > EMA(100)  AND  close > EMA(100)
    -1  downtrend  — EMA(50) < EMA(100)  AND  close < EMA(100)
     0  neutral    — anything else (mixed signals)

Cached in-process — invalidate via ``invalidate_cache()`` after a fresh write.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app import arctic_charting as ac
from app import public_access
from app.engine import ensure_connected


_META_CACHE: dict[str, Any] = {}
# Per-symbol payload cache. Negative results (unusable curves) are cached as
# ``None`` so we don't retry the expensive read on every page refresh.
_CHART_CACHE: dict[str, dict[str, Any] | None] = {}

# Column-name aliases — production may use any of these.
SECTOR_COLS = ("asset_class", "sector", "category", "assetClass")
MULTIPLIER_COLS = ("multiplier", "point_value", "contract_size", "contractMultiplier")
NAME_COLS = ("name", "description", "long_name")

# Heuristic guards against the micro-contract failure mode: instruments with
# only a handful of rolls (e.g. recently-launched micro grains) produce a
# back-adjusted series with a huge leading spike that ruins the y-axis. Drop
# the symbol entirely if it's too short or the series spans more than an order
# of magnitude — that range is wider than any real, healthy futures curve.
MIN_HISTORY_POINTS = 150
MAX_BACK_ADJ_RATIO = 10.0


def invalidate_cache() -> None:
    _META_CACHE.clear()
    _CHART_CACHE.clear()


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    """Return the first non-empty value among the given keys."""
    for k in keys:
        v = d.get(k)
        if v is not None and v != "" and not (isinstance(v, float) and v != v):
            return v
    return None


# ── universe/Futures metadata ────────────────────────────────────────────────

def _read_universe_futures() -> dict[str, dict[str, Any]]:
    """Return ``{SYMBOL: {name, sector, exchange, currency, multiplier, …}}``."""
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


def _meta_for(sym: str, uni: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Normalise a universe row down to the canonical fields the UI uses."""
    meta = uni.get(sym.upper(), {})
    sector = _first(meta, SECTOR_COLS) or "Other"
    mult = _first(meta, MULTIPLIER_COLS)
    try:
        mult = float(mult) if mult is not None else None
    except (TypeError, ValueError):
        mult = None
    name = str(_first(meta, NAME_COLS) or sym)
    return {
        "name": name,
        "sector": str(sector),
        "exchange": str(meta.get("exchange") or ""),
        "currency": str(meta.get("currency") or ""),
        "multiplier": mult,
        "is_micro": "micro" in name.lower(),
    }


# ── Front-month OHLC + ATR(100) ──────────────────────────────────────────────

def _front_month_ohlc(df: pd.DataFrame) -> pd.DataFrame | None:
    """Daily OHLC of the front-month contract (selected by smallest positive DTE).

    Falls back to the first contract per date if no `dte` column is present.
    Returns None if the frame doesn't carry OHLC.
    """
    cols = {c.lower(): c for c in df.columns}
    if not all(k in cols for k in ("high", "low", "close")):
        return None
    has_dte = "dte" in df.columns
    rows = []
    for date in sorted(df.index.get_level_values(0).unique()):
        try:
            slab = df.loc[date]
        except KeyError:
            continue
        if isinstance(slab, pd.Series):
            slab = slab.to_frame().T
        if has_dte:
            live = slab[slab["dte"] > 0]
            if len(live) == 0:
                continue
            front = live.sort_values(by="dte").iloc[0]
        else:
            front = slab.iloc[0]
        rows.append((date, float(front[cols["high"]]), float(front[cols["low"]]), float(front[cols["close"]])))
    if not rows:
        return None
    out = pd.DataFrame(rows, columns=["date", "h", "l", "c"]).set_index("date")
    return out


def _atr100(ohlc: pd.DataFrame | None) -> float | None:
    """Latest ATR(100) on a daily OHLC frame with columns ``h``, ``l``, ``c``."""
    if ohlc is None or len(ohlc) < 20:
        return None
    prev_c = ohlc["c"].shift(1)
    tr = pd.concat([
        ohlc["h"] - ohlc["l"],
        (ohlc["h"] - prev_c).abs(),
        (ohlc["l"] - prev_c).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(100, min_periods=20).mean().dropna()
    return float(atr.iloc[-1]) if len(atr) else None


def _curve_is_usable(curve_main: dict | None) -> bool:
    """Drop symbols whose back-adjusted series is too short or out of scale.

    Catches the micro-contract case where a sparse roll history produces a
    leading spike (first value 50–100× the latest), which both pollutes the
    EMAs and crushes the y-axis.
    """
    if not curve_main or not curve_main.get("datasets"):
        return False
    raw = curve_main["datasets"][0].get("data", [])
    valid = [v for v in raw if v is not None and isinstance(v, (int, float))]
    if len(valid) < MIN_HISTORY_POINTS:
        return False
    first, last = abs(valid[0]), abs(valid[-1])
    if first == 0 or last == 0:
        return False
    ratio = max(first / last, last / first)
    return ratio <= MAX_BACK_ADJ_RATIO


# ── Per-symbol compute ───────────────────────────────────────────────────────

def _compute_for_symbol(symbol: str) -> dict[str, Any] | None:
    """Continuous curve + term structure + trend + ATR(100) for one root.

    Returns ``None`` for symbols we should hide from the page entirely —
    unreadable, not a MultiIndex future, or with too few/too erratic back-
    adjusted observations to chart meaningfully.
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
        # back_ratio (multiplicative back-adjustment) keeps the series on the
        # same scale as actual prices, so close-vs-EMA comparisons are
        # meaningful. back_diff would bias every contango-heavy market to
        # "below EMA100" regardless of real trend.
        curve_main, _, curve_err = ac.build_chart(df, symbol, {
            "contract_mode": "single",
            "contract_col": col,
            "continuous_method": "back_ratio",
            "roll_rule": "expiry",
            "contract_rank": 1,
            "chart_type": "line",
            "studies": '[{"type":"ema","period":50},{"type":"ema","period":100}]',
        })
        if curve_err:
            curve_main = None
    except Exception:  # noqa: BLE001
        curve_main = None

    # Drop the symbol if the continuous curve is unusable (no data, too short
    # or the back-adjustment produced a leading spike from sparse rolls).
    if not _curve_is_usable(curve_main):
        return None

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

    # Trend signal: +1 if EMA50 > EMA100 AND close > EMA100, -1 if both below.
    last_close = last_ema50 = last_ema100 = None
    if curve_main and curve_main.get("datasets"):
        raw = curve_main["datasets"][0].get("data", [])
        ema50 = next((d.get("data", []) for d in curve_main["datasets"]
                      if str(d.get("label", "")).upper() == "EMA(50)"), [])
        ema100 = next((d.get("data", []) for d in curve_main["datasets"]
                       if str(d.get("label", "")).upper() == "EMA(100)"), [])
        last_close = next((v for v in reversed(raw) if v is not None), None)
        last_ema50 = next((v for v in reversed(ema50) if v is not None), None)
        last_ema100 = next((v for v in reversed(ema100) if v is not None), None)

    trend_signal = 0
    trend_pct = None
    if last_close is not None and last_ema50 is not None and last_ema100 not in (None, 0):
        trend_pct = (last_close - last_ema100) / last_ema100
        if last_ema50 > last_ema100 and last_close > last_ema100:
            trend_signal = 1
        elif last_ema50 < last_ema100 and last_close < last_ema100:
            trend_signal = -1

    # ATR(100) on front-month OHLC
    try:
        atr100 = _atr100(_front_month_ohlc(df))
    except Exception:  # noqa: BLE001
        atr100 = None

    return {
        "symbol": symbol,
        "curve_chart": curve_main,
        "term_chart": term_main,
        "last": last_close,
        "trend_pct": trend_pct,
        "trend_signal": trend_signal,
        "atr100": atr100,
    }


# ── Top-level builders ───────────────────────────────────────────────────────

def build_meta() -> dict[str, Any]:
    """Lightweight shell data for /futures: sector groups with metadata only.

    Cheap: one read of universe/Futures + one list of `futures` symbols. No
    per-symbol chart payloads, so the page renders instantly; JS then fetches
    /futures/api/payload to fill in trend numbers, ATR, and the charts.
    """
    if "data" in _META_CACHE:
        return _META_CACHE["data"]

    if not ensure_connected():
        return {"sectors": [], "rows": [], "error": "The data engine is not connected in this environment."}

    try:
        symbols = sorted(public_access.list_symbols("futures"))
    except Exception as e:  # noqa: BLE001
        return {"sectors": [], "rows": [], "error": f"Could not list the futures library: {e}"}

    uni = _read_universe_futures()
    rows: list[dict[str, Any]] = []
    for s in symbols:
        meta = _meta_for(s, uni)
        rows.append({
            "symbol": s,
            "name": meta["name"],
            "sector": meta["sector"],
            "exchange": meta["exchange"],
            "currency": meta["currency"],
            "multiplier": meta["multiplier"],
            "is_micro": meta["is_micro"],
        })

    sectors: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sectors.setdefault(r["sector"], []).append(r)
    for items in sectors.values():
        items.sort(key=lambda r: r["symbol"])

    out = {
        "sectors": [{"name": sec, "markets": sectors[sec]} for sec in sorted(sectors)],
        "rows": rows,
        "error": None,
    }
    _META_CACHE["data"] = out
    return out


def _payload_entry_for(symbol: str, uni: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Compute or fetch the per-symbol payload entry, using the per-symbol
    cache so a later ``subset='all'`` request only computes the rest.
    """
    if symbol in _CHART_CACHE:
        return _CHART_CACHE[symbol]
    data = _compute_for_symbol(symbol)
    if data is None:
        _CHART_CACHE[symbol] = None
        return None
    meta = _meta_for(symbol, uni)
    entry: dict[str, Any] = {
        "last": data["last"],
        "trend_pct": data["trend_pct"],
        "trend_signal": data["trend_signal"],
        "atr100": data["atr100"],
        "multiplier": meta["multiplier"],
        "is_micro": meta["is_micro"],
        "curve_chart": data["curve_chart"],
        "term_chart": data["term_chart"],
    }
    _CHART_CACHE[symbol] = entry
    return entry


def build_chart_payload(subset: str = "all") -> dict[str, Any]:
    """Per-symbol chart payloads + trend + ATR.

    ``subset='micro'`` computes only the markets whose universe ``name``
    contains "micro" — much cheaper on cold start, used as the default page
    load. ``subset='all'`` fills in the rest. Per-symbol cache means asking
    for ``'all'`` after ``'micro'`` only does the incremental work.
    """
    if not ensure_connected():
        return {}

    try:
        symbols = sorted(public_access.list_symbols("futures"))
    except Exception:  # noqa: BLE001
        return {}

    uni = _read_universe_futures()
    if subset == "micro":
        symbols = [s for s in symbols if _meta_for(s, uni)["is_micro"]]

    out: dict[str, Any] = {}
    for s in symbols:
        entry = _payload_entry_for(s, uni)
        if entry is not None:
            out[s] = entry
    return out
