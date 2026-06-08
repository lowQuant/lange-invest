"""Public route group — articles, asset-class & strategy pages, model portfolio.

No auth. Reads ONLY precomputed snapshots (and markdown content); never touches
ArcticDB at request time. Gated components (live signals, real portfolio) are
embedded as HTMX fragments that fall back to an access wall — see app.routes.gated.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from app import articles as articles_mod
from app import futures_overview
from app.auth import current_user
from app.config import get_config
from app import snapshots

router = APIRouter()


def render(request: Request, name: str, **ctx):
    templates = request.app.state.templates
    ctx["user"] = current_user(request)
    return templates.TemplateResponse(request, name, ctx)


# ── Home ─────────────────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    cfg = get_config()
    return render(
        request,
        "home.html",
        nav_active="home",
        asset_classes=cfg.asset_classes,
        recent_articles=articles_mod.list_articles()[:3],
        model=snapshots.model_portfolio_snapshot(),
    )


# ── Articles ─────────────────────────────────────────────────────────────────
@router.get("/articles", response_class=HTMLResponse)
async def article_list(request: Request):
    return render(
        request,
        "articles_list.html",
        nav_active="articles",
        articles=articles_mod.list_articles(),
    )


@router.get("/articles/{slug}", response_class=HTMLResponse)
async def article_detail(request: Request, slug: str):
    article = articles_mod.get_article(slug)
    if article is None:
        raise HTTPException(status_code=404)
    return render(request, "article_detail.html", nav_active="articles", article=article)


# ── Portfolio (model when logged out; real handled by gated fragment) ─────────
@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio(request: Request):
    return render(
        request,
        "portfolio.html",
        nav_active="portfolio",
        model=snapshots.model_portfolio_snapshot(),
    )


# ── SEO ──────────────────────────────────────────────────────────────────────
@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    cfg = get_config()
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /mcp\n"
        "Disallow: /gated\n"
        f"Sitemap: https://{cfg.domain}/sitemap.xml\n"
    )


@router.get("/sitemap.xml")
async def sitemap(request: Request):
    cfg = get_config()
    base = f"https://{cfg.domain}"
    # Only index the surfaces that are actually populated. Equities and
    # Portfolio are admin-only / not-yet-public.
    public_slugs = {"futures"}
    urls = ["/", "/articles"]
    for ac in cfg.asset_classes:
        if ac.slug not in public_slugs:
            continue
        urls.append(f"/{ac.slug}")
        urls.extend(f"/{ac.slug}/{v.slug}" for v in ac.variants)
    urls.extend(f"/articles/{a.slug}" for a in articles_mod.list_articles())
    items = "".join(f"<url><loc>{base}{u}</loc></url>" for u in urls)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{items}</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


# ── Futures overview ─────────────────────────────────────────────────────────
# /futures has no strategy variants yet, so it renders a custom data-driven
# overview (sector-grouped continuous curves + term structures + trend table)
# instead of the generic asset-class landing. Declared BEFORE the /{ac_slug}
# catch-all so this route wins.
#
# Two-stage render: the page itself only reads metadata (one universe + one
# symbol list) so it appears instantly. The chart payload is fetched async
# via /futures/api/payload and cached server-side after first hit.
@router.get("/futures", response_class=HTMLResponse)
async def futures_overview_page(request: Request):
    cfg = get_config()
    ac = cfg.asset_class("futures")
    if ac is None:
        raise HTTPException(status_code=404)
    data = futures_overview.build_meta()
    return render(
        request,
        "futures_overview.html",
        nav_active="futures",
        asset_class=ac,
        sectors=data["sectors"],
        rows=data["rows"],
        error=data["error"],
    )


@router.get("/futures/api/payload")
async def futures_overview_payload():
    """Per-symbol chart data + trend metric, cached. Hydrates the /futures shell."""
    return JSONResponse(futures_overview.build_chart_payload())


# ── Asset-class landing + strategy pages (data-driven; declared LAST) ─────────
@router.get("/{ac_slug}", response_class=HTMLResponse)
async def asset_class_landing(request: Request, ac_slug: str):
    cfg = get_config()
    ac = cfg.asset_class(ac_slug)
    if ac is None:
        raise HTTPException(status_code=404)
    return render(
        request,
        "asset_class.html",
        nav_active=ac.slug,
        asset_class=ac,
        active_variant=None,
    )


@router.get("/{ac_slug}/{variant_slug}", response_class=HTMLResponse)
async def strategy_page(request: Request, ac_slug: str, variant_slug: str):
    cfg = get_config()
    ac = cfg.asset_class(ac_slug)
    if ac is None:
        raise HTTPException(status_code=404)
    variant = ac.variant(variant_slug)
    if variant is None:
        raise HTTPException(status_code=404)
    snap = snapshots.strategy_snapshot(ac.slug, variant.slug)
    return render(
        request,
        "strategy.html",
        nav_active=ac.slug,
        asset_class=ac,
        variant=variant,
        active_variant=variant.slug,
        snap=snap,
    )
