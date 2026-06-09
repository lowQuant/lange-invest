"""Query pipeline for the ArcticDB data table — ported from arcticdb-viewer.

filter → deduplicate → group → sort → limit → columns. Pure pandas over a passed
DataFrame. Values support 10^6 (caret exponent), 2.5e3, 1_000_000, ISO dates, and
dayfirst European dates (DD.MM.YYYY).
"""
from __future__ import annotations

import json
import re

import pandas as pd


def parse_value(val: str):
    val = val.strip()
    m = re.match(r"^([+-]?[\d.]+)\s*\^\s*([+-]?[\d.]+)$", val)
    if m:
        return float(m.group(1)) ** float(m.group(2))
    try:
        return float(val.replace("_", ""))
    except (ValueError, TypeError):
        return val


def parse_timestamp(val: str) -> pd.Timestamp:
    val = val.strip()
    if re.match(r"^\d{4}-\d{2}", val):
        return pd.Timestamp(val)
    return pd.to_datetime(val, dayfirst=True)


def parse_query(query_str: str) -> dict:
    if not query_str:
        return {}
    try:
        return json.loads(query_str)
    except (json.JSONDecodeError, TypeError):
        return {}


def _apply_single_filter(df: pd.DataFrame, col: str, op: str, val: str) -> pd.DataFrame:
    is_index = col == "__index__"
    mi_level = None
    if col.startswith("__index_") and col.endswith("__") and isinstance(df.index, pd.MultiIndex):
        try:
            mi_level = int(col[len("__index_"):-len("__")])
        except ValueError:
            pass

    if is_index or mi_level is not None:
        if isinstance(df.index, pd.MultiIndex):
            level = mi_level if mi_level is not None else 0
            level_values = df.index.get_level_values(level)
            if pd.api.types.is_datetime64_any_dtype(level_values):
                try:
                    if op == "between":
                        parts = [p.strip() for p in val.split(",")]
                        if len(parts) == 2:
                            mask = (level_values >= parse_timestamp(parts[0])) & (level_values <= parse_timestamp(parts[1]))
                            return df[mask]
                        return df
                    parsed_ts = parse_timestamp(val)
                    ops_map = {"eq": "__eq__", "neq": "__ne__", "gt": "__gt__", "gte": "__ge__", "lt": "__lt__", "lte": "__le__"}
                    if op in ops_map:
                        series = pd.Series(level_values, index=df.index)
                        return df[getattr(series, ops_map[op])(parsed_ts)]
                    return df
                except Exception:
                    pass
            series = pd.Series(level_values.astype(str), index=df.index)
        else:
            series = df.index.to_series()
            if isinstance(df.index, pd.DatetimeIndex):
                try:
                    if op == "between":
                        parts = [p.strip() for p in val.split(",")]
                        if len(parts) == 2:
                            return df[(series >= parse_timestamp(parts[0])) & (series <= parse_timestamp(parts[1]))]
                        return df
                    parsed_ts = parse_timestamp(val)
                    ops_map = {"eq": "__eq__", "neq": "__ne__", "gt": "__gt__", "gte": "__ge__", "lt": "__lt__", "lte": "__le__"}
                    if op in ops_map:
                        return df[getattr(series, ops_map[op])(parsed_ts)]
                    return df
                except Exception:
                    pass
            series = series.astype(str)
    else:
        if col not in df.columns:
            return df
        series = df[col]

    parsed = parse_value(val)

    if isinstance(parsed, float) and not is_index and pd.api.types.is_numeric_dtype(series):
        if op == "between":
            parts = [p.strip() for p in val.split(",")]
            if len(parts) == 2:
                lo, hi = parse_value(parts[0]), parse_value(parts[1])
                if isinstance(lo, float) and isinstance(hi, float):
                    return df[(series >= lo) & (series <= hi)]
            return df
        ops_map = {"eq": "__eq__", "neq": "__ne__", "gt": "__gt__", "gte": "__ge__", "lt": "__lt__", "lte": "__le__"}
        if op in ops_map:
            return df[getattr(series, ops_map[op])(parsed)]
        return df

    if not is_index and pd.api.types.is_datetime64_any_dtype(series):
        try:
            if op == "between":
                parts = [p.strip() for p in val.split(",")]
                if len(parts) == 2:
                    return df[(series >= parse_timestamp(parts[0])) & (series <= parse_timestamp(parts[1]))]
                return df
            parsed_ts = parse_timestamp(val)
            ops_map = {"eq": "__eq__", "neq": "__ne__", "gt": "__gt__", "gte": "__ge__", "lt": "__lt__", "lte": "__le__"}
            if op in ops_map:
                return df[getattr(series, ops_map[op])(parsed_ts)]
        except Exception:
            pass

    str_series = series.astype(str)
    val_str = str(parsed) if isinstance(parsed, float) else val
    str_ops = {
        "eq": lambda s, v: s == v, "neq": lambda s, v: s != v,
        "gt": lambda s, v: s > v, "gte": lambda s, v: s >= v,
        "lt": lambda s, v: s < v, "lte": lambda s, v: s <= v,
        "contains": lambda s, v: s.str.contains(v, case=False, na=False),
        "startswith": lambda s, v: s.str.startswith(v, na=False),
        "endswith": lambda s, v: s.str.endswith(v, na=False),
    }
    if op == "regex":
        try:
            return df[str_series.str.contains(val_str, case=False, na=False, regex=True)]
        except re.error:
            return df
    if op in str_ops:
        return df[str_ops[op](str_series, val_str)]
    return df


