"""Data layer for the /futures overview page (futures + equities).

Reads the public ``futures`` library and the metadata symbol ``universe/Futures``,
computes per-symbol chart data (continuous back-adjusted close with EMA50/100
and the latest forward curve), plus a trend signal and an ATR(100) estimate
used by the position-sizing tab.

Equities ride along: any allowlisted equity library (``equities`` /
``market_data`` / ``stocks``) contributes single-index OHLCV symbols with the
same close + EMA50/100 chart, trend signal and ATR(100). Equities have no term
structure and a point value of 1 (per share) unless the universe says otherwise.

Sector is taken from the ``asset_class`` column of ``universe/Futures``
(``sector`` / ``category`` as fallbacks). Point value is read from
``multiplier`` (``point_value`` / ``contract_size`` as fallbacks).

Trend signal:
    +1  uptrend    — EMA(50) > EMA(100)  AND  close > EMA(100)
    -1  downtrend  — EMA(50) < EMA(100)  AND  close < EMA(100)
     0  neutral    — anything else (mixed signals)

Caching: in-process with a TTL (``FUTURES_CACHE_TTL`` seconds, default 900) so
out-of-process ArcticDB writes show up without a restart. Writes that go
through this app's own /mcp endpoint invalidate immediately via
``invalidate_cache()``.
"""
from __future__ import annotations

import os
import time
from typing import Any

import pandas as pd

from app import arctic_charting as ac
from app import public_access
from app.engine import ensure_connected


_META_CACHE: dict[str, Any] = {}
# Per-symbol payload cache. Negative results (unusable curves) are cached as
# ``None`` so we don't retry the expensive read on every page refresh.
_CHART_CACHE: dict[str, dict[str, Any] | None] = {}
# Correlation matrices keyed by (subset, window).
_CORR_CACHE: dict[tuple[str, int], dict[str, Any]] = {}

# TTL so direct-to-ArcticDB writes (scripts, other processes) surface without a
# web-process restart. Writes through this app's /mcp endpoint invalidate
# immediately; the TTL is the safety net for everything else.
_CACHE_TTL_S = float(os.getenv("FUTURES_CACHE_TTL", "900"))
_cache_filled_at: float | None = None

# Column-name aliases — production may use any of these.
SECTOR_COLS = ("asset_class", "sector", "category", "assetClass", "industry")
MULTIPLIER_COLS = ("multiplier", "point_value", "contract_size", "contractMultiplier")
NAME_COLS = ("name", "description", "long_name")

# Equity price libraries we accept, in priority order. Only allowlisted +
# actually existing ones are read; a futures root always wins a name clash.
EQUITY_LIBRARIES = ("equities", "market_data", "stocks")
# universe/<symbol> names (case-insensitive) that carry equity metadata.
UNIVERSE_EQUITY_SYMBOLS = ("equities", "market_data", "stocks", "equity")
UNIVERSE_FUTURES_SYMBOLS = ("futures",)

# Heuristic guards against the micro-contract failure mode: instruments with
# only a handful of rolls (e.g. recently-launched micro grains) produce a
# back-adjusted series with a huge leading spike that ruins the y-axis. Drop
# the symbol entirely if it's too short or the series spans more than an order
# of magnitude — that range is wider than any real, healthy futures curve.
# (The ratio guard applies to futures only: a growth stock can legitimately
# span 10x over its history.)
MIN_HISTORY_POINTS = 150
MAX_BACK_ADJ_RATIO = 10.0

EMA_STUDIES = '[{"type":"ema","period":50},{"type":"ema","period":100}]'


def invalidate_cache() -> None:
    global _cache_filled_at
    _META_CACHE.clear()
    _CHART_CACHE.clear()
    _CORR_CACHE.clear()
    _cache_filled_at = None


def _expire_stale() -> None:
    """Drop all caches once they outlive the TTL (counted from first fill)."""
    if _cache_filled_at is not None and time.monotonic() - _cache_filled_at > _CACHE_TTL_S:
        invalidate_cache()


def _mark_filled() -> None:
    global _cache_filled_at
    if _cache_filled_at is None:
        _cache_filled_at = time.monotonic()


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    """Return the first non-empty value among the given keys."""
    for k in keys:
        v = d.get(k)
        if v is not None and v != "" and not (isinstance(v, float) and v != v):
            return v
    return None


