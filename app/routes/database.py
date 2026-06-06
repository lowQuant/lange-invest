"""Public "Database" tab — a read-only ArcticDB browser, open to anyone.

Exposes ONLY the libraries configured under ``[[database_library]]`` (futures +
market_data), all reads routed through ``public_access`` (allowlist-enforced,
read-only). Charting is built server-side via ``app.chartdata`` and rendered with
the shared LangeChart module. No login, no writes, no other libraries.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import chartdata, public_access
from app.auth import current_user
from app.config import get_config
from app.engine import ensure_connected

router = APIRouter(prefix="/database")

PAGE_SIZE = 50


def render(request: Request, name: str, **ctx):
    ctx["user"] = current_user(request)
    ctx["nav_active"] = "database"
    return request.app.state.templates.TemplateResponse(request, name, ctx)


def _require_db_library(name: str):
    db = get_config().database_library(name)
    if db is None:
        raise HTTPException(status_code=404)
    return db


def _table_from_df(df) -> dict:
    index_name = df.index.name or "index"
    columns = [index_name] + [str(c) for c in df.columns]
    rows = []
    for idx, row in zip(df.index, df.itertuples(index=False, name=None)):
        cells = [str(idx)] + ["" if v != v else v for v in row]
        rows.append(cells)
    return {"columns": columns, "rows": rows}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def database_landing(request: Request):
    cfg = get_config()
    connected = ensure_connected()
    libs = []
    for db in cfg.database_libraries:
        count = None
        if connected:
            try:
                count = len(public_access.list_symbols(db.name))
            except Exception:  # noqa: BLE001
                count = None
        libs.append({"cfg": db, "count": count})
    return render(request, "database_landing.html", libs=libs, connected=connected)


@router.get("/{library}", response_class=HTMLResponse)
async def database_library(request: Request, library: str):
    db = _require_db_library(library)
    connected = ensure_connected()
    symbols, error = [], None
    if not connected:
        error = "The data engine is not connected in this environment."
    else:
        try:
            symbols = sorted(public_access.list_symbols(library))
        except public_access.AccessDenied:
            raise HTTPException(status_code=404)
        except Exception as e:  # noqa: BLE001
            error = f"Could not list symbols: {e}"
    return render(request, "database_library.html", db=db, symbols=symbols, error=error)


@router.get("/{library}/{symbol:path}", response_class=HTMLResponse)
async def database_symbol(request: Request, library: str, symbol: str, page: int = 1):
    db = _require_db_library(library)
    if not ensure_connected():
        return render(request, "database_symbol.html", db=db, symbol=symbol,
                      desc=None, table=None, chart=None, subplots=None,
                      page=1, pages=1, error="The data engine is not connected in this environment.")

    try:
        desc = public_access.describe_symbol(library, symbol)
    except public_access.AccessDenied:
        raise HTTPException(status_code=404)
    except Exception as e:  # noqa: BLE001
        return render(request, "database_symbol.html", db=db, symbol=symbol, desc=None,
                      table=None, chart=None, subplots=None, page=1, pages=1, error=str(e))

    total = int(desc.get("rows") or 0)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE

    table = None
    try:
        df_page = public_access.read_data(library, symbol, row_range=(start, start + PAGE_SIZE))
        table = _table_from_df(df_page)
    except Exception as e:  # noqa: BLE001
        return render(request, "database_symbol.html", db=db, symbol=symbol, desc=desc,
                      table=None, chart=None, subplots=None, page=page, pages=pages, error=str(e))

    chart = subplots = None
    if db.chart and total:
        try:
            cstart = max(0, total - chartdata.MAX_POINTS)
            cdf = public_access.read_data(library, symbol, row_range=(cstart, total))
            spec = chartdata.build_chart(cdf, title=symbol)
            if spec:
                chart, subplots = spec["chart"], spec["subplots"]
        except Exception:  # noqa: BLE001
            chart = subplots = None

    return render(request, "database_symbol.html", db=db, symbol=symbol, desc=desc,
                  table=table, chart=chart, subplots=subplots, page=page, pages=pages, error=None)
