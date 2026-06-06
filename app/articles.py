"""Markdown-backed articles.

Posts live as ``content/articles/<slug>.md`` with a small ``---`` frontmatter
block (key: value lines) followed by a Markdown body. We parse frontmatter by
hand (no YAML dependency) and render the body with ``markdown``.

Frontmatter keys: ``title``, ``date`` (ISO), ``summary``, ``tags`` (comma list),
optional ``image`` (OG image path).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from functools import lru_cache

import markdown as _md

from app.config import CONTENT_DIR

ARTICLES_DIR = CONTENT_DIR / "articles"


@dataclass(frozen=True)
class Article:
    slug: str
    title: str
    date: str
    summary: str
    tags: tuple[str, ...]
    image: str | None
    body_md: str

    @property
    def date_display(self) -> str:
        try:
            return _dt.date.fromisoformat(self.date).strftime("%b %d, %Y")
        except ValueError:
            return self.date

    @property
    def html(self) -> str:
        return _md.markdown(
            self.body_md,
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


def _build(slug: str, text: str) -> Article:
    meta, body = _parse_frontmatter(text)
    tags = tuple(
        t.strip() for t in meta.get("tags", "").split(",") if t.strip()
    )
    return Article(
        slug=slug,
        title=meta.get("title", slug.replace("-", " ").title()),
        date=meta.get("date", ""),
        summary=meta.get("summary", ""),
        tags=tags,
        image=meta.get("image") or None,
        body_md=body,
    )


@lru_cache(maxsize=1)
def _all() -> tuple[Article, ...]:
    if not ARTICLES_DIR.exists():
        return ()
    items = [
        _build(p.stem, p.read_text(encoding="utf-8"))
        for p in ARTICLES_DIR.glob("*.md")
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