# ── universe metadata ────────────────────────────────────────────────────────

def _read_universe(target_names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Return ``{SYMBOL: {name, sector, exchange, currency, multiplier, …}}``
    merged from every ``universe/<symbol>`` whose name matches (case-insensitive).
    """
    try:
        usyms = public_access.list_symbols("universe")
    except Exception:  # noqa: BLE001
        return {}
    targets = [s for s in usyms if s.lower() in target_names]
    out: dict[str, dict[str, Any]] = {}
    for target in targets:
        try:
            df = public_access.read_data("universe", target)
        except Exception:  # noqa: BLE001
            continue
        key_col = next((c for c in ("symbol", "ibkr_symbol", "ticker", "name") if c in df.columns), None)
        if key_col is None:
            continue
        for _, row in df.iterrows():
            sym = str(row[key_col]).strip().upper()
            if not sym or sym in out:
                continue
            out[sym] = {str(k): (None if pd.isna(v) else v) for k, v in row.items() if k != key_col}
    return out


def _meta_for(sym: str, uni: dict[str, dict[str, Any]], asset_type: str = "future") -> dict[str, Any]:
    """Normalise a universe row down to the canonical fields the UI uses."""
    meta = uni.get(sym.upper(), {})
    sector = _first(meta, SECTOR_COLS) or ("Equities" if asset_type == "equity" else "Other")
    mult = _first(meta, MULTIPLIER_COLS)
    try:
        mult = float(mult) if mult is not None else None
    except (TypeError, ValueError):
        mult = None
    if mult is None and asset_type == "equity":
        mult = 1.0  # point value of a share
    name = str(_first(meta, NAME_COLS) or sym)
    return {
        "name": name,
        "sector": str(sector),
        "exchange": str(meta.get("exchange") or ""),
        "currency": str(meta.get("currency") or ""),
        "multiplier": mult,
        "is_micro": asset_type == "future" and "micro" in name.lower(),
        "asset_type": asset_type,
    }


# ── Symbol roster (futures + equities) ───────────────────────────────────────

def _list_equity_symbols() -> dict[str, str]:
    """``{SYMBOL: library}`` across the allowlisted equity libraries.

    First library wins a duplicate; futures-root collisions are resolved by the
    caller (futures win).
    """
    out: dict[str, str] = {}
    for lib in EQUITY_LIBRARIES:
        if not public_access.is_public(lib):
            continue
        try:
            syms = public_access.list_symbols(lib)
        except Exception:  # noqa: BLE001
            continue
        for s in syms:
            out.setdefault(s, lib)
    return out


def _in_subset(meta: dict[str, Any], subset: str) -> bool:
    """The default ``micro`` subset = micro futures + all equities — we're
    starting with micro contracts only, and equities size down to one share."""
    if subset != "micro":
        return True
    return meta["asset_type"] == "equity" or meta["is_micro"]


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


def _equity_ohlc(df: pd.DataFrame) -> pd.DataFrame | None:
    """Single-index daily OHLC → the ``h``/``l``/``c`` frame ATR expects."""
    cols = {c.lower(): c for c in df.columns}
    if not all(k in cols for k in ("high", "low", "close")):
        return None
    return pd.DataFrame({
        "h": pd.to_numeric(df[cols["high"]], errors="coerce"),
        "l": pd.to_numeric(df[cols["low"]], errors="coerce"),
        "c": pd.to_numeric(df[cols["close"]], errors="coerce"),
    }, index=df.index).dropna()


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


def _curve_is_usable(curve_main: dict | None, check_ratio: bool = True) -> bool:
    """Drop symbols whose series is too short or (futures) out of scale.

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
    if not check_ratio:
        return True
    first, last = abs(valid[0]), abs(valid[-1])
    if first == 0 or last == 0:
        return False
    ratio = max(first / last, last / first)
    return ratio <= MAX_BACK_ADJ_RATIO


def _trend_from_chart(curve_main: dict) -> tuple[float | None, float | None, int]:
    """(last_close, trend_pct, trend_signal) from a close + EMA50/100 chart."""
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
    return last_close, trend_pct, trend_signal


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
            "studies": EMA_STUDIES,
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

    last_close, trend_pct, trend_signal = _trend_from_chart(curve_main)

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


def _compute_for_equity(symbol: str, library: str) -> dict[str, Any] | None:
    """Close + EMA50/100 chart, trend and ATR(100) for a single-index equity."""
    try:
        df = public_access.read_data(library, symbol)
    except Exception:  # noqa: BLE001
        return None
    if ac.detect_multiindex_contracts(df) or not isinstance(df.index, pd.DatetimeIndex):
        return None
    cols = {c.lower(): c for c in df.columns}
    col = cols.get("close") or next(
        (c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])), None)
    if col is None:
        return None

    curve_main = None
    try:
        curve_main, _, curve_err = ac.build_chart(df, symbol, {
            "chart_type": "line",
            "x_col": "__index__",
            "y_cols": col,
            "studies": EMA_STUDIES,
        })
        if curve_err:
            curve_main = None
    except Exception:  # noqa: BLE001
        curve_main = None

    if not _curve_is_usable(curve_main, check_ratio=False):
        return None

    last_close, trend_pct, trend_signal = _trend_from_chart(curve_main)

    try:
        atr100 = _atr100(_equity_ohlc(df))
    except Exception:  # noqa: BLE001
        atr100 = None

    return {
        "symbol": symbol,
        "curve_chart": curve_main,
        "term_chart": None,  # equities have no forward curve
        "last": last_close,
        "trend_pct": trend_pct,
        "trend_signal": trend_signal,
        "atr100": atr100,
    }


