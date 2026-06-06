"""Charting engine for the ArcticDB tab.

Ported from arcticdb-viewer's data.py so the charts match the viewer feature-for-
feature: standard line/bar/scatter/candlestick with Y-column selection, studies
(SMA/EMA), period resampling, AND a futures "contract mode" for MultiIndex
``(date, localsymbol)`` symbols — continuous front-month series with several
back-adjustment methods + roll rules, calendar spreads, and rank overlays.

Pure functions over a passed DataFrame; no ArcticDB access here. Output dicts are
consumed by the shared LangeChart JS module.
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

MAX_POINTS = 2500


# ── MultiIndex contract detection ────────────────────────────────────────────

def detect_multiindex_contracts(df: pd.DataFrame) -> bool:
    if not isinstance(df.index, pd.MultiIndex) or df.index.nlevels != 2:
        return False
    try:
        l0 = df.index.get_level_values(0)
        l1 = df.index.get_level_values(1)
        return pd.api.types.is_datetime64_any_dtype(l0) and l1.dtype == object
    except Exception:
        return False


def contract_names(df: pd.DataFrame) -> list[str]:
    if not isinstance(df.index, pd.MultiIndex):
        return []
    return sorted(df.index.get_level_values(1).unique().tolist())


def symbol_meta(df: pd.DataFrame) -> dict[str, Any]:
    is_multi = detect_multiindex_contracts(df)
    cols_lower = {c.lower() for c in df.columns}
    return {
        "is_multiindex": is_multi,
        "columns": [str(c) for c in df.columns],
        "has_ohlc": all(k in cols_lower for k in ("open", "high", "low", "close")),
        "has_volume": "volume" in cols_lower,
        "has_dte": "dte" in cols_lower,
        "n_contracts": len(contract_names(df)) if is_multi else 0,
    }


# ── Continuous / spread construction ─────────────────────────────────────────

def _extract_by_rank(df: pd.DataFrame, rank: int, col: str) -> pd.Series:
    if "dte" not in df.columns:
        dates = df.index.get_level_values(0)
        contracts = df.index.get_level_values(1)
        result = {}
        for date in dates.unique():
            mask = dates == date
            date_df = df.loc[mask]
            sorted_c = sorted(contracts[mask].unique())
            if rank <= len(sorted_c):
                result[date] = date_df.loc[(date, sorted_c[rank - 1]), col]
        return pd.Series(result, name=f"rank{rank}_{col}")
    result = {}
    for date in df.index.get_level_values(0).unique():
        ds = df.loc[date]
        if isinstance(ds, pd.Series):
            if rank == 1 and col in ds.index:
                result[date] = ds[col]
            continue
        valid = ds[ds["dte"] > 0].sort_values("dte")
        if rank <= len(valid):
            result[date] = valid.iloc[rank - 1][col]
    return pd.Series(result, name=f"rank{rank}_{col}")


def _compute_spread(df, rank1, rank2, col) -> pd.Series:
    s = _extract_by_rank(df, rank1, col) - _extract_by_rank(df, rank2, col)
    s.name = f"spread_{rank1}_{rank2}_{col}"
    return s


def _perpetual(df, col, window) -> pd.Series:
    out = {}
    for date in sorted(df.index.get_level_values(0).unique()):
        ds = df.loc[date]
        if isinstance(ds, pd.Series):
            if col in ds.index and pd.notna(ds[col]):
                out[date] = float(ds[col])
            continue
        valid = ds[ds["dte"] > 0].sort_values("dte")
        if len(valid) == 0:
            continue
        if len(valid) == 1:
            v = valid.iloc[0][col]
            if pd.notna(v):
                out[date] = float(v)
            continue
        fp, dp = valid.iloc[0][col], valid.iloc[1][col]
        if pd.isna(fp) or pd.isna(dp):
            continue
        fdte = float(valid.iloc[0]["dte"])
        w = 1.0 if fdte >= window else (0.0 if fdte <= 0 else fdte / window)
        out[date] = w * float(fp) + (1 - w) * float(dp)
    return pd.Series(out, name=f"perpetual_{col}")


def build_continuous(df, rank, col, method, roll_rule) -> pd.Series:
    if "dte" not in df.columns:
        return _extract_by_rank(df, rank, col)
    if method == "perpetual":
        window = 5
        if roll_rule.startswith("calendar_"):
            try:
                window = max(1, int(roll_rule.split("_", 1)[1]))
            except (ValueError, IndexError):
                window = 5
        return _perpetual(df, col, window)

    calendar_offset = 0
    if roll_rule.startswith("calendar_"):
        try:
            calendar_offset = int(roll_rule.split("_", 1)[1])
        except (ValueError, IndexError):
            calendar_offset = 0
    use_volume = roll_rule == "volume" and "volume" in df.columns

    selections: list[tuple] = []
    for date in sorted(df.index.get_level_values(0).unique()):
        try:
            ds = df.loc[date]
        except KeyError:
            continue
        if isinstance(ds, pd.Series):
            if rank == 1 and col in ds.index and pd.notna(ds[col]):
                selections.append((date, "_", float(ds[col])))
            continue
        valid = ds[ds["dte"] > 0]
        if len(valid) == 0:
            continue
        if use_volume:
            ranked = valid.sort_values("volume", ascending=False, na_position="last")
        elif calendar_offset > 0:
            f = valid[valid["dte"] > calendar_offset]
            ranked = f.sort_values("dte") if len(f) >= rank else valid.sort_values("dte")
        else:
            ranked = valid.sort_values("dte")
        if len(ranked) < rank:
            continue
        pick = ranked.iloc[rank - 1]
        if pd.isna(pick[col]):
            continue
        selections.append((date, pick.name, float(pick[col])))

    if not selections:
        return pd.Series([], dtype=float)
    dates_arr = [s[0] for s in selections]
    contracts_arr = [s[1] for s in selections]
    prices = [s[2] for s in selections]
    n = len(prices)
    if method == "none":
        return pd.Series(prices, index=dates_arr, name=f"cont{rank}_{col}")

    adj = [0.0 if method == "back_diff" else 1.0] * n
    for i in range(n - 1, 0, -1):
        adj[i - 1] = adj[i]
        if contracts_arr[i] == contracts_arr[i - 1]:
            continue
        new_today = prices[i]
        try:
            raw = df.loc[(dates_arr[i], contracts_arr[i - 1]), col]
            old_today = float(raw) if pd.notna(raw) else prices[i - 1]
        except (KeyError, TypeError):
            old_today = prices[i - 1]
        if method == "back_diff":
            adj[i - 1] = adj[i] + (new_today - old_today)
        elif method == "back_ratio" and old_today > 0 and new_today > 0:
            adj[i - 1] = adj[i] * (new_today / old_today)
    if method == "back_diff":
        return pd.Series([prices[i] + adj[i] for i in range(n)], index=dates_arr, name=f"cont{rank}_{col}_adj")
    return pd.Series([prices[i] * adj[i] for i in range(n)], index=dates_arr, name=f"cont{rank}_{col}_adjr")


def _label(sym, rank, col, method) -> str:
    suffix = {"back_diff": " [adj-Δ]", "back_ratio": " [adj-r]", "perpetual": " [perp]"}.get(method, "")
    if method == "perpetual":
        return f"{sym} {col}{suffix}"
    return f"{sym}{rank} {col}{suffix}"


# ── Studies / resampling / standard chart ────────────────────────────────────

def _study(values: list, study_type: str, period: int) -> list:
    s = pd.Series(values, dtype=float)
    if study_type == "sma":
        r = s.rolling(window=period, min_periods=1).mean()
    elif study_type == "ema":
        r = s.ewm(span=period, min_periods=1, adjust=False).mean()
    else:
        return values
    return [None if pd.isna(v) else round(v, 6) for v in r.tolist()]


def _study_datasets(data: list, studies_str: str) -> list[dict]:
    if not studies_str:
        return []
    try:
        cfgs = json.loads(studies_str)
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for sc in cfgs:
        t = sc.get("type", "sma")
        p = int(sc.get("period", 20))
        out.append({"label": f"{t.upper()}({p})", "data": _study(data, t, p), "is_study": True})
    return out


def _fmt_index(v) -> str:
    if isinstance(v, tuple):
        return _fmt_index(v[0])
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def _ohlc_cols(df) -> dict | None:
    cl = {c.lower(): c for c in df.columns}
    if all(k in cl for k in ("open", "high", "low", "close")):
        return {k: cl[k] for k in ("open", "high", "low", "close")}
    return None


def _resample(df, period) -> pd.DataFrame:
    freq = {"W": "W", "M": "ME", "Q": "QE", "Y": "YE"}.get(period)
    if not freq:
        return df
    if isinstance(df.index, pd.MultiIndex):
        lvl = df.index.get_level_values(0)
        if not pd.api.types.is_datetime64_any_dtype(lvl):
            return df
        flat = df.copy()
        flat.index = lvl
        num = flat.select_dtypes("number").groupby(level=0).last()
        return num.resample(freq).last().dropna(how="all")
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    num = df.select_dtypes("number")
    return num.resample(freq).last().dropna(how="all") if not num.empty else df


def _series_data(s) -> list:
    return [v if pd.notna(v) else None for v in s.tolist()]


def _subsample(df):
    if len(df) <= MAX_POINTS:
        return df
    step = max(1, len(df) // MAX_POINTS)
    return df.iloc[::step]


def _build_standard(df, x_col, y_cols, chart_type):
    df = _subsample(df)
    if x_col == "__index__" or not x_col:
        x_values = [_fmt_index(v) for v in df.index]
        x_label = (df.index.names[0] if isinstance(df.index, pd.MultiIndex) else df.index.name) or "index"
    else:
        x_values = [_fmt_index(v) for v in df[x_col]]
        x_label = x_col

    if chart_type == "candlestick":
        ohlc = _ohlc_cols(df)
        if not ohlc:
            return None, "Candlestick needs Open/High/Low/Close columns."
        data = [
            {"o": float(r[ohlc["open"]]), "h": float(r[ohlc["high"]]),
             "l": float(r[ohlc["low"]]), "c": float(r[ohlc["close"]])}
            if r[[ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"]]].notna().all() else None
            for _, r in df.iterrows()
        ]
        return {"x_values": x_values, "x_label": x_label, "chart_type": "candlestick",
                "datasets": [{"label": "OHLC", "data": data}]}, None

    if y_cols:
        cols = [c.strip() for c in y_cols.split(",") if c.strip() in df.columns]
    else:
        cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])][:5]
    datasets = [{"label": c, "data": _series_data(df[c])} for c in cols]
    return {"x_values": x_values, "x_label": x_label, "chart_type": chart_type, "datasets": datasets}, None


# ── Top-level entry ──────────────────────────────────────────────────────────

def build_chart(df: pd.DataFrame, sym: str, p: dict) -> tuple[dict | None, list, str | None]:
    """Return (main_chart, subplots, error)."""
    chart_type = p.get("chart_type", "line")
    period = p.get("period", "")
    is_multi = detect_multiindex_contracts(df)
    mode = p.get("contract_mode", "")

    def resample_series(s):
        if period and isinstance(s.index, pd.DatetimeIndex):
            freq = {"W": "W", "M": "ME", "Q": "QE", "Y": "YE"}.get(period)
            if freq:
                return s.resample(freq).last().dropna()
        return s

    col = p.get("contract_col", "close")
    method = p.get("continuous_method", "back_diff")
    roll = p.get("roll_rule", "expiry")
    err = None

    if is_multi and mode == "single":
        s = resample_series(build_continuous(df, int(p.get("contract_rank", 1)), col, method, roll))
        ds = [{"label": _label(sym, int(p.get("contract_rank", 1)), col, method), "data": _series_data(s)}]
        ds += _study_datasets(ds[0]["data"], p.get("studies", ""))
        main = {"x_values": [_fmt_index(v) for v in s.index], "x_label": "date",
                "chart_type": chart_type if chart_type != "candlestick" else "line", "datasets": ds}
    elif is_multi and mode == "spread":
        s = resample_series(_compute_spread(df, int(p.get("spread_rank1", 1)), int(p.get("spread_rank2", 2)), col))
        ds = [{"label": f"{sym}{p.get('spread_rank1',1)}−{sym}{p.get('spread_rank2',2)} ({col})", "data": _series_data(s)}]
        ds += _study_datasets(ds[0]["data"], p.get("studies", ""))
        main = {"x_values": [_fmt_index(v) for v in s.index], "x_label": "date",
                "chart_type": chart_type if chart_type != "candlestick" else "line", "datasets": ds}
    elif is_multi and mode == "overlay":
        ds, xv = [], None
        for rank in (int(p.get("spread_rank1", 1)), int(p.get("spread_rank2", 2))):
            s = resample_series(build_continuous(df, rank, col, method, roll))
            if xv is None:
                xv = [_fmt_index(v) for v in s.index]
            ds.append({"label": _label(sym, rank, col, method), "data": _series_data(s)})
        main = {"x_values": xv or [], "x_label": "date", "chart_type": "line", "datasets": ds}
    else:
        work = _resample(df, period) if period else df
        main, err = _build_standard(work, p.get("x_col", "__index__"), p.get("y_cols", ""), chart_type)
        if main and chart_type in ("line", "scatter") and p.get("studies"):
            extra = []
            for d in main["datasets"]:
                extra += _study_datasets(d["data"], p["studies"])
            main["datasets"] += extra

    if err:
        return None, [], err

    subplots = []
    if p.get("subplots"):
        try:
            for sp in json.loads(p["subplots"]):
                work = _resample(df, period) if period else df
                sp_data, sp_err = _build_standard(work, p.get("x_col", "__index__"), sp.get("y_cols", ""), sp.get("type", "bar"))
                if sp_data:
                    subplots.append(sp_data)
        except (json.JSONDecodeError, TypeError):
            pass

    return main, subplots, None
