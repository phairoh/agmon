"""SQLite schema and connection helpers.

The ingester owns a single long-lived writer connection; HTTP handlers open
short-lived read-only connections.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Bump this whenever the schema changes. Migrations are drop-and-replay: on a
# version mismatch the db file is deleted and the whole spool re-ingested (the
# spool is the source of truth, the db a disposable index). See init_db.
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  session_id     TEXT,
  prompt         TEXT,
  cwd            TEXT,
  git_branch     TEXT,
  git_commit     TEXT,
  model          TEXT,
  host           TEXT,
  pid            INTEGER,
  started_at     TEXT,
  ended_at       TEXT,
  exit_code      INTEGER,
  status         TEXT,
  result_subtype TEXT,
  num_turns      INTEGER,
  total_cost_usd REAL,
  meta_json      TEXT
);

CREATE TABLE IF NOT EXISTS events (
  run_id      TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  ingested_at TEXT NOT NULL,
  type        TEXT,
  subtype     TEXT,
  payload     TEXT NOT NULL,
  is_error    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS ingest_state (
  path       TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL,
  byte_off   INTEGER NOT NULL,
  meta_mtime REAL
);

-- Flat key=value labels stamped at dispatch, re-derived from meta.json on every
-- upsert. Meaning (pipeline/phase/parent lineage) is a derivation-layer concern;
-- this table stays a plain string->string store. The (key, value) index serves
-- label= filters and the sibling/children lineage lookups.
CREATE TABLE IF NOT EXISTS run_labels (
  run_id TEXT NOT NULL,
  key    TEXT NOT NULL,
  value  TEXT NOT NULL,
  PRIMARY KEY (run_id, key)
);

CREATE INDEX IF NOT EXISTS idx_run_labels_kv ON run_labels (key, value);
"""


def _stored_version(db_path: Path) -> int | None:
    """The schema version recorded in an existing db, or None if the file is
    absent or predates the schema_meta table."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None  # schema_meta table missing (pre-v2 db)
    finally:
        conn.close()


def _drop_db(db_path: Path) -> None:
    """Delete the db file and its WAL/SHM sidecars."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def init_db(db_path: Path) -> None:
    """Create the parent dir, schema, and switch the db into WAL mode.

    Migrations are drop-and-replay: if an existing db's schema version is
    missing or does not match ``SCHEMA_VERSION``, the file (and its sidecars)
    is deleted and recreated empty, so the next scan re-ingests the whole
    spool from scratch (ingest offsets live in the dropped db).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() and _stored_version(db_path) != SCHEMA_VERSION:
        _drop_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO schema_meta (version) SELECT ? "
            "WHERE NOT EXISTS (SELECT 1 FROM schema_meta)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()


def writer(db_path: Path) -> sqlite3.Connection:
    """The single writer connection, used from the ingester thread."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def reader(db_path: Path) -> sqlite3.Connection:
    """A short-lived read-only connection for an HTTP handler."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