def apply_chart_filters(df: pd.DataFrame, query: dict) -> pd.DataFrame:
    """Apply only the row-selecting parts of a query (filters + limit) to df.

    Used by the chart endpoint so charts respect the user's table filter without
    reshaping the data. Skips group_by / columns / sort, which would either
    flatten a MultiIndex or reorder the x-axis in a way that doesn't make sense
    for time-series plotting.
    """
    if not query:
        return df
    for f in query.get("filters", []):
        col, op, val = f.get("col", ""), f.get("op", ""), f.get("val", "")
        if col and op and val:
            df = _apply_single_filter(df, col, op, val)
    limit = query.get("limit")
    if limit and limit.get("n"):
        n = int(limit["n"])
        df = df.tail(n) if limit.get("mode", "first") == "last" else df.head(n)
    return df


def execute_query(df: pd.DataFrame, query: dict) -> tuple[pd.DataFrame, list[str]]:
    """Run the pipeline. Returns (transformed_df, display_columns)."""
    for f in query.get("filters", []):
        col, op, val = f.get("col", ""), f.get("op", ""), f.get("val", "")
        if col and op and val:
            df = _apply_single_filter(df, col, op, val)

    dedup = query.get("deduplicate")
    if dedup and dedup.get("col"):
        col, keep = dedup["col"], dedup.get("keep", "last")
        if col == "__index__":
            df = df[~df.index.duplicated(keep=keep)]
        elif col in df.columns:
            df = df.drop_duplicates(subset=[col], keep=keep)

    gb = query.get("group_by")
    if gb and gb.get("col"):
        col, agg = gb["col"], gb.get("agg", "last")
        if col == "__index__":
            idx_name = df.index.name or "index"
            df = df.reset_index(); col = idx_name
        if col in df.columns:
            numeric_cols = df.select_dtypes(include="number").columns.tolist()
            if agg in ("last", "first"):
                df = getattr(df.groupby(col, sort=False), agg)()
            elif agg == "count":
                df = df.groupby(col, sort=False).size().reset_index(name="count")
            elif agg in ("sum", "mean", "min", "max", "median", "std"):
                if numeric_cols:
                    df = getattr(df.groupby(col, sort=False)[numeric_cols], agg)()
                else:
                    df = df.groupby(col, sort=False).first()
            df = df.reset_index()

    sort = query.get("sort")
    if sort and sort.get("col"):
        ascending = sort.get("dir", "asc") != "desc"
        if sort["col"] == "__index__":
            df = df.sort_index(ascending=ascending)
        elif sort["col"] in df.columns:
            df = df.sort_values(by=sort["col"], ascending=ascending, na_position="last")

    limit = query.get("limit")
    if limit and limit.get("n"):
        n = int(limit["n"])
        df = df.tail(n) if limit.get("mode", "first") == "last" else df.head(n)

    display_cols = list(df.columns)
    sel_cols = query.get("columns")
    if sel_cols:
        valid = [c for c in sel_cols if c in df.columns]
        if valid:
            df = df[valid]; display_cols = valid

    return df, display_cols
