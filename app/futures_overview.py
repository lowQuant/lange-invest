"""Data layer for the /futures overview page.

Reads ONLY the public ``futures`` library and the metadata symbol
``universe/Futures``, computes per-symbol chart data (continuous back-adjusted
close with EMA50/100 and the latest forward curve), plus a trend signal and an
ATR(100) estimate used by the position-sizing tab.

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

import math
import os
import time
from typing import Any

import numpy as np
import pandas as pd

from app import arctic_charting as ac
from app import public_access
from app import strategy_cache
from app.engine import ensure_connected


_META_CACHE: dict[str, Any] = {}
# Per-symbol payload cache. Negative results (unusable curves) are cached as
# ``None`` so we don't retry the expensive read on every page refresh.
_CHART_CACHE: dict[str, dict[str, Any] | None] = {}
# Correlation matrices keyed by (subset, window).
_CORR_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
# universe/Futures metadata, cached so batch payload calls don't re-read it.
_UNI_CACHE: dict[str, Any] = {}
# Full combined-universe records (outright + spread instruments, both trend
# variants) — the expensive, sizing-independent compute. Persisted to disk by
# ``strategy_cache`` and held in-process within the TTL so per-request sizing is
# cheap. ``{"fingerprint", "instruments", "as_of", ...}``.
_STRAT_UNIVERSE_CACHE: dict[str, Any] = {}
# c1−c2 spread chart payloads, built lazily for the markets actually displayed.
_STRAT_SPREAD_CHART_CACHE: dict[str, dict[str, Any] | None] = {}

# TTL so direct-to-ArcticDB writes (scripts, other processes) surface without a
# web-process restart. Writes through this app's /mcp endpoint invalidate
# immediately; the TTL is the safety net for everything else.
_CACHE_TTL_S = float(os.getenv("FUTURES_CACHE_TTL", "900"))
_cache_filled_at: float | None = None

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

# ── Donchian + loser-filter strategy (members-only "Strategy" tab) ───────────
# Donchian-50 breakout entry + Donchian-20 opposite-channel exit, with an
# *optional* SMA(200) trend gate (off by default — the combined-universe
# backtest showed it neither lifts Sharpe nor cuts drawdown once the loser
# filter is on) and a "loser filter": after a *winning* closed trade on a market
# the next signal is skipped; only a loss (or no prior trade) re-arms it. Runs on
# the COMBINED universe — every market's outright AND its c1−c2 calendar spread
# are independent instruments carrying the same signal. Recomputed from the
# back-adjusted continuous series and cached to disk per the futures fingerprint.
# The 3·ATR catastrophe stop is surfaced as a level for open positions but is NOT
# simulated in the historical replay (the 20-day channel governs exits here), so
# the series stays reproducible from the single curve we already read.
STRAT_ENTRY = 50
STRAT_EXIT = 20
STRAT_SMA = 200
STRAT_STOP_ATR = 3.0
# A c1−c2 calendar spread joins the tradable universe only if its own signal has
# fired at least this many times historically (thin track records are dropped).
STRAT_MIN_SPREAD_FIRINGS = 10
# Bumped whenever the cached per-instrument record schema changes, so an old
# cache file (missing new fields) is recomputed instead of served stale.
STRAT_SCHEMA_VERSION = 2
STRAT_RISK_DEFAULT = 0.001
STRAT_ACCOUNT_DEFAULT = 150_000.0
# Exposure caps as multiples of account equity (per-position / per-side / total).
STRAT_CAP_POS = 1.0
STRAT_CAP_SIDE = 2.0
STRAT_CAP_TOTAL = 4.0
STRAT_MARGIN_PCT = 0.10


def invalidate_cache() -> None:
    global _cache_filled_at
    _META_CACHE.clear()
    _CHART_CACHE.clear()
    _CORR_CACHE.clear()
    _UNI_CACHE.clear()
    _STRAT_UNIVERSE_CACHE.clear()
    _STRAT_SPREAD_CHART_CACHE.clear()
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


# ── universe/Futures metadata ────────────────────────────────────────────────

def _read_universe_futures() -> dict[str, dict[str, Any]]:
    """Return ``{SYMBOL: {name, sector, exchange, currency, multiplier, …}}``.

    Cached for the life of the TTL so a burst of batch payload calls doesn't
    re-read universe/Futures on every request.
    """
    if "data" in _UNI_CACHE:
        return _UNI_CACHE["data"]
    out = _load_universe_futures()
    _UNI_CACHE["data"] = out
    _mark_filled()
    return out


def _load_universe_futures() -> dict[str, dict[str, Any]]:
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
    _expire_stale()
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
    _mark_filled()
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
        _mark_filled()
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
    _mark_filled()
    return entry


def _subset_symbols(subset: str, uni: dict[str, dict[str, Any]]) -> list[str]:
    try:
        symbols = sorted(public_access.list_symbols("futures"))
    except Exception:  # noqa: BLE001
        return []
    if subset == "micro":
        symbols = [s for s in symbols if _meta_for(s, uni)["is_micro"]]
    return symbols


def build_chart_payload(subset: str = "all", symbols: list[str] | None = None) -> dict[str, Any]:
    """Per-symbol chart payloads + trend + ATR.

    ``symbols`` (an explicit list) takes precedence and computes just those —
    this is the batch/progressive-load path: the client asks for a handful of
    markets at a time so the first paint is fast and the rest stream in.
    Otherwise ``subset='micro'`` computes only the markets whose universe
    ``name`` contains "micro" and ``subset='all'`` the whole library. The
    per-symbol cache means repeated/overlapping requests only do new work once.
    """
    _expire_stale()
    if not ensure_connected():
        return {}

    uni = _read_universe_futures()
    if symbols is not None:
        try:
            available = set(public_access.list_symbols("futures"))
        except Exception:  # noqa: BLE001
            available = set()
        target = [s for s in symbols if s in available]
    else:
        target = _subset_symbols(subset, uni)

    out: dict[str, Any] = {}
    for s in target:
        entry = _payload_entry_for(s, uni)
        if entry is not None:
            out[s] = entry
    return out


def build_correlations(subset: str = "micro", window: int = 250) -> dict[str, Any]:
    """Pairwise correlation of daily returns across the subset's markets.

    Returns ``{"symbols": [...], "matrix": [[r or None]], "window": n,
    "n_obs": {sym: count}}``. Built from the back-adjusted continuous close
    (the same series the charts show), pairwise on the last ``window`` trading
    days with at least 40 overlapping observations — pairs below that come
    back as ``None``.

    Feeds the (optional) portfolio builder in the simulator.
    """
    _expire_stale()
    if not ensure_connected():
        return {"symbols": [], "matrix": [], "window": window, "n_obs": {}}

    key = (subset, window)
    if key in _CORR_CACHE:
        return _CORR_CACHE[key]

    uni = _read_universe_futures()
    returns: dict[str, pd.Series] = {}
    for sym in _subset_symbols(subset, uni):
        entry = _payload_entry_for(sym, uni)
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


# ── b08 strategy signals (members-only) ──────────────────────────────────────

def _close_series_from_entry(entry: dict[str, Any]) -> pd.Series:
    """Clean daily back-adjusted close series from a payload entry's curve."""
    chart = entry.get("curve_chart") or {}
    x = chart.get("x_values") or []
    data = (chart.get("datasets") or [{}])[0].get("data") or []
    if not x or len(x) != len(data):
        return pd.Series(dtype=float)
    s = pd.Series(data, index=pd.to_datetime(x, errors="coerce"), dtype=float)
    s = s[s.index.notna()].dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def _strategy_signal(close: pd.Series, use_trend: bool = False) -> np.ndarray:
    """Per-bar target position (+1/-1/0) for the Donchian state machine.

    Long while a fresh 50-day high prints (optionally above SMA(200)) and held
    until a 20-day low; short symmetrically. No look-ahead — the channels
    include the current bar, i.e. the rule is "today made a new N-day extreme".

    ``use_trend`` toggles the 200-day SMA gate on entries (and the trend-flip
    exit). The combined-universe backtest showed the gate neither lifts Sharpe
    nor cuts drawdown once the loser filter is on, so it defaults **off**;
    members can switch it back on for choppy regimes.
    """
    n = len(close)
    out = np.zeros(n, dtype=np.int8)
    # Without the trend gate we only need ENTRY history; with it we need SMA too.
    min_bars = (STRAT_SMA if use_trend else STRAT_ENTRY) + 1
    if n < min_bars:
        return out
    c = close.to_numpy(dtype=float)
    hi_e = close.rolling(STRAT_ENTRY).max().to_numpy()
    lo_e = close.rolling(STRAT_ENTRY).min().to_numpy()
    hi_x = close.rolling(STRAT_EXIT).max().to_numpy()
    lo_x = close.rolling(STRAT_EXIT).min().to_numpy()
    sma = close.rolling(STRAT_SMA).mean().to_numpy() if use_trend else None
    state = 0
    for i in range(n):
        ci = c[i]
        if state == 0:
            if use_trend:
                if sma[i] == sma[i]:  # not NaN
                    if ci >= hi_e[i] and ci > sma[i]:
                        state = 1
                    elif ci <= lo_e[i] and ci < sma[i]:
                        state = -1
            else:
                if ci >= hi_e[i] and hi_e[i] == hi_e[i]:
                    state = 1
                elif ci <= lo_e[i] and lo_e[i] == lo_e[i]:
                    state = -1
        elif state == 1:
            if ci <= lo_x[i] or (use_trend and sma[i] == sma[i] and ci < sma[i]):
                state = 0
        else:  # state == -1
            if ci >= hi_x[i] or (use_trend and sma[i] == sma[i] and ci > sma[i]):
                state = 0
        out[i] = state
    return out


