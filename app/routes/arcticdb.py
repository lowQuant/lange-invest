"""ArcticDB tab — a read-only browser with the viewer's full charting.

Public visitors see ONLY the configured libraries (futures + market_data), read
through ``public_access`` (allowlist-enforced). ADMINS get the full view — every
library in the instance, read through ``core.operations`` directly — so the same
tab doubles as the admin's data browser.

Charting (line/candlestick/bar/scatter, studies, resampling, and futures contract
mode: continuous curves / spreads / overlays) is built by ``app.arctic_charting``.
"""
from __future__ import annotations

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
    connected = ensure_connected()
    libs = []
    for item in _list_libraries(request):
        count = None
        if connected:
            try:
                count = len(_symbols(request, item["name"]))
            except Exception:
                count = None
        libs.append({**item, "count": count})
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

    index_name = " / ".join(str(n or "") for n in df.index.names) if isinstance(df.index, pd.MultiIndex) else (df.index.name or "index")
    columns = [index_name] + [str(c) for c in df.columns]
    rows = []
    for idx, row in zip(df.index, df.itertuples(index=False, name=None)):
        cells = [(" / ".join(str(x) for x in idx) if isinstance(idx, tuple) else str(idx))]
        cells += ["" if (v != v) else v for v in row]
        rows.append(cells)
    return render(request, "partials/arctic_table.html", library=library, symbol=symbol,
                  columns=columns, rows=rows, page=page, pages=pages, total=total, page_size=PAGE_SIZE)


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


# ── Browse pages (greedy routes LAST so /api/* wins) ──────────────────────────

@router.get("/{library}", response_class=HTMLResponse)
async def library_view(request: Request, library: str):
    if not _allowed(request, library):
        raise HTTPException(status_code=404)
    connected = ensure_connected()
    symbols, error = [], None
    if not connected:
        error = "The data engine is not connected in this environment."
    else:
        try:
            symbols = sorted(_symbols(request, library))
        except public_access.AccessDenied:
            raise HTTPException(status_code=404)
        except Exception as e:  # noqa: BLE001
            error = f"Could not list symbols: {e}"
    return render(request, "arcticdb_library.html", library=library, symbols=symbols, error=error)


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
                  desc=desc, meta=meta, error=None)
