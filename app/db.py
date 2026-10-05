"""SQLite persistence layer.

Schema overview
---------------
``cells``
    Every pending / occupied cell. ``occupied_group`` is NULL while the cell is
    available; once a group is confirmed it points at ``groups.id`` and is
    frozen for reuse (a trigger forbids flipping it back to NULL).

``groups``
    One row per confirmed pack. Insert-only — triggers forbid UPDATE/DELETE —
    so the release record itself can never be rewritten.

``group_snapshots``
    Immutable, append-only per-cell inspection snapshot taken at confirmation.
    ``UNIQUE(cell_id)`` makes double-booking impossible at the storage level.
    The group row plus its snapshots are queried by ``group_no`` and are
    unaffected by later revisions of ``cells``.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS cells (
    id               TEXT PRIMARY KEY,
    chemistry        TEXT NOT NULL,
    capacity_ah      REAL NOT NULL CHECK (capacity_ah > 0),
    resistance_mohm  REAL NOT NULL CHECK (resistance_mohm >= 0),
    test_version     TEXT NOT NULL,
    occupied_group   INTEGER REFERENCES groups(id)
);

CREATE TABLE IF NOT EXISTS groups (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    chemistry            TEXT NOT NULL,
    min_size            INTEGER NOT NULL,
    max_size            INTEGER NOT NULL,
    min_capacity_ah     REAL NOT NULL,
    max_capacity_delta  REAL NOT NULL,
    max_res_delta       REAL NOT NULL,
    limiting_capacity   REAL NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS group_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id         INTEGER NOT NULL REFERENCES groups(id),
    cell_id          TEXT NOT NULL UNIQUE,
    position         INTEGER NOT NULL,
    chemistry        TEXT NOT NULL,
    capacity_ah      REAL NOT NULL,
    resistance_mohm  REAL NOT NULL,
    test_version     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_group ON group_snapshots(group_id);
CREATE INDEX IF NOT EXISTS idx_cells_available
    ON cells(chemistry, occupied_group);
"""

# A confirmed release must be tamper-proof: neither the group record nor its
# snapshots may ever be changed or removed.
_IMMUTABILITY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_groups_no_update
BEFORE UPDATE ON groups
BEGIN
    SELECT RAISE(ABORT, 'confirmed groups are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_groups_no_delete
BEFORE DELETE ON groups
BEGIN
    SELECT RAISE(ABORT, 'confirmed groups cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_snapshots_no_update
BEFORE UPDATE ON group_snapshots
BEGIN
    SELECT RAISE(ABORT, 'inspection snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_snapshots_no_delete
BEFORE DELETE ON group_snapshots
BEGIN
    SELECT RAISE(ABORT, 'inspection snapshots cannot be deleted');
END;

-- A cell once assigned to a confirmed group stays assigned; revisions may
-- update its measured values/version but can never free it for reuse.
CREATE TRIGGER IF NOT EXISTS trg_cells_no_release
BEFORE UPDATE OF occupied_group ON cells
WHEN OLD.occupied_group IS NOT NULL AND NEW.occupied_group IS NULL
BEGIN
    SELECT RAISE(ABORT, 'occupied cells cannot be released');
END;
"""


def get_conn(db_path=None) -> sqlite3.Connection:
    path = str(db_path if db_path is not None else DB_PATH)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Commit-on-success / rollback-on-error transaction.

    ``immediate`` opens with ``BEGIN IMMEDIATED`` (write lock acquired up front)
    which is what the confirmation flow needs to serialize racing confirmations.
    """
    if conn.in_transaction:
        # Nested usage: let the outer transaction own commit/rollback.
        yield conn
        return
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("BEGIN")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(db_path=None) -> None:
    path = db_path if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn(path)
    try:
        conn.executescript(SCHEMA)
        conn.executescript(_IMMUTABILITY_TRIGGERS)
        conn.commit()
    finally:
        conn.close()