def _count_firings(target: np.ndarray) -> int:
    """Number of fresh entries in a raw target series (0/flip → nonzero).

    Used to apply the spec's "minimum spread firings to include" gate: a c1−c2
    spread only joins the tradable universe if its own signal has fired at least
    ``STRAT_MIN_SPREAD_FIRINGS`` times historically — otherwise it has too thin
    a track record to trust as a standalone instrument.
    """
    if len(target) == 0:
        return 0
    prev = 0
    n = 0
    for t in target:
        ti = int(t)
        if ti != 0 and ti != prev:
            n += 1
        prev = ti
    return n


def _replay_loser_filter(close: pd.Series, target: np.ndarray) -> tuple[int, int, float, int]:
    """Replay the signal with the loser-filter entry gate.

    Returns ``(current_dir, entry_index, entry_px, last_result)`` for the
    position held on the final bar. A new entry is taken only if the most recent
    *closed* trade on this market lost (``last_result != 1``) or there was none
    yet; after a winner the market is held flat and the signal keeps being
    blocked until it resets — the mechanic that progressively reduces turnover.
    """
    c = close.to_numpy(dtype=float)
    actual = 0
    entry_px = float("nan")
    entry_i = -1
    last_result = 0
    for i in range(len(c)):
        t = int(target[i])
        if t != actual:
            if actual != 0:  # close the open trade, record win/loss
                realised = actual * (c[i] - entry_px)
                last_result = 1 if realised > 0 else -1
                actual, entry_px, entry_i = 0, float("nan"), -1
            if t != 0 and last_result != 1:  # loser-filter gate
                actual, entry_px, entry_i = t, c[i], i
    return actual, entry_i, entry_px, last_result


