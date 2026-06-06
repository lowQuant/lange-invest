"""Admin authoring routes: add / remove articles (admin-only).

Registered BEFORE the public router so GET /articles/new wins over the public
/articles/{slug} detail route.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import articles as A
from app.auth import current_user

router = APIRouter()


def _require_admin(request: Request):
    u = current_user(request)
    if u is None or not u.is_admin:
        raise HTTPException(status_code=403)
    return u


def render(request: Request, name: str, **ctx):
    ctx["user"] = current_user(request)
    return request.app.state.templates.TemplateResponse(request, name, ctx)


@router.get("/articles/new", response_class=HTMLResponse)
async def new_form(request: Request):
    _require_admin(request)
    return render(request, "article_new.html", nav_active="articles",
                  today=dt.date.today().isoformat(), article=None)


@router.get("/articles/{slug}/edit", response_class=HTMLResponse)
async def edit_form(request: Request, slug: str):
    _require_admin(request)
    article = A.get_article(slug)
    if article is None:
        raise HTTPException(status_code=404)
    return render(request, "article_new.html", nav_active="articles",
                  today=dt.date.today().isoformat(), article=article)


@router.post("/articles/new", response_class=HTMLResponse)
async def create(request: Request, title: str = Form(...), body: str = Form(""),
                 date: str = Form(""), summary: str = Form(""), tags: str = Form(""),
                 category: str = Form(""), format: str = Form("markdown"),
                 slug: str = Form("")):
    _require_admin(request)
    if not title.strip():
        raise HTTPException(status_code=400, detail="Title required")
    new_slug = A.save_article(title=title, body=body, date=date, summary=summary,
                              tags=tags, category=category, fmt=format,
                              slug=(slug.strip() or None))
    return RedirectResponse(f"/articles/{new_slug}", status_code=303)


@router.delete("/articles/{slug}", response_class=HTMLResponse)
async def delete(request: Request, slug: str):
    _require_admin(request)
    A.delete_article(slug)
    return HTMLResponse("", headers={"HX-Trigger": '{"showToast":{"message":"Article deleted"}}'})