# ── Top-level builders ───────────────────────────────────────────────────────

def build_meta() -> dict[str, Any]:
    """Lightweight shell data for /futures: sector groups with metadata only.

    Cheap: one read of universe metadata + symbol lists. No per-symbol chart
    payloads, so the page renders instantly; JS then fetches
    /futures/api/payload to fill in trend numbers, ATR, and the charts.
    """
    _expire_stale()
    if "data" in _META_CACHE:
        return _META_CACHE["data"]

    if not ensure_connected():
        return {"sectors": [], "rows": [], "error": "The data engine is not connected in this environment."}

    fut_err: str | None = None
    try:
        fut_symbols = sorted(public_access.list_symbols("futures"))
    except Exception as e:  # noqa: BLE001
        fut_symbols, fut_err = [], str(e)

    eq_map = _list_equity_symbols()
    if fut_err and not eq_map:
        return {"sectors": [], "rows": [], "error": f"Could not list the futures library: {fut_err}"}

    uni_fut = _read_universe(UNIVERSE_FUTURES_SYMBOLS)
    uni_eq = _read_universe(UNIVERSE_EQUITY_SYMBOLS)

    rows: list[dict[str, Any]] = []
    for s in fut_symbols:
        meta = _meta_for(s, uni_fut, "future")
        rows.append({"symbol": s, **{k: meta[k] for k in (
            "name", "sector", "exchange", "currency", "multiplier", "is_micro", "asset_type")}})
    fut_set = set(fut_symbols)
    for s in sorted(eq_map):
        if s in fut_set:  # a futures root always wins a name clash
            continue
        meta = _meta_for(s, uni_eq, "equity")
        rows.append({"symbol": s, **{k: meta[k] for k in (
            "name", "sector", "exchange", "currency", "multiplier", "is_micro", "asset_type")}})

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
    _mark_filled()
    return out


