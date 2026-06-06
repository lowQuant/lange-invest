#!/usr/bin/env python3
"""Import blog posts from the old Django blog sqlite into content/articles.

Posts are CKEditor HTML with inline base64 images. We:
  * extract base64 data-URI images to app/static/blog/<slug>/NN.<ext> and rewrite src;
  * write each post as content/articles/<slug>.html with a frontmatter block;
  * skip empty/"test" posts.

Usage: python scripts/import_blog.py /path/to/blog.sqlite3
"""
from __future__ import annotations

import base64
import html
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTICLES = ROOT / "content" / "articles"
STATIC_BLOG = ROOT / "app" / "static" / "blog"

DATA_URI = re.compile(r"data:image/([A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+?)(?=[\"'\)])")
TAG = re.compile(r"<[^>]+>")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:80] or "post"


def strip_html(s: str) -> str:
    return html.unescape(TAG.sub("", s or "")).strip()


def extract_images(body: str, slug: str) -> str:
    imgs = list(DATA_URI.finditer(body))
    if not imgs:
        return body
    outdir = STATIC_BLOG / slug
    outdir.mkdir(parents=True, exist_ok=True)
    for i, m in enumerate(imgs):
        ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(m.group(1), m.group(1))
        raw = re.sub(r"\s+", "", m.group(2))
        try:
            data = base64.b64decode(raw + "=" * (-len(raw) % 4))
        except Exception:
            continue
        (outdir / f"{i}.{ext}").write_bytes(data)
        body = body.replace(m.group(0), f"/static/blog/{slug}/{i}.{ext}")
    return body


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else None
    if not db or not Path(db).exists():
        raise SystemExit("Pass the path to the blog sqlite3 file.")

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    posts = con.execute(
        "SELECT title, summary, body, tags, post_date, category FROM myblog_post ORDER BY post_date DESC"
    ).fetchall()

    written = 0
    for p in posts:
        title = (p["title"] or "").strip()
        body = (p["body"] or "").strip()
        if not title or len(strip_html(body)) < 200 or title.lower() == "test":
            continue
        slug = slugify(title)
        body = extract_images(body, slug)
        summary = strip_html(p["summary"]) or strip_html(body)[:200]
        tags = ", ".join(t.strip() for t in (p["tags"] or "").split(",") if t.strip())
        fm = (
            "---\n"
            f"title: {title}\n"
            f"date: {p['post_date']}\n"
            f"summary: {summary[:240]}\n"
            f"tags: {tags}\n"
            f"category: {p['category'] or ''}\n"
            "format: html\n"
            "---\n"
        )
        ARTICLES.mkdir(parents=True, exist_ok=True)
        (ARTICLES / f"{slug}.html").write_text(fm + body, encoding="utf-8")
        written += 1
        print(f"wrote {slug}.html  ({len(body)//1024}kb, {title[:50]})")
    print(f"\nimported {written} posts")


if __name__ == "__main__":
    main()
