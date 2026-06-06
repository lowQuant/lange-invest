"""ArcticDB tab — a read-only browser with the viewer's full charting.

Public visitors see ONLY the configured libraries (futures + market_data), read
through ``public_access`` (allowlist-enforced). ADMINS get the full view — every
library in the instance, read through ``core.operations`` directly — so the same
tab doubles as the admin's data browser.

Charting (line/candlestick/bar/scatter, studies, resampling, and futures contract
mode: continuous curves / spreads / overlays) is built by ``app.arctic_charting``.
"""
from __future__ import annotations

import io
import json
import re

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import arctic_charting as ac
from app import public_access
from app.auth import current_user
from app.config import get_config
from app.engine import ensure_connected

router = APIRouter(prefix="/arcticdb")
PAGE_SIZE = 50


def render(request: Request, name: str, **ctx):
    ctx["user"] = current_user(request)
    ctx["nav_active"] = "arcticdb"
    return request.app.state.templates.TemplateResponse(request, name, ctx)


# ── Access layer: admins use core directly, public uses the allowlist gate ────

def _is_admin(request: Request) -> bool:
    u = current_user(request)
    return u is not None and u.is_admin


def _ops():
    from core import operations as ops
    return ops


def _allowed(request: Request, library: str) -> bool:
    if _is_admin(request):
        return True
    cfg = get_config()
    return cfg.database_library(library) is not None and public_access.is_public(library)


def _list_libraries(request: Request) -> list[dict]:
    cfg = get_config()
    if _is_admin(request):
        try:
            names = sorted(_ops().list_libraries())
        except Exception:
            names = []
        db = {d.name: d for d in cfg.database_libraries}
        return [{"name": n, "label": db[n].label if n in db else n,
                 "description": db[n].description if n in db else "",
                 "chart": db[n].chart if n in db else True} for n in names]
    return [{"name": d.name, "label": d.label, "description": d.description, "chart": d.chart}
            for d in cfg.database_libraries]


def _read(request: Request, library: str, symbol: str, **kw):
    if _is_admin(request):
        return _ops().read_data(library, symbol, **kw)
    return public_access.read_data(library, symbol, **kw)


def _describe(request: Request, library: str, symbol: str):
    if _is_admin(request):
        return _ops().get_description(library, symbol)
    return public_access.describe_symbol(library, symbol)


def _symbols(request: Request, library: str):
    if _is_admin(request):
        return _ops().list_symbols(library)
    return public_access.list_symbols(library)


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    # List library NAMES only — symbol counts are one round-trip per library, so
    # we defer them to the library page (loaded on click). Keeps the landing fast.
    connected = ensure_connected()
    libs = _list_libraries(request) if connected else _list_libraries(request)
    return render(request, "arcticdb_landing.html", libs=libs, connected=connected,
                  is_admin=_is_admin(request))


# ── API fragments (declared BEFORE the greedy /{library}/{symbol} routes) ─────

@router.get("/api/chart/{library}/{symbol:path}", response_class=HTMLResponse)
async def api_chart(request: Request, library: str, symbol: str):
    if not _allowed(request, library):
        raise HTTPException(status_code=404)
    ensure_connected()
    params = dict(request.query_params)
    try:
        df = _read(request, library, symbol)
        main, subplots, err = ac.build_chart(df, symbol, params)
    except public_access.AccessDenied:
        raise HTTPException(status_code=404)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f'<div class="alert alert-danger mb-0">Chart error: {e}</div>')
    if err:
        return HTMLResponse(f'<div class="alert alert-warning mb-0"><i class="bi bi-exclamation-triangle"></i> {err}</div>')
    return render(request, "partials/arctic_chart.html", chart=main, subplots=subplots)


@router.get("/api/table/{library}/{symbol:path}", response_class=HTMLResponse)
async def api_table(request: Request, library: str, symbol: str, page: int = 1):
    if not _allowed(request, library):
        raise HTTPException(status_code=404)
    ensure_connected()
    try:
        desc = _describe(request, library, symbol)
        total = int(desc.get("rows") or 0)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, pages))
        start = (page - 1) * PAGE_SIZE
        df = _read(request, library, symbol, row_range=(start, start + PAGE_SIZE))
    except public_access.AccessDenied:
        raise HTTPException(status_code=404)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f'<div class="alert alert-danger mb-0">{e}</div>')

    is_mi = isinstance(df.index, pd.MultiIndex)
    has_index = is_mi or df.index.name is not None or not isinstance(df.index, pd.RangeIndex)
    index_label = " / ".join(str(n or "") for n in df.index.names) if is_mi else (df.index.name or "index")
    data_columns = [str(c) for c in df.columns]
    rows = []
    for i, (idx, row) in enumerate(zip(df.index, df.itertuples(index=False, name=None))):
        idx_str = " / ".join(str(x) for x in idx) if isinstance(idx, tuple) else str(idx)
        cells = ["" if (v != v) else v for v in row]
        rows.append({"n": start + i, "idx": idx_str, "cells": cells})
    return render(request, "partials/arctic_table.html", library=library, symbol=symbol,
                  data_columns=data_columns, index_label=index_label, has_index=has_index,
                  rows=rows, page=page, pages=pages, total=total, page_size=PAGE_SIZE,
                  can_edit=_is_admin(request))


