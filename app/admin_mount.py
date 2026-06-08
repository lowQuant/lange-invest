"""Mount the arcticdb-viewer full-CRUD web app under /admin, behind auth.

The viewer is reused wholesale: we import its FastAPI app and wrap it in an ASGI
guard that requires an authenticated admin user. Unauthenticated/non-admin
requests never reach the viewer — they are redirected to /login.

The viewer is imported from a configurable location so deployment can pip-install
it (``pip install git+https://github.com/lowQuant/arcticdb-viewer``) or point at a
checkout via ``ADMIN_VIEWER_PATH``. If it can't be imported, a small fallback app
is mounted instead so the rest of the site is unaffected.

Config (env):
    ADMIN_VIEWER_MODULE  default "web.app:app"
    ADMIN_VIEWER_PATH    optional dir to add to sys.path before importing
"""
from __future__ import annotations

import importlib
import os
import sys

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from app.auth import current_user


class AdminAuthGuard:
    """ASGI middleware: only authenticated admins pass through to the viewer."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        user = current_user(request)
        if user is None or not user.is_admin:
            target = "/login?next=/admin"
            status = 303 if user is None else 403
            if status == 403:
                resp = HTMLResponse(
                    "<h3>403 — admin only</h3><p>This area requires an admin account.</p>",
                    status_code=403,
                )
            else:
                resp = RedirectResponse(target, status_code=303)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _import_viewer_app():
    module_spec = os.getenv("ADMIN_VIEWER_MODULE", "web.app:app")
    path = os.getenv("ADMIN_VIEWER_PATH")
    if path and path not in sys.path:
        sys.path.insert(0, path)
    mod_name, _, attr = module_spec.partition(":")
    module = importlib.import_module(mod_name)
    return getattr(module, attr or "app")


def _fallback_app() -> Starlette:
    # The standalone arcticdb-viewer isn't installed — admins use the in-app
    # ArcticDB tab (full CRUD) instead, so send /admin there.
    async def to_arcticdb(request: Request):
        return RedirectResponse("/arcticdb", status_code=303)

    return Starlette(routes=[Route("/{path:path}", to_arcticdb)])


def build_admin_app():
    """Return the guarded ASGI app to mount at /admin."""
    try:
        viewer = _import_viewer_app()
    except Exception as exc:  # noqa: BLE001
        print(f"[admin] standalone viewer not installed ({type(exc).__name__}); "
              f"/admin redirects to the in-app ArcticDB tab.")
        viewer = _fallback_app()
    return AdminAuthGuard(viewer)
