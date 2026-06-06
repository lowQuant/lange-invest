"""lange-invest — single deployable web app.

Route groups (one app, one domain):
    public/  articles, asset-class & strategy pages, model portfolio — no auth
    gated/   signals + real-portfolio fragments — auth + 2FA + entitlement (Phase 4)
    admin/   the mounted arcticdb-viewer — auth + admin role (Phase 5)
    mcp/     stateless Streamable-HTTP MCP endpoint — scoped bearer tokens (Phase 6)

Everything funnels through ``app.public_access`` (read-only + allowlist) for the
public/gated read path; the engine itself is the vendored viewer ``core``.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_config

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="lange-invest")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ── Template globals available to every render ──
def _site():
    return get_config()


templates.env.globals["site"] = _site()
# Expose the template object so routers can share it.
app.state.templates = templates


@app.exception_handler(404)
async def not_found(request: Request, exc):  # noqa: ANN001
    from app.auth import current_user

    ctx = {"user": current_user(request), "nav_active": None}
    return templates.TemplateResponse(request, "404.html", ctx, status_code=404)


@app.get("/healthz", response_class=HTMLResponse)
async def healthz():
    return "ok"


# ── Admin: mount the arcticdb-viewer wholesale, behind an admin-only guard ──
from fastapi.responses import RedirectResponse  # noqa: E402

from app.admin_mount import build_admin_app  # noqa: E402


@app.get("/admin")
async def admin_root_redirect():
    # Starlette Mount matches "/admin/..."; normalise the bare path.
    return RedirectResponse("/admin/", status_code=307)


app.mount("/admin", build_admin_app())


# ── Routers ──
# Auth + gated fragments register BEFORE the public catch-all `/{ac_slug}`,
# which is greedy and must stay LAST.
from app.routes import auth_routes, gated, public  # noqa: E402

app.include_router(auth_routes.router)
app.include_router(gated.router)
app.include_router(public.router)
