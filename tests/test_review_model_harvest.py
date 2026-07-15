"""Review-added characterization tests for spec 007 (model harvest).

These guard invariants the feature commit promises in prose but the feature
tests do not exercise — the *incremental* ingest paths (the feature tests are
single-scan or full-replay). They pass against the reviewed code; they exist to
pin down behavior a future refactor could silently break. Provenance: added by
an adversarial review, not the feature author.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agmon import db
from agmon.config import Config
from agmon.ingest import Ingester


def _config(tmp_path: Path) -> Config:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    return Config(
        runs_dir=runs_dir, db_path=tmp_path / "agmon.db",
        host="127.0.0.1", port=8400, stall_seconds=300,
    )


def _write_meta(runs_dir: Path, run_id: str, **fields) -> None:
    meta = {"run_id": run_id, "git": {"branch": "main", "commit": "abc123"}}
    meta.update(fields)
    (runs_dir / f"{run_id}.meta.json").write_text(json.dumps(meta))


def _model(conn: sqlite3.Connection, run_id: str):
    return conn.execute(
        "SELECT model FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()[0]


def test_meta_rewrite_after_init_does_not_clobber_model(tmp_path):
    """A meta.json rewritten after the model was observed (status running ->
    finished) must not clobber the observed model back to null — the meta path
    never owns the `model` column. Exercises the incremental re-ingest of meta
    that the feature tests never trigger."""
    config = _config(tmp_path)
    _write_meta(config.runs_dir, "r1", status="running")
    (config.runs_dir / "r1.jsonl").write_text(
        json.dumps({"type": "system", "subtype": "init",
                    "model": "claude-opus-4-8"}) + "\n"
    )
    db.init_db(config.db_path)
    ing = Ingester(config)
    try:
        ing.scan()
        assert _model(ing.conn, "r1") == "claude-opus-4-8"

        # Rewrite meta with a bumped mtime so _ingest_meta re-runs; the new meta
        # carries no model field. Model must survive.
        import os
        st = (config.runs_dir / "r1.meta.json").stat()
        _write_meta(config.runs_dir, "r1", status="finished")
        os.utime(config.runs_dir / "r1.meta.json",
                 (st.st_atime + 10, st.st_mtime + 10))
        ing.scan()
        assert _model(ing.conn, "r1") == "claude-opus-4-8"
        assert ing.conn.execute(
            "SELECT status FROM runs WHERE run_id=?", ("r1",)
        ).fetchone()[0] == "finished"
    finally:
        ing.conn.close()


def test_init_arriving_in_later_scan_populates_model(tmp_path):
    """The init event need not be present on the first scan: an append-only
    jsonl whose init line lands on a later scan still backfills the model. The
    feature tests only ever scan a file that already holds its init event."""
    config = _config(tmp_path)
    _write_meta(config.runs_dir, "r1", status="running")
    jsonl = config.runs_dir / "r1.jsonl"
    # First scan sees only a partial line (no trailing newline) -> no event yet.
    jsonl.write_text('{"type": "system", "subtype": "init", "model":')
    db.init_db(config.db_path)
    ing = Ingester(config)
    try:
        ing.scan()
        assert _model(ing.conn, "r1") is None

        # The init line completes on the next append; now it is observed.
        jsonl.write_text(
            json.dumps({"type": "system", "subtype": "init",
                        "model": "claude-sonnet-5"}) + "\n"
        )
        ing.scan()
        assert _model(ing.conn, "r1") == "claude-sonnet-5"
    finally:
        ing.conn.close()
