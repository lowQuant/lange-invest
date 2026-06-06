"""Gated component fragments — HTMX partials behind auth + entitlement.

These return the live table/figure when the viewer is authenticated AND
entitled; otherwise the access-wall card. The wall lives in the component, not
the route, so the surrounding page is always public. Reads go through
``public_access`` (allowlisted, read-only) for signals; the real portfolio uses
the separate private ``portfolio_store``.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import portfolio_store, public_access
from app.auth import ENT_REAL_PORTFOLIO, ENT_SIGNALS, current_user
from app.config import get_config

router = APIRouter(prefix="/gated")


def render(request: Request, name: str, **ctx):
    ctx["user"] = current_user(request)
    return request.app.state.templates.TemplateResponse(request, name, ctx)


def _wall(request: Request, title: str, body: str):
    return render(request, "partials/access_wall.html", wall_title=title, wall_body=body)


@router.get("/strategy/{ac_slug}/{variant_slug}/signals", response_class=HTMLResponse)
async def strategy_signals(request: Request, ac_slug: str, variant_slug: str):
    user = current_user(request)
    if user is None or not user.has_entitlement(ENT_SIGNALS):
        return _wall(request, "Live signals are members-only",
                     "Sign in with an entitled account to see the current signal table.")

    cfg = get_config()
    ac = cfg.asset_class(ac_slug)
    variant = ac.variant(variant_slug) if ac else None
    if variant is None:
        return HTMLResponse('<div class="alert alert-danger mb-0">Unknown strategy.</div>', status_code=404)

    rows, columns, error = [], [], None
    try:
        df = public_access.read_data(f"{ac_slug}_signals", variant.signals_symbol, row_range=(0, 100))
        columns = [str(c) for c in df.columns]
        rows = df.head(100).astype(object).where(df.notna(), "").values.tolist()
    except public_access.AccessDenied:
        return _wall(request, "Unavailable", "This signal feed is not publicly accessible.")
    except Exception:  # noqa: BLE001
        error = "No live signals published yet for this strategy."

    return render(request, "partials/signals_table.html",
                  columns=columns, rows=rows, error=error, variant=variant)


@router.get("/portfolio/real", response_class=HTMLResponse)
async def real_portfolio(request: Request):
    user = current_user(request)
    if user is None or not user.has_entitlement(ENT_REAL_PORTFOLIO):
        return _wall(request, "Real portfolio is restricted",
                     "Positions, allocation and P&L from the live IBKR account are owner/admin only.")

    pf = portfolio_store.real_portfolio()
    if pf is None:
        return render(request, "partials/real_portfolio.html", pf=None)
    return render(request, "partials/real_portfolio.html", pf=pf)