def _states_both(series: pd.Series) -> dict[str, Any]:
    """Final loser-filtered position for *both* trend variants + firing count.

    Computing trend-on and trend-off in one pass means a member toggling the
    200-day gate never triggers a re-read or re-replay — only a re-pick of the
    already-cached state. Firings are counted on the headline (no-trend) target.
    """
    out: dict[str, Any] = {}
    firings = 0
    for key, use_trend in (("notrend", False), ("trend", True)):
        target = _strategy_signal(series, use_trend=use_trend)
        if not use_trend:
            firings = _count_firings(target)
        direction, entry_i, entry_px, last_result = _replay_loser_filter(series, target)
        out[key] = {
            # dir = actual position after the loser filter; raw_dir = the bare
            # Donchian breakout state on the last bar (what fired, ignoring the
            # filter); last_result = win/loss of the last closed trade, which is
            # what decides whether a fresh breakout is taken or stood aside.
            "dir": int(direction),
            "raw_dir": int(target[-1]) if len(target) else 0,
            "last_result": int(last_result),
            "entry_px": None if entry_i < 0 else float(entry_px),
            "entry_date": None if entry_i < 0 else series.index[entry_i].date().isoformat(),
        }
    out["firings"] = firings
    return out


def _outright_record(sym: str, entry: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any] | None:
    """Per-instrument signal record for a market's outright continuous series."""
    close = _close_series_from_entry(entry)
    if len(close) < STRAT_ENTRY + STRAT_EXIT:
        return None
    states = _states_both(close)
    atr, last, mult = entry.get("atr100"), entry.get("last"), meta["multiplier"]
    return {
        "id": sym, "kind": "outright", "sym": sym,
        "name": meta["name"], "sector": meta["sector"], "is_micro": meta["is_micro"],
        "last": None if last is None else float(last),
        "atr": None if atr is None else float(atr),
        "mult": mult,
        "avg_dollar_move": (float(atr) * mult) if (atr and mult) else None,
        "as_of": close.index[-1].date().isoformat(),
        "notrend": states["notrend"], "trend": states["trend"], "firings": states["firings"],
    }


