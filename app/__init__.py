"""lange-invest application package.

Puts the vendored ``core`` package (from arcticdb-viewer) on the import path so
``from core import operations`` resolves to ``vendor/core``. We vendor rather
than fork: the engine is the viewer's, untouched, and remains the single path
to ArcticDB.
"""
from __future__ import annotations

import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
