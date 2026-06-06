"""Private read path for the REAL (IBKR) portfolio.

This is deliberately separate from ``public_access`` and the snapshot reader.
Real-account data is as sensitive as the hidden IBKR libraries: it is NEVER on
the public allowlist, NEVER written to public snapshots, and is reachable only
by the gated Portfolio route after an entitlement check.

The IBKR ingestion task (``scripts/ingest_ibkr.py``) normalises the latest
subscribed Flex/activity report into ``PRIVATE_DIR/real_portfolio.json``; this
module is the only reader. The private dir is git-ignored.
"""
from __future__ import annotations

import json
from typing import Any

from app.config import PRIVATE_DIR

REAL_PORTFOLIO_FILE = PRIVATE_DIR / "real_portfolio.json"


def real_portfolio() -> dict[str, Any] | None:
    """Return the normalised real-portfolio snapshot, or ``None`` if absent."""
    if not REAL_PORTFOLIO_FILE.exists():
        return None
    try:
        return json.loads(REAL_PORTFOLIO_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
