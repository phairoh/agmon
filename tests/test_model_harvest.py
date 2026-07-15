"""Model harvest: ``runs.model`` is the **observed** model — derived from the
run's init system event — not the wrapper's ``--model`` *argument* (requested).

A run killed before init honestly stays null ("never observed"); requested
intent lands in meta as ``model_requested`` and is never used as a fallback.
See spec 007 (extracted from 006 §6).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agmon import db, runner
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


def _write_jsonl(runs_dir: Path, run_id: str, *events) -> None:
    (runs_dir / f"{run_id}.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events)
    )


def _model(conn: sqlite3.Connection, run_id: str):
    return conn.execute(
        "SELECT model FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()[0]


# -- ingest: observed model -------------------------------------------------


def test_init_event_populates_model_byte_exact(tmp_path):
    """The init event's model lands in the column, byte-exact."""
    config = _config(tmp_path)
    _write_meta(config.runs_dir, "r1", status="finished")
    _write_jsonl(
        config.runs_dir, "r1",
        {"type": "system", "subtype": "init", "session_id": "s1",
         "model": "claude-opus-4-8"},
        {"type": "result", "subtype": "success"},
    )
    db.init_db(config.db_path)
    ing = Ingester(config)
    try:
        ing.scan()
        assert _model(ing.conn, "r1") == "claude-opus-4-8"
    finally:
        ing.conn.close()


def test_no_init_event_leaves_model_null(tmp_path):
    """A run interrupted before its init event never observed a model."""
    config = _config(tmp_path)
    _write_meta(config.runs_dir, "r1", status="interrupted")
    _write_jsonl(
        config.runs_dir, "r1",
        {"type": "assistant", "message": {"role": "assistant"}},
    )
    db.init_db(config.db_path)
    ing = Ingester(config)
    try:
        ing.scan()
        assert _model(ing.conn, "r1") is None
    finally:
        ing.conn.close()


def test_requested_value_is_never_a_fallback(tmp_path):
    """model_requested set, no init event -> model stays null. Intent is not
    observation; the requested value must never backfill the column."""
    config = _config(tmp_path)
    _write_meta(config.runs_dir, "r1", status="interrupted",
                model_requested="claude-opus-4-8")
    _write_jsonl(
        config.runs_dir, "r1",
        {"type": "assistant", "message": {"role": "assistant"}},
    )
    db.init_db(config.db_path)
    ing = Ingester(config)
    try:
        ing.scan()
        assert _model(ing.conn, "r1") is None
    finally:
        ing.conn.close()


def test_replay_backfills_model_from_init(tmp_path):
    """A historical spool (legacy metas carrying the old ``model`` argument,
    no ``model_requested``) replays with model backfilled from each init event
    — and the init value wins over any legacy meta ``model``."""
    config = _config(tmp_path)
    # Legacy wrapper wrote the *argument* to meta `model`; the served model
    # differs (init). Observation must win.
    _write_meta(config.runs_dir, "r1", status="finished", model="opus-requested")
    _write_jsonl(
        config.runs_dir, "r1",
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
        {"type": "result", "subtype": "success"},
    )
    _write_meta(config.runs_dir, "r2", status="finished")
    _write_jsonl(
        config.runs_dir, "r2",
        {"type": "system", "subtype": "init", "model": "claude-opus-4-8"},
        {"type": "result", "subtype": "success"},
    )

    db.init_db(config.db_path)
    ing = Ingester(config)
    ing.scan()
    assert _model(ing.conn, "r1") == "claude-sonnet-5"  # init, not legacy meta
    assert _model(ing.conn, "r2") == "claude-opus-4-8"
    ing.conn.close()

    # Simulate a schema bump: stamp an older version, then drop-and-replay.
    conn = sqlite3.connect(config.db_path)
    conn.execute("UPDATE schema_meta SET version = 1")
    conn.commit()
    conn.close()
    db.init_db(config.db_path)
    ing2 = Ingester(config)
    try:
        assert ing2.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        ing2.scan()
        assert _model(ing2.conn, "r1") == "claude-sonnet-5"
        assert _model(ing2.conn, "r2") == "claude-opus-4-8"
    finally:
        ing2.conn.close()


# -- wrapper: requested model ----------------------------------------------


def _short_circuit_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)

    def _boom(*a, **k):
        raise FileNotFoundError  # bail before launching claude; meta still written

    monkeypatch.setattr(runner.subprocess, "Popen", _boom)


def test_wrapper_writes_model_requested_when_passed(tmp_path, monkeypatch):
    _short_circuit_launch(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        runner.main(["hello", "--cwd", str(tmp_path), "--model", "claude-opus-4-8"])
    meta = json.loads(next(tmp_path.glob("*.meta.json")).read_text())
    assert meta["model_requested"] == "claude-opus-4-8"
    assert "model" not in meta  # meta `model` is no longer written


def test_wrapper_omits_model_requested_when_absent(tmp_path, monkeypatch):
    _short_circuit_launch(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        runner.main(["hello", "--cwd", str(tmp_path)])
    meta = json.loads(next(tmp_path.glob("*.meta.json")).read_text())
    assert "model_requested" not in meta
    assert "model" not in meta
