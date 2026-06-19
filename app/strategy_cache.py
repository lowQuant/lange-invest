"""Disk-backed cache for the expensive full-universe strategy compute.

The Donchian + loser-filter signal replay reads *every* symbol in the public
``futures`` library, builds two continuous series per market (the outright and
its c1−c2 calendar spread), and replays a state machine over each. That is far
too slow to run on every page load, so the sizing-independent result is cached
to a local JSON file under ``data/cache/`` and reused until the underlying data
changes.

Validity rule (mirrors the product requirement):

  * **Data unchanged** — the ``futures`` library fingerprint (per-symbol row
    count + last-update + date range) matches what we computed against → reuse.
  * **Same day, can't fingerprint** — the engine is momentarily unavailable so
    we can't recompute a fingerprint, but we already computed today → reuse the
    last result rather than fail.
  * **New data** — the fingerprint changed → recompute and overwrite the cache.

The cache is a *derived* artifact (recomputable from ArcticDB at any time), so
it lives outside git like the other runtime caches.  It is keyed only on public
``futures`` data, so nothing private is ever written here.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

# data/cache/ — sibling of data/snapshots and data/private. Git-ignored.
_CACHE_DIR = Path(os.getenv("LANGE_CACHE_DIR") or Path(__file__).resolve().parent.parent / "data" / "cache")


def _path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_"))
    return _CACHE_DIR / f"{safe}.json"


def _today() -> str:
    return _dt.date.today().isoformat()


def load(name: str) -> dict[str, Any] | None:
    """Read a cached payload, or ``None`` if absent/corrupt."""
    p = _path(name)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — a corrupt cache is just a miss
        return None


def write(name: str, payload: dict[str, Any]) -> None:
    """Atomically write a payload to the cache (best-effort; never raises)."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same dir, then rename — readers never see
        # a half-written file.
        fd, tmp = tempfile.mkstemp(dir=_CACHE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, _path(name))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception:  # noqa: BLE001 — caching is an optimisation, not load-bearing
        pass


def _is_valid(cached: dict[str, Any], fingerprint: str | None) -> bool:
    if fingerprint is not None:
        # Authoritative path: reuse iff the data is byte-for-byte the version we
        # computed against. A changed fingerprint means new data → recompute.
        return cached.get("fingerprint") == fingerprint
    # Fingerprint unavailable (engine hiccup): fall back to the "same day"
    # heuristic so we don't throw away a perfectly good same-session result.
    return cached.get("computed_date") == _today()


def get_or_compute(
    name: str,
    fingerprint: str | None,
    compute: Callable[[], dict[str, Any]],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Return a cached payload if still valid, else compute, persist, and return.

    ``compute`` produces the payload dict; this wrapper stamps it with the
    ``fingerprint`` and the compute date and writes it to disk. ``force``
    bypasses the cache entirely (used by the manual "Recompute" control) and
    overwrites whatever was there.
    """
    if not force:
        cached = load(name)
        if cached is not None and _is_valid(cached, fingerprint):
            cached["source"] = "cache"
            return cached

    payload = compute()
    payload["fingerprint"] = fingerprint
    payload["computed_date"] = _today()
    payload["computed_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    write(name, payload)
    payload["source"] = "computed"
    return payload
