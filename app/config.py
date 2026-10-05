"""Application configuration.

The SQLite database path is taken from the ``DB_PATH`` environment variable so
that both local runs (``./data/battery.db``) and container deployments
(``/data/battery.db``) work without code changes.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "battery.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(DEFAULT_DB_PATH)))

# A single pre-selection round may consider at most this many candidate cells
# (the task caps the pending pool at 12 cells per chemistry).
MAX_CANDIDATES = 12

# Floating-point tolerance for capacity / resistance spread comparisons.
EPS = 1e-9