# ── c1−c2 calendar-spread fallback (too-large / too-volatile markets) ─────────

def _continuous_spread(df: pd.DataFrame, col: str) -> pd.Series:
    """Roll-adjusted continuous c1−c2 calendar spread (front minus second by DTE).

    A raw rank1−rank2 series steps at every roll because the *pair* changes,
    which both inflates the ATR and prints false breakouts. We additively
    back-adjust at each roll (spreads are price differences, so the correction is
    additive) to get a continuous series whose moves reflect the spread tightening
    / widening, not the roll. Requires a ``dte`` column to rank by expiry.
    """
    if "dte" not in df.columns:
        return pd.Series(dtype=float)
    sub = df.loc[df["dte"] > 0, [col, "dte"]]
    sub = sub[sub[col].notna()]
    if len(sub) < 2:
        return pd.Series(dtype=float)

    # Vectorised front/second extraction: sort by (date, dte), take ranks 0 and 1
    # within each date. Much cheaper than a per-date .loc over thousands of days.
    flat = pd.DataFrame({
        "date": sub.index.get_level_values(0),
        "sym": sub.index.get_level_values(1),
        "px": sub[col].to_numpy(dtype=float),
        "dte": sub["dte"].to_numpy(dtype=float),
    }).sort_values(["date", "dte"], kind="mergesort")
    flat["rank"] = flat.groupby("date").cumcount()
    c1 = flat[flat["rank"] == 0].set_index("date")
    c2 = flat[flat["rank"] == 1].set_index("date")
    pair = c1[["sym", "px"]].join(c2[["sym", "px"]], lsuffix="1", rsuffix="2", how="inner").sort_index()
    n = len(pair)
    if n == 0:
        return pd.Series(dtype=float)

    dates = pair.index.to_list()
    sym1 = pair["sym1"].to_list()
    sym2 = pair["sym2"].to_list()
    spread_raw = (pair["px1"] - pair["px2"]).to_numpy(dtype=float)
    if n < 2:
        return pd.Series(spread_raw, index=dates, dtype=float)

    # Roll lookups (~monthly) read the previous pair's prices on the roll date
    # from the full frame, including contracts already past their DTE-rank.
    price_map = df[col].to_dict()
    adj = [0.0] * n
    for i in range(n - 1, 0, -1):
        adj[i - 1] = adj[i]
        if sym1[i] == sym1[i - 1]:  # same front contract → no roll
            continue
        p1 = price_map.get((dates[i], sym1[i - 1]))
        p2 = price_map.get((dates[i], sym2[i - 1]))
        if p1 is None or p2 is None or pd.isna(p1) or pd.isna(p2):
            old_spread = float(spread_raw[i - 1])
        else:
            old_spread = float(p1) - float(p2)
        adj[i - 1] = adj[i] + (float(spread_raw[i]) - old_spread)
    return pd.Series([spread_raw[i] + adj[i] for i in range(n)], index=dates, dtype=float)


def _spread_series(sym: str) -> pd.Series:
    """Roll-adjusted continuous c1−c2 calendar-spread series for ``sym``.

    A relative-value series with a fraction of the outright's notional and
    volatility, so markets that can't be sized one-lot outright can still be
    traded as a spread. Built from the per-contract MultiIndex in the public
    ``futures`` library.
    """
    try:
        df = public_access.read_data("futures", sym)
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=float)
    if not ac.detect_multiindex_contracts(df):
        return pd.Series(dtype=float)
    col = "close" if "close" in df.columns else next(
        (c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])), None)
    if col is None:
        return pd.Series(dtype=float)
    try:
        s = _continuous_spread(df, col)
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=float)
    if s.empty:
        return s
    s = pd.Series(s.values, index=pd.to_datetime(s.index, errors="coerce"), dtype=float)
    s = s[s.index.notna()].dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def _spread_atr(spread: pd.Series) -> float | None:
    """Close-to-close ATR(100) proxy for the spread (no per-leg high/low, so
    true range collapses to |Δclose| — the standard choice for a spread)."""
    if len(spread) < 20:
        return None
    tr = spread.diff().abs()
    atr = tr.rolling(100, min_periods=20).mean().dropna()
    return float(atr.iloc[-1]) if len(atr) else None


