"""public_access — the read-only allowlist gate in front of ``core.operations``.

Every public, gated, and MCP *read*-scope entry point goes through here and
NEVER imports ``core.operations`` directly. This module:

  * enforces the central allowlist from ``config/site.toml``;
  * refuses to list, read, describe, or even *enumerate* non-allowlisted
    libraries (including the protected IBKR / real-account libraries);
  * exposes read-only operations only — no write/update/append/delete, no
    library create/delete.

``core`` (and therefore ``arcticdb``) is imported lazily so the public,
snapshot-only deployment can boot without the ArcticDB driver installed.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.config import get_config


class AccessDenied(Exception):
    """Raised when a caller targets a library outside the public allowlist."""


def _ops():
    # Lazy import: keeps arcticdb out of the boot path for snapshot-only serving.
    from core import operations as ops  # noqa: WPS433 (intentional local import)

    return ops


# ── Allowlist primitives ─────────────────────────────────────────────────────

def is_public(library: str) -> bool:
    """True iff ``library`` is on the public allowlist."""
    return library in get_config().public_libraries


def ensure_public(library: str) -> None:
    """Raise :class:`AccessDenied` unless ``library`` is allowlisted.

    The error message deliberately does not reveal whether a non-allowlisted
    library exists — it is rejected identically whether real or not.
    """
    if not is_public(library):
        raise AccessDenied(f"Library {library!r} is not publicly accessible.")


# ── Read-only operations (allowlist-enforced) ────────────────────────────────

def list_libraries() -> list[str]:
    """List ONLY allowlisted libraries that actually exist.

    We never enumerate the full ArcticDB library set to the public surface —
    the allowlist is intersected with reality, not exposed wholesale.
    """
    allow = set(get_config().public_libraries)
    try:
        existing = set(_ops().list_libraries())
    except Exception:
        # Driver unavailable (snapshot-only deploy) — fall back to the allowlist
        # so callers still see the *intended* public surface, never extra libs.
        return [lib for lib in get_config().public_libraries]
    return [lib for lib in get_config().public_libraries if lib in allow & existing]


def list_symbols(library: str) -> list[str]:
    ensure_public(library)
    return _ops().list_symbols(library)


def describe_symbol(library: str, symbol: str) -> dict[str, Any]:
    ensure_public(library)
    return _ops().get_description(library, symbol)


def read_data(
    library: str,
    symbol: str,
    row_range: tuple[int, int] | None = None,
    columns: list[str] | None = None,
    date_range: tuple | None = None,
) -> pd.DataFrame:
    ensure_public(library)
    return _ops().read_data(
        library,
        symbol,
        row_range=row_range,
        columns=columns,
        date_range=date_range,
    )