@router.get("/api/chart-info/{library}/{symbol:path}", response_class=JSONResponse)
async def api_chart_info(request: Request, library: str, symbol: str):
    if not _allowed(request, library):
        raise HTTPException(status_code=404)
    ensure_connected()
    try:
        df = _read(request, library, symbol)
        meta = ac.symbol_meta(df)
        meta["contracts"] = ac.contract_names(df) if meta["is_multiindex"] else []
        return JSONResponse(meta)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Admin CRUD (full read/write; never reachable by non-admins) ───────────────

def _require_admin(request: Request) -> None:
    if not _is_admin(request):
        raise HTTPException(status_code=403)


def _toast(message: str, type_: str = "success", **extra) -> dict:
    payload = {"showToast": {"message": message, "type": type_}}
    payload.update(extra)
    return {"HX-Trigger": json.dumps(payload)}


def _parse_value(val: str):
    val = val.strip()
    m = re.match(r"^([+-]?[\d.]+)\s*\^\s*([+-]?[\d.]+)$", val)
    if m:
        return float(m.group(1)) ** float(m.group(2))
    try:
        return float(val.replace("_", ""))
    except (ValueError, TypeError):
        return val


def _parse_timestamp(val: str) -> pd.Timestamp:
    val = val.strip()
    if re.match(r"^\d{4}-\d{2}", val):
        return pd.Timestamp(val)
    return pd.to_datetime(val, dayfirst=True)


def _library_list_response(request: Request, headers: dict):
    return request.app.state.templates.TemplateResponse(
        request, "partials/arctic_library_list.html",
        {"libs": _list_libraries(request), "is_admin": True, "user": current_user(request)},
        headers=headers,
    )


def _symbol_list_response(request: Request, library: str, headers: dict):
    return request.app.state.templates.TemplateResponse(
        request, "partials/arctic_symbol_list.html",
        {"library": library, "symbols": sorted(_ops().list_symbols(library)),
         "is_admin": True, "user": current_user(request)},
        headers=headers,
    )


@router.post("/api/library", response_class=HTMLResponse)
async def create_library(request: Request):
    _require_admin(request); ensure_connected()
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return HTMLResponse("", status_code=400, headers=_toast("Library name is required", "error"))
    try:
        _ops().create_library(name)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse("", status_code=400, headers=_toast(f"Error: {e}", "error"))
    return _library_list_response(request, _toast(f"Library '{name}' created"))


@router.delete("/api/library/{library}", response_class=HTMLResponse)
async def delete_library(request: Request, library: str):
    _require_admin(request); ensure_connected()
    try:
        _ops().delete_library(library)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse("", status_code=400, headers=_toast(f"Error: {e}", "error"))
    return _library_list_response(request, _toast(f"Library '{library}' deleted"))


@router.delete("/api/symbol/{library}/{symbol:path}", response_class=HTMLResponse)
async def delete_symbol(request: Request, library: str, symbol: str):
    _require_admin(request); ensure_connected()
    try:
        _ops().delete_symbol(library, symbol)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse("", status_code=400, headers=_toast(f"Error: {e}", "error"))
    return _symbol_list_response(request, library, _toast(f"Symbol '{symbol}' deleted"))


@router.post("/api/symbol/{library}/create", response_class=HTMLResponse)
async def create_symbol(request: Request, library: str):
    _require_admin(request); ensure_connected()
    try:
        body = await request.json()
        symbol = (body.get("symbol") or "").strip()
        columns = body.get("columns", [])
        if not symbol:
            return HTMLResponse("Symbol name is required", status_code=400)
        if not columns:
            return HTMLResponse("At least one column is required", status_code=400)
        dtype_map = {"float": "float64", "int": "int64", "str": "object", "bool": "bool"}
        df = pd.DataFrame({c["name"]: pd.Series(dtype=dtype_map.get(c["type"], "object")) for c in columns})
        index_type = body.get("index_type", "none")
        if index_type == "datetime":
            df.index = pd.DatetimeIndex([], name=body.get("index_name") or "timestamp")
        elif index_type == "integer":
            df.index = pd.Index([], dtype="int64", name=body.get("index_name") or "index")
        _ops().write_data(library, symbol, df)
        return HTMLResponse("OK")
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(str(e), status_code=400)


@router.post("/api/upload/{library}", response_class=HTMLResponse)
async def upload_csv(request: Request, library: str):
    _require_admin(request); ensure_connected()
    form = await request.form()
    symbol = (form.get("symbol") or "").strip()
    file = form.get("file")
    if not symbol or file is None:
        return HTMLResponse("", status_code=400, headers=_toast("Symbol and file required", "error"))
    try:
        content = await file.read()
        df = pd.read_csv(io.BytesIO(content))
        _ops().write_data(library, symbol, df)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse("", status_code=400, headers=_toast(f"Error: {e}", "error"))
    return _symbol_list_response(request, library, _toast(f"Uploaded {symbol} ({len(df)} rows)"))