def _spread_record(sym: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    """Per-instrument signal record for a market's c1−c2 calendar spread.

    The spread is treated as a *standalone* instrument running the same Donchian
    + loser-filter signal on its own roll-adjusted series. It joins the universe
    only if its signal has fired at least ``STRAT_MIN_SPREAD_FIRINGS`` times — a
    thin track record is dropped rather than trusted.
    """
    spread = _spread_series(sym)
    if len(spread) < STRAT_ENTRY + STRAT_EXIT:
        return None
    states = _states_both(spread)
    if states["firings"] < STRAT_MIN_SPREAD_FIRINGS:
        return None
    atr, last, mult = _spread_atr(spread), float(spread.iloc[-1]), meta["multiplier"]
    return {
        "id": sym + "|SP", "kind": "spread", "sym": sym,
        "name": meta["name"], "sector": meta["sector"], "is_micro": meta["is_micro"],
        "last": last,
        "atr": None if atr is None else float(atr),
        "mult": mult,
        "avg_dollar_move": (float(atr) * mult) if (atr and mult) else None,
        "as_of": spread.index[-1].date().isoformat(),
        "notrend": states["notrend"], "trend": states["trend"], "firings": states["firings"],
    }


def _spread_chart(sym: str) -> dict[str, Any] | None:
    """LangeChart line spec for a market's c1−c2 spread (built/cached lazily)."""
    if sym in _STRAT_SPREAD_CHART_CACHE:
        return _STRAT_SPREAD_CHART_CACHE[sym]
    spread = _spread_series(sym)
    chart = None
    if not spread.empty:
        s = spread.tail(1000)
        chart = {
            "title": f"{sym} · c1−c2 spread", "chart_type": "line", "x_label": "Date",
            "x_values": [d.date().isoformat() for d in s.index],
            "datasets": [{"label": "c1−c2", "data": [float(v) for v in s.to_numpy()]}],
        }
    _STRAT_SPREAD_CHART_CACHE[sym] = chart
    _mark_filled()
    return chart


# ── Full combined universe: compute, fingerprint, persistent cache ────────────

def _futures_fingerprint(symbols: list[str]) -> str | None:
    """Cheap content fingerprint of the ``futures`` library (metadata only).

    Hashes each symbol's row count, last-update time and date range — enough to
    detect *any* write/append without reading a single price. A matching
    fingerprint means the cached compute is still exact; a changed one means new
    data and triggers a recompute.
    """
    import hashlib

    parts: list[str] = []
    for s in symbols:
        try:
            d = public_access.describe_symbol("futures", s)
        except Exception:  # noqa: BLE001 — a symbol we can't describe just drops out
            continue
        parts.append(f"{s}|{d.get('rows')}|{d.get('last_update')}|{d.get('date_range')}")
    if not parts:
        return None
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    # Prefix the schema version so a record-shape change invalidates old caches
    # even when the underlying data is byte-for-byte identical.
    return f"v{STRAT_SCHEMA_VERSION}:{digest}"


def _compute_universe_records() -> dict[str, Any]:
    """The expensive bit: signal records for the full combined universe.

    For every market we build BOTH an outright instrument and (where it has a
    deep enough track record) its c1−c2 calendar-spread instrument, each running
    the Donchian + loser-filter signal in parallel. Sizing-independent, so the
    result is cached to disk and reused until the data changes.
    """
    uni = _read_universe_futures()
    try:
        symbols = sorted(public_access.list_symbols("futures"))
    except Exception:  # noqa: BLE001
        symbols = []

    instruments: list[dict[str, Any]] = []
    as_of: str | None = None
    for sym in symbols:
        meta = _meta_for(sym, uni)
        entry = _payload_entry_for(sym, uni)
        if entry and entry.get("curve_chart"):
            rec = _outright_record(sym, entry, meta)
            if rec:
                instruments.append(rec)
                if as_of is None or rec["as_of"] > as_of:
                    as_of = rec["as_of"]
        srec = _spread_record(sym, meta)
        if srec:
            instruments.append(srec)
            if as_of is None or srec["as_of"] > as_of:
                as_of = srec["as_of"]
    return {"instruments": instruments, "as_of": as_of}


def _strategy_universe(force: bool = False) -> dict[str, Any] | None:
    """Combined-universe records, via the in-memory → disk → compute cascade.

    Within the in-process TTL the in-memory copy is reused with no re-read at
    all. Otherwise we fingerprint the ``futures`` library and hand off to the
    disk cache, which recomputes only when the fingerprint changed.
    """
    if not force and _STRAT_UNIVERSE_CACHE.get("instruments") is not None:
        # In-memory hit within the TTL — no recompute happened, so report it as a
        # cache serve (the heavy work ran in an earlier call this session).
        _STRAT_UNIVERSE_CACHE["source"] = "cache"
        return _STRAT_UNIVERSE_CACHE
    try:
        symbols = sorted(public_access.list_symbols("futures"))
    except Exception:  # noqa: BLE001
        return None
    fingerprint = _futures_fingerprint(symbols)
    payload = strategy_cache.get_or_compute(
        "futures_strategy_universe", fingerprint, _compute_universe_records, force=force)
    _STRAT_UNIVERSE_CACHE.clear()
    _STRAT_UNIVERSE_CACHE.update(payload)
    _mark_filled()
    return payload


def build_strategy_charts(ids: list[str]) -> dict[str, Any]:
    """Chart payloads for the displayed instruments, built lazily by id.

    ``id`` is the symbol for an outright and ``SYM|SP`` for its calendar spread.
    Outright curves reuse the gallery's back-adjusted payload; spreads build a
    line of their roll-adjusted series. Only the markets actually on screen are
    requested, so this stays bounded even over the full library.
    """
    _expire_stale()
    if not ensure_connected():
        return {}
    uni = _read_universe_futures()
    out: dict[str, Any] = {}
    for iid in ids:
        if iid.endswith("|SP"):
            sym, kind = iid[:-3], "spread"
            chart = _spread_chart(sym)
        else:
            sym, kind = iid, "outright"
            entry = _payload_entry_for(sym, uni)
            chart = entry.get("curve_chart") if entry else None
        if chart:
            out[iid] = {"id": iid, "sym": sym, "kind": kind, "chart": chart}
    return out


def build_strategy_signals(account: float = STRAT_ACCOUNT_DEFAULT,
                           risk: float = STRAT_RISK_DEFAULT,
                           use_trend: bool = False,
                           min_bps: float | None = None,
                           max_bps: float | None = None,
                           force: bool = False) -> dict[str, Any]:
    """All live signals of the Donchian + loser-filter strategy, classified.

    Runs on the **combined universe** — every market's outright AND its c1−c2
    calendar spread are independent instruments carrying their own signal. The
    expensive replay is cached (per the ``futures`` fingerprint); this call picks
    the chosen trend variant and returns *every instrument with a live signal*,
    not just the ones in the book.

    Each row is classified two ways so the UI can group them:

      * ``kind``    — ``outright`` (single contract) vs ``spread`` (synthetic).
      * ``in_book`` — whether the loser filter is letting us hold it. A live
        breakout whose **last closed trade won** is stood aside (``in_book`` is
        False, ``last_result`` 1); one whose last trade **lost** or never traded
        is armed and held (``in_book`` True). This is the "previous was a
        loser / winner" split.

    Only the in-book rows are *sized* (with the per-position 1× / per-side 2× /
    total 4× caps); stood-aside rows carry the signal and its avg daily $ move
    but no contracts. ``use_trend`` toggles the optional 200-day SMA gate
    (default off). ``min_bps`` / ``max_bps`` filter every row by its average
    daily $ move as bps of the account (the universe is always computed in full).
    Returns ``{positions, summary, as_of, source, computed_at, error}``.
    """
    _expire_stale()
    if not ensure_connected():
        return {"positions": [], "summary": {}, "as_of": None,
                "error": "The data engine is not connected in this environment."}
    payload = _strategy_universe(force=force)
    if payload is None:
        return {"positions": [], "summary": {}, "as_of": None,
                "error": "Could not list the futures library."}

    variant = "trend" if use_trend else "notrend"
    instruments = payload.get("instruments", [])
    as_of = payload.get("as_of")

    # Every instrument with a live signal (a breakout fired, or we hold one).
    rows: list[dict[str, Any]] = []
    for r in instruments:
        st = r.get(variant) or {}
        raw_dir = int(st.get("raw_dir", 0))
        in_dir = int(st.get("dir", 0))
        if raw_dir == 0 and in_dir == 0:
            continue  # genuinely flat — not a signal
        move = r.get("avg_dollar_move")
        bps = (move / account * 1e4) if (move and account > 0) else None
        rows.append({
            "id": r["id"], "symbol": r["sym"], "name": r["name"], "sector": r["sector"],
            "is_micro": r["is_micro"], "kind": r["kind"],
            "signal": raw_dir,                 # bare Donchian direction
            "side": in_dir if in_dir != 0 else raw_dir,
            "in_book": in_dir != 0,            # loser filter is holding it
            "last_result": int(st.get("last_result", 0)),
            "entry_px": st.get("entry_px"), "entry_date": st.get("entry_date"),
            "last": r["last"], "atr": r["atr"], "point_value": r["mult"],
            "avg_dollar_move": move, "bps": bps, "firings": r.get("firings"),
            "contracts": 0, "notional": 0.0, "daily_risk": 0.0, "stop": None,
        })

    # bps band — display/selection filter only; the universe above is full.
    n_signals = len(rows)
    if min_bps is not None or max_bps is not None:
        def _in_band(a: dict[str, Any]) -> bool:
            b = a["bps"]
            if b is None:
                return False
            if min_bps is not None and b < min_bps:
                return False
            if max_bps is not None and b > max_bps:
                return False
            return True
        rows = [a for a in rows if _in_band(a)]
    n_filtered = n_signals - len(rows)

    # Size the actual book (in-book rows) oldest-entry-first so the per-side /
    # total caps fill in trade order. Stood-aside rows are left un-sized.
    book = [p for p in rows if p["in_book"]]
    book.sort(key=lambda p: (p["entry_date"] or ""))
    gl = gs = 0.0
    n_sized = n_long = n_short = 0
    for p in book:
        atr, mult, last, d = p["atr"], p["point_value"], p["last"], p["side"]
        if atr and mult and atr > 0 and mult > 0:
            raw_n = risk * account / (atr * mult)
            cands = [raw_n]
            # Notional uses |level| so a negative spread (contango) still sizes.
            pc = abs(last) * mult if last is not None else 0.0
            if pc > 0:  # honour the exposure caps when there's a real notional
                sex = gl if d > 0 else gs
                cands += [
                    STRAT_CAP_POS * account / pc,
                    max(0.0, STRAT_CAP_SIDE * account - sex) / pc,
                    max(0.0, STRAT_CAP_TOTAL * account - gl - gs) / pc,
                ]
            n = int(math.floor(min(cands)))
            if n >= 1:
                p["contracts"] = n
                p["notional"] = n * pc
                p["daily_risk"] = n * atr * mult
                if d > 0:
                    gl += p["notional"]
                    n_long += 1
                else:
                    gs += p["notional"]
                    n_short += 1
                n_sized += 1
            if p["entry_px"] is not None:
                p["stop"] = (p["entry_px"] - STRAT_STOP_ATR * atr) if d > 0 \
                    else (p["entry_px"] + STRAT_STOP_ATR * atr)

    # Display order: single before synthetic, in-book before stood-aside, then
    # biggest notional / dollar move on top.
    rows.sort(key=lambda p: (
        p["kind"] != "outright", not p["in_book"],
        -(p["notional"] or 0.0), -(p["avg_dollar_move"] or 0.0)))

    def _grp(kind: str, in_book: bool) -> int:
        return sum(1 for p in rows if p["kind"] == kind and p["in_book"] == in_book)

    summary = {
        "account": account, "risk": risk, "use_trend": use_trend,
        "n_signals": len(rows), "n_sized": n_sized, "n_filtered": n_filtered,
        "n_long": n_long, "n_short": n_short,
        "n_single_book": _grp("outright", True), "n_single_aside": _grp("outright", False),
        "n_spread_book": _grp("spread", True), "n_spread_aside": _grp("spread", False),
        "n_universe": len(instruments),
        "gross_long": gl, "gross_short": gs, "gross_total": gl + gs,
        "margin_est": STRAT_MARGIN_PCT * (gl + gs),
    }
    return {"positions": rows, "summary": summary, "as_of": as_of,
            "source": payload.get("source"), "computed_at": payload.get("computed_at"),
            "error": None}
