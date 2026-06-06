"""Engine connection bootstrap for live reads (the Database tab).

The public Database browser reads ArcticDB live (through ``public_access``), so
the web process needs an active connection. We connect the shared
ConnectionManager once, idempotently, from either:

    LANGE_DB_URI   an explicit ArcticDB URI (e.g. lmdb:///path or s3s://...), or
    .env S3 vars   (AWS_*/BUCKET_NAME) via ConnectionManager.detect_env_connection.

If neither is available the app still boots (snapshot-only); Database routes then
surface a friendly "engine not connected" state instead of erroring.
"""
from __future__ import annotations

import os


def ensure_connected() -> bool:
    """Connect the engine if possible. Returns True if connected. Idempotent."""
    from core.connection import ConnectionManager, get_manager

    mgr = get_manager()
    if mgr.is_connected:
        return True

    uri = os.getenv("LANGE_DB_URI")
    name = "lange-db"
    if not uri:
        env = ConnectionManager.detect_env_connection()
        if env:
            uri, name = env["uri"], env["name"]
    if not uri:
        return False
    try:
        mgr.connect(uri, name=name)
        return True
    except Exception:  # noqa: BLE001
        return False