@router.put("/api/cell/{library}/{symbol:path}", response_class=HTMLResponse)
async def edit_cell(request: Request, library: str, symbol: str):
    _require_admin(request); ensure_connected()
    form = await request.form()
    try:
        row_idx = int(form["row_idx"])
        col = form["col_name"]
        value = form["value"]
        df = _ops().read_data(library, symbol)
        if pd.api.types.is_numeric_dtype(df[col].dtype):
            parsed = _parse_value(value)
            if isinstance(parsed, float):
                value = int(parsed) if pd.api.types.is_integer_dtype(df[col].dtype) else parsed
        df.at[df.index[row_idx], col] = value
        _ops().write_data(library, symbol, df)
        return HTMLResponse(str(value), headers=_toast("Cell updated"))
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(str(e), status_code=400, headers=_toast(f"Error: {e}", "error"))


@router.post("/api/addrow/{library}/{symbol:path}", response_class=HTMLResponse)
async def add_row(request: Request, library: str, symbol: str):
    _require_admin(request); ensure_connected()
    try:
        body = await request.json()
        df = _ops().read_data(library, symbol)
        new_row = {}
        for col in df.columns:
            val = body.get(col, "")
            if val == "":
                new_row[col] = None
            elif pd.api.types.is_numeric_dtype(df[col]):
                parsed = _parse_value(val)
                new_row[col] = parsed if isinstance(parsed, float) else None
            else:
                new_row[col] = val
        new_df = pd.DataFrame([new_row], columns=df.columns)
        if isinstance(df.index, pd.DatetimeIndex):
            iv = body.get(df.index.name or "index", "")
            new_df.index = pd.DatetimeIndex([_parse_timestamp(iv) if iv else pd.Timestamp.now()], name=df.index.name)
        elif df.index.name and df.index.name in body:
            new_df.index = pd.Index([body[df.index.name]], name=df.index.name)
        combined = pd.concat([df, new_df])
        if isinstance(combined.index, pd.DatetimeIndex):
            combined = combined.sort_index()
        _ops().write_data(library, symbol, combined)
        return HTMLResponse("OK")
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(str(e), status_code=400)


@router.delete("/api/rows/{library}/{symbol:path}", response_class=HTMLResponse)
async def delete_rows(request: Request, library: str, symbol: str):
    _require_admin(request); ensure_connected()
    form = await request.form()
    try:
        indices = json.loads(form["row_indices"])
        df = _ops().read_data(library, symbol)
        df = df.drop(df.index[indices])
        _ops().write_data(library, symbol, df)
        return HTMLResponse("", headers=_toast("Rows deleted", refreshTable="true"))
    except Exception as e:  # noqa: BLE001
        return HTMLResponse("", status_code=400, headers=_toast(f"Error: {e}", "error"))


# ── Browse pages (greedy routes LAST so /api/* wins) ──────────────────────────

@router.get("/{library}", response_class=HTMLResponse)
async def library_view(request: Request, library: str, q: str = ""):
    if not _allowed(request, library):
        raise HTTPException(status_code=404)
    connected = ensure_connected()
    symbols, error = [], None
    if not connected:
        error = "The data engine is not connected in this environment."
    else:
        try:
            symbols = sorted(_symbols(request, library))
            if q:
                symbols = [s for s in symbols if q.lower() in s.lower()]
        except public_access.AccessDenied:
            raise HTTPException(status_code=404)
        except Exception as e:  # noqa: BLE001
            error = f"Could not list symbols: {e}"
    is_admin = _is_admin(request)
    # HTMX search returns just the symbol-list partial.
    if request.headers.get("HX-Request") and not error:
        return render(request, "partials/arctic_symbol_list.html",
                      library=library, symbols=symbols, is_admin=is_admin)
    return render(request, "arcticdb_library.html", library=library, symbols=symbols,
                  error=error, is_admin=is_admin)


@router.get("/{library}/{symbol:path}", response_class=HTMLResponse)
async def symbol_view(request: Request, library: str, symbol: str):
    if not _allowed(request, library):
        raise HTTPException(status_code=404)
    if not ensure_connected():
        return render(request, "arcticdb_symbol.html", library=library, symbol=symbol,
                      desc=None, meta=None, error="The data engine is not connected.")
    try:
        desc = _describe(request, library, symbol)
        sample = _read(request, library, symbol, row_range=(0, 50))
        meta = ac.symbol_meta(sample)
    except public_access.AccessDenied:
        raise HTTPException(status_code=404)
    except Exception as e:  # noqa: BLE001
        return render(request, "arcticdb_symbol.html", library=library, symbol=symbol,
                      desc=None, meta=None, error=str(e))
    return render(request, "arcticdb_symbol.html", library=library, symbol=symbol,
                  desc=desc, meta=meta, error=None, is_admin=_is_admin(request))
