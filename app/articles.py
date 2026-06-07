"""Markdown-backed articles.

Posts live as ``content/articles/<slug>.md`` with a small ``---`` frontmatter
block (key: value lines) followed by a Markdown body. We parse frontmatter by
hand (no YAML dependency) and render the body with ``markdown``.

Frontmatter keys: ``title``, ``date`` (ISO), ``summary``, ``tags`` (comma list),
optional ``image`` (OG image path).
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import re as _re
from dataclasses import dataclass
from functools import lru_cache

import markdown as _md

from app.config import ARTICLES_USER_DIR, CONTENT_DIR

# Seed articles committed to git; user-authored articles live in a git-ignored dir.
ARTICLES_DIR = CONTENT_DIR / "articles"
USER_DIR = ARTICLES_USER_DIR
TOMBSTONE = USER_DIR / ".deleted.json"


def slugify(title: str) -> str:
    s = _re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return _re.sub(r"-{2,}", "-", s)[:80] or "post"


@dataclass(frozen=True)
class Article:
    slug: str
    title: str
    date: str
    summary: str
    tags: tuple[str, ...]
    image: str | None
    body: str
    is_html: bool
    category: str = ""

    @property
    def date_display(self) -> str:
        try:
            return _dt.date.fromisoformat(self.date).strftime("%b %d, %Y")
        except ValueError:
            return self.date

    @property
    def html(self) -> str:
        if self.is_html:
            return self.body
        return _md.markdown(
            self.body,
            extensions=["fenced_code", "tables", "toc", "sane_lists"],
        )


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip()
            body = text[end + 4 :].lstrip("\n")
            for line in block.splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip().lower()] = val.strip()
    return meta, body


def _build(slug: str, text: str, is_html: bool) -> Article:
    meta, body = _parse_frontmatter(text)
    tags = tuple(
        t.strip() for t in meta.get("tags", "").split(",") if t.strip()
    )
    fmt = meta.get("format", "").lower()
    return Article(
        slug=slug,
        title=meta.get("title", slug.replace("-", " ").title()),
        date=meta.get("date", ""),
        summary=meta.get("summary", ""),
        tags=tags,
        image=meta.get("image") or None,
        body=body,
        is_html=is_html or fmt == "html",
        category=meta.get("category", ""),
    )


def _load_tombstones() -> set[str]:
    if TOMBSTONE.exists():
        try:
            return set(_json.loads(TOMBSTONE.read_text()))
        except (ValueError, OSError):
            return set()
    return set()


def _save_tombstones(slugs: set[str]) -> None:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    TOMBSTONE.write_text(_json.dumps(sorted(slugs)))


@lru_cache(maxsize=1)
def _all() -> tuple[Article, ...]:
    # Gather seed (tracked) then user (git-ignored) files; user overrides by slug.
    by_slug: dict[str, tuple] = {}
    for d in (ARTICLES_DIR, USER_DIR):
        if not d.exists():
            continue
        for p in list(d.glob("*.md")) + list(d.glob("*.html")):
            by_slug[p.stem] = (p, p.suffix == ".html")
    tomb = _load_tombstones()
    items = [
        _build(slug, p.read_text(encoding="utf-8"), is_html=is_html)
        for slug, (p, is_html) in by_slug.items()
        if slug not in tomb
    ]
    # Newest first; undated articles sink to the bottom.
    items.sort(key=lambda a: a.date or "", reverse=True)
    return tuple(items)


def list_articles() -> list[Article]:
    return list(_all())


def get_article(slug: str) -> Article | None:
    return next((a for a in _all() if a.slug == slug), None)


def reload() -> None:
    _all.cache_clear()


# ── Admin authoring (write/remove markdown-or-HTML article files) ─────────────

def save_article(title: str, body: str, date: str = "", summary: str = "",
                 tags: str = "", category: str = "", fmt: str = "markdown",
                 slug: str | None = None) -> str:
    """Create/overwrite an article file. Returns the slug."""
    slug = slug or slugify(title)
    ext = "html" if fmt == "html" else "md"
    fm = (
        "---\n"
        f"title: {title.strip()}\n"
        f"date: {date or _dt.date.today().isoformat()}\n"
        f"summary: {summary.strip()}\n"
        f"tags: {tags.strip()}\n"
        f"category: {category.strip()}\n"
        f"format: {'html' if ext == 'html' else 'markdown'}\n"
        "---\n"
    )
    USER_DIR.mkdir(parents=True, exist_ok=True)
    # Write to the user (git-ignored) dir; remove the other extension there.
    other = USER_DIR / f"{slug}.{'md' if ext == 'html' else 'html'}"
    if other.exists():
        other.unlink()
    (USER_DIR / f"{slug}.{ext}").write_text(fm + (body or ""), encoding="utf-8")
    # Un-tombstone (e.g. re-publishing a previously deleted seed article).
    tomb = _load_tombstones()
    if slug in tomb:
        _save_tombstones(tomb - {slug})
    reload()
    return slug


def delete_article(slug: str) -> bool:
    """Delete a user article; for a committed (seed) article, tombstone it so the
    tracked file is never touched (keeps `git pull` conflict-free on the server)."""
    removed = False
    for ext in ("md", "html"):
        p = USER_DIR / f"{slug}.{ext}"
        if p.exists():
            p.unlink()
            removed = True
    seed_exists = any((ARTICLES_DIR / f"{slug}.{ext}").exists() for ext in ("md", "html"))
    if seed_exists:
        _save_tombstones(_load_tombstones() | {slug})
        removed = True
    reload()
    return removed
