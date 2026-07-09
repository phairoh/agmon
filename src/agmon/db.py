"""SQLite schema and connection helpers.

The ingester owns a single long-lived writer connection; HTTP handlers open
short-lived read-only connections.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
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
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS ingest_state (
  path       TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL,
  byte_off   INTEGER NOT NULL,
  meta_mtime REAL
);
"""


def init_db(db_path: Path) -> None:
    """Create the parent dir, schema, and switch the db into WAL mode."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
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