def _payload_entry_for(symbol: str, asset_type: str, library: str,
                       uni: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Compute or fetch the per-symbol payload entry, using the per-symbol
    cache so a later ``subset='all'`` request only computes the rest.
    """
    if symbol in _CHART_CACHE:
        return _CHART_CACHE[symbol]
    if asset_type == "equity":
        data = _compute_for_equity(symbol, library)
    else:
        data = _compute_for_symbol(symbol)
    if data is None:
        _CHART_CACHE[symbol] = None
        _mark_filled()
        return None
    meta = _meta_for(symbol, uni, asset_type)
    entry: dict[str, Any] = {
        "last": data["last"],
        "trend_pct": data["trend_pct"],
        "trend_signal": data["trend_signal"],
        "atr100": data["atr100"],
        "multiplier": meta["multiplier"],
        "is_micro": meta["is_micro"],
        "asset_type": asset_type,
        "curve_chart": data["curve_chart"],
        "term_chart": data["term_chart"],
    }
    _CHART_CACHE[symbol] = entry
    _mark_filled()
    return entry


def _roster(subset: str, uni_fut: dict, uni_eq: dict) -> list[tuple[str, str, str, dict]]:
    """``[(symbol, asset_type, library, universe)]`` honouring the subset."""
    out: list[tuple[str, str, str, dict]] = []
    try:
        fut_symbols = sorted(public_access.list_symbols("futures"))
    except Exception:  # noqa: BLE001
        fut_symbols = []
    for s in fut_symbols:
        if _in_subset(_meta_for(s, uni_fut, "future"), subset):
            out.append((s, "future", "futures", uni_fut))
    fut_set = set(fut_symbols)
    eq_map = _list_equity_symbols()
    for s in sorted(eq_map):
        if s in fut_set:
            continue
        if _in_subset(_meta_for(s, uni_eq, "equity"), subset):
            out.append((s, "equity", eq_map[s], uni_eq))
    return out


def build_chart_payload(subset: str = "all") -> dict[str, Any]:
    """Per-symbol chart payloads + trend + ATR.

    ``subset='micro'`` computes only the micro futures plus all equities —
    much cheaper on cold start, used as the default page load.
    ``subset='all'`` fills in the rest. Per-symbol cache means asking for
    ``'all'`` after ``'micro'`` only does the incremental work.
    """
    _expire_stale()
    if not ensure_connected():
        return {}

    uni_fut = _read_universe(UNIVERSE_FUTURES_SYMBOLS)
    uni_eq = _read_universe(UNIVERSE_EQUITY_SYMBOLS)

    out: dict[str, Any] = {}
    for sym, asset_type, library, uni in _roster(subset, uni_fut, uni_eq):
        entry = _payload_entry_for(sym, asset_type, library, uni)
        if entry is not None:
            out[sym] = entry
    return out


def build_correlations(subset: str = "micro", window: int = 250) -> dict[str, Any]:
    """Pairwise correlation of daily returns across the subset's markets.

    Returns ``{"symbols": [...], "matrix": [[r or None]], "window": n,
    "n_obs": {sym: count}}``. Futures use the back-adjusted continuous close
    (the same series the charts show); equities use close. Correlations are
    computed pairwise on the last ``window`` trading days with at least 40
    overlapping observations — pairs below that come back as ``None``.

    Feeds the portfolio builder in the simulator: candidates are scored by
    their (side-adjusted) average correlation against the current book.
    """
    _expire_stale()
    if not ensure_connected():
        return {"symbols": [], "matrix": [], "window": window, "n_obs": {}}

    key = (subset, window)
    if key in _CORR_CACHE:
        return _CORR_CACHE[key]

    uni_fut = _read_universe(UNIVERSE_FUTURES_SYMBOLS)
    uni_eq = _read_universe(UNIVERSE_EQUITY_SYMBOLS)

    returns: dict[str, pd.Series] = {}
    for sym, asset_type, library, uni in _roster(subset, uni_fut, uni_eq):
        entry = _payload_entry_for(sym, asset_type, library, uni)
        if not entry or not entry.get("curve_chart"):
            continue
        chart = entry["curve_chart"]
        x = chart.get("x_values") or []
        data = (chart.get("datasets") or [{}])[0].get("data") or []
        if len(x) != len(data):
            continue
        s = pd.Series(data, index=pd.to_datetime(x, errors="coerce"), dtype=float)
        s = s[s.index.notna()].dropna()
        s = s[~s.index.duplicated(keep="last")].sort_index()
        r = s.pct_change().dropna()
        if len(r) >= 40:
            returns[sym] = r

    if not returns:
        out: dict[str, Any] = {"symbols": [], "matrix": [], "window": window, "n_obs": {}}
    else:
        df = pd.DataFrame(returns).tail(window)
        corr = df.corr(min_periods=40)
        symbols = [str(c) for c in corr.columns]
        matrix = [[None if pd.isna(v) else round(float(v), 3) for v in row] for row in corr.values]
        out = {
            "symbols": symbols,
            "matrix": matrix,
            "window": window,
            "n_obs": {str(c): int(df[c].count()) for c in df.columns},
        }

    _CORR_CACHE[key] = out
    _mark_filled()
    return out
