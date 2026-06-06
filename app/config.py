"""Configuration loader for lange-invest.

Reads the declarative taxonomy + allowlist from ``config/site.toml`` and the
runtime secrets/settings from the environment. Nothing here touches ArcticDB.

The loaded config is cached; call :func:`reload` in tests to pick up changes.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# Repo root = parent of the ``app`` package directory.
ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "site.toml"
CONTENT_DIR = ROOT / "content"
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", str(ROOT / "data" / "snapshots")))
PRIVATE_DIR = Path(os.getenv("PRIVATE_DIR", str(ROOT / "data" / "private")))


@dataclass(frozen=True)
class Variant:
    slug: str
    name: str
    blurb: str
    methodology: str
    signals_symbol: str
    stats_symbol: str


@dataclass(frozen=True)
class AssetClass:
    slug: str
    name: str
    icon: str
    blurb: str
    variants: list[Variant] = field(default_factory=list)

    def variant(self, slug: str) -> Variant | None:
        return next((v for v in self.variants if v.slug == slug), None)


@dataclass(frozen=True)
class DatabaseLibrary:
    name: str
    label: str
    description: str
    chart: bool


@dataclass(frozen=True)
class SiteConfig:
    name: str
    tagline: str
    domain: str
    public_libraries: tuple[str, ...]
    protected_libraries: tuple[str, ...]
    asset_classes: tuple[AssetClass, ...]
    database_libraries: tuple[DatabaseLibrary, ...]

    def asset_class(self, slug: str) -> AssetClass | None:
        return next((a for a in self.asset_classes if a.slug == slug), None)

    def database_library(self, name: str) -> DatabaseLibrary | None:
        return next((d for d in self.database_libraries if d.name == name), None)


def _load() -> SiteConfig:
    with CONFIG_FILE.open("rb") as fh:
        raw = tomllib.load(fh)

    site = raw.get("site", {})
    access = raw.get("access", {})

    asset_classes: list[AssetClass] = []
    for ac in raw.get("asset_class", []):
        variants = [
            Variant(
                slug=v["slug"],
                name=v["name"],
                blurb=v.get("blurb", ""),
                methodology=v.get("methodology", ""),
                signals_symbol=v.get("signals_symbol", v["slug"].replace("-", "_")),
                stats_symbol=v.get("stats_symbol", v["slug"].replace("-", "_")),
            )
            for v in ac.get("variant", [])
        ]
        asset_classes.append(
            AssetClass(
                slug=ac["slug"],
                name=ac["name"],
                icon=ac.get("icon", "bi-grid"),
                blurb=ac.get("blurb", ""),
                variants=variants,
            )
        )

    database_libraries = tuple(
        DatabaseLibrary(
            name=d["name"],
            label=d.get("label", d["name"]),
            description=d.get("description", ""),
            chart=bool(d.get("chart", False)),
        )
        for d in raw.get("database_library", [])
    )

    return SiteConfig(
        name=site.get("name", "lange-invest"),
        tagline=site.get("tagline", ""),
        domain=site.get("domain", "lange-invest.com"),
        public_libraries=tuple(access.get("public_libraries", [])),
        protected_libraries=tuple(access.get("protected_libraries", [])),
        asset_classes=tuple(asset_classes),
        database_libraries=database_libraries,
    )


@lru_cache(maxsize=1)
def get_config() -> SiteConfig:
    return _load()


def reload() -> SiteConfig:
    """Clear the cache and reload (used by tests)."""
    get_config.cache_clear()
    return get_config()
