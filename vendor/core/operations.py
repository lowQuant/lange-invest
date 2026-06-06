from __future__ import annotations

from typing import Any

import pandas as pd

from core.connection import get_arctic


# ── Library operations ──

def list_libraries() -> list[str]:
    return get_arctic().list_libraries()


def create_library(name: str) -> None:
    get_arctic().create_library(name)


def delete_library(name: str) -> None:
    get_arctic().delete_library(name)


def has_library(name: str) -> bool:
    return get_arctic().has_library(name)


# ── Symbol operations ──

def list_symbols(library: str) -> list[str]:
    lib = get_arctic()[library]
    return lib.list_symbols()


def has_symbol(library: str, symbol: str) -> bool:
    lib = get_arctic()[library]
    return lib.has_symbol(symbol)


def get_description(library: str, symbol: str) -> dict[str, Any]:
    lib = get_arctic()[library]
    desc = lib.get_description(symbol)
    col_names = [c.name for c in desc.columns]
    col_dtypes = {c.name: str(c.dtype) for c in desc.columns}
    index_info = None
    if desc.index:
        idx = desc.index[0]
        # Handle both single index and MultiIndex descriptors
        if hasattr(idx, 'name'):
            index_info = {
                "name": idx.name,
                "dtype": str(idx.dtype),
            }
        elif isinstance(idx, (list, tuple)):
            # MultiIndex: idx might be a list of NameWithDType
            index_info = {
                "name": idx[0].name if hasattr(idx[0], 'name') else str(idx[0]),
                "dtype": str(idx[0].dtype) if hasattr(idx[0], 'dtype') else "unknown",
            }
        else:
            index_info = {
                "name": str(idx),
                "dtype": "unknown",
            }
    return {
        "rows": desc.row_count,
        "columns": col_names,
        "dtypes": col_dtypes,
        "index": index_info,
        "date_range": [str(d) for d in desc.date_range] if desc.date_range else None,
        "last_update": str(desc.last_update_time) if hasattr(desc, "last_update_time") else None,
    }


def delete_symbol(library: str, symbol: str) -> None:
    lib = get_arctic()[library]
    lib.delete(symbol)


# ── Data operations ──

def read_data(
    library: str,
    symbol: str,
    row_range: tuple[int, int] | None = None,
    columns: list[str] | None = None,
    date_range: tuple | None = None,
) -> pd.DataFrame:
    lib = get_arctic()[library]
    kwargs: dict[str, Any] = {}
    if row_range is not None:
        kwargs["row_range"] = row_range
    if columns is not None:
        kwargs["columns"] = columns
    if date_range is not None:
        kwargs["date_range"] = date_range
    return lib.read(symbol, **kwargs).data


def write_data(library: str, symbol: str, data: pd.DataFrame) -> None:
    lib = get_arctic()[library]
    lib.write(symbol, data)


def update_data(library: str, symbol: str, data: pd.DataFrame) -> None:
    lib = get_arctic()[library]
    lib.update(symbol, data)


def append_data(library: str, symbol: str, data: pd.DataFrame) -> None:
    lib = get_arctic()[library]
    lib.append(symbol, data)
