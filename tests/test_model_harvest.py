"""Spec 007 — observed model harvest.

`runs.model` is derived at ingest from the run's init system event (observed);
meta never populates the column. The wrapper records the `--model` argument as
the additive meta field `model_requested`, retrievable via the detail's
meta_json passthrough. See specs/007-model-harvest.md.

Scans are driven directly via the ingester; the wrapper is invoked in-process
with Popen short-circuited, mirroring test_labels.py.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agmon import cli, db, runner
from agmon.api import create_app
from agmon.config import Config
from agmon.ingest import Ingester

OBSERVED = "claude-opus-4-8[1m]"  # resolved identity, variant suffix included


@pytest.fixture()
def env(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    config = Config(
        runs_dir=runs_dir,
        db_path=tmp_path / "agmon.db",
        host="127.0.0.1",
        port=8400,
    )
    app = create_app(config)
    client = TestClient(app)
    ingester = app.state.ingester
    try:
        yield runs_dir, client, ingester
    finally:
        ingester.conn.close()


def write_meta(runs_dir: Path, run_id: str, **fields) -> None:
    meta = {"run_id": run_id, "git": {"branch": "main", "commit": "abc123"}}
    meta.update(fields)
    (runs_dir / f"{run_id}.meta.json").write_text(json.dumps(meta))


def jsonl_lines(*events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def _db_model(conn, run_id: str):
    return conn.execute(
        "SELECT model FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()[0]


# ============================================================================
# Wrapper — --model becomes meta model_requested; meta model is gone
# ============================================================================


def _run_wrapper(tmp_path, monkeypatch, argv):
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)

    def _boom(*a, **k):
        raise FileNotFoundError  # short-circuit before launching claude

    monkeypatch.setattr(runner.subprocess, "Popen", _boom)
    with pytest.raises(SystemExit):
        runner.main(argv)
    return json.loads(next(tmp_path.glob("*.meta.json")).read_text())


def test_wrapper_writes_model_requested_when_flag_passed(tmp_path, monkeypatch):
    meta = _run_wrapper(
        tmp_path, monkeypatch, ["hello", "--cwd", str(tmp_path), "--model", "opus"]
    )
    assert meta["model_requested"] == "opus"
    # Assert on the parsed dict, not the JSON text: meta["argv"] legitimately
    # contains the literal --model flag.
    assert "model" not in meta


def test_wrapper_omits_model_requested_without_flag(tmp_path, monkeypatch):
    meta = _run_wrapper(tmp_path, monkeypatch, ["hello", "--cwd", str(tmp_path)])
    assert "model_requested" not in meta
    assert "model" not in meta


# ============================================================================
# Ingest — model is observed (init event), never requested
# ============================================================================


def test_init_event_model_populates_column_byte_exact(env):
    runs_dir, client, ingester = env
    run_id = "20260717T000000-aaaaaa"
    write_meta(runs_dir, run_id, prompt="say pong", status="finished")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init", "session_id": "s1", "model": OBSERVED},
            {"type": "assistant", "message": {"role": "assistant"}},
            {"type": "result", "subtype": "success"},
        )
    )
    ingester.scan()

    item = client.get("/v1/runs").json()["runs"][0]
    assert item["model"] == OBSERVED
    assert client.get(f"/v1/runs/{run_id}").json()["model"] == OBSERVED
    assert client.get(f"/v1/runs/{run_id}/summary").json()["run"]["model"] == OBSERVED


def test_no_init_event_stays_null(env):
    """Interrupted pre-init: null means "never observed" — that's the signal."""
    runs_dir, client, ingester = env
    write_meta(runs_dir, "r-events", status="error", result_subtype=None)
    (runs_dir / "r-events.jsonl").write_text(
        jsonl_lines({"type": "assistant", "message": {"role": "assistant"}})
    )
    write_meta(runs_dir, "r-bare", status="error", result_subtype=None)  # no .jsonl
    ingester.scan()

    models = {r["run_id"]: r["model"] for r in client.get("/v1/runs").json()["runs"]}
    assert models == {"r-events": None, "r-bare": None}


def test_model_requested_is_never_a_fallback(env):
    runs_dir, client, ingester = env
    run_id = "20260717T000000-cccccc"
    write_meta(runs_dir, run_id, status="error", result_subtype=None,
               model_requested="opus")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines({"type": "assistant", "message": {"role": "assistant"}})
    )
    ingester.scan()

    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["model"] is None
    # Requested intent stays retrievable via the meta passthrough.
    assert detail["meta_json"]["model_requested"] == "opus"


def test_legacy_meta_model_key_does_not_populate_column(env):
    """Old spools wrote meta ``model`` (the argument); the column must come
    from the init event, not that key."""
    runs_dir, client, ingester = env
    run_id = "20260717T000000-dddddd"
    write_meta(runs_dir, run_id, status="finished", model="claude-sonnet-5")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init", "model": OBSERVED},
            {"type": "result", "subtype": "success"},
        )
    )
    ingester.scan()
    assert client.get(f"/v1/runs/{run_id}").json()["model"] == OBSERVED


def test_live_meta_rewrite_does_not_clobber_model(env):
    """The wrapper rewrites meta.json as the run progresses; later meta ingests
    must not null the observed model back out."""
    runs_dir, client, ingester = env
    run_id = "20260717T000000-eeeeee"
    write_meta(runs_dir, run_id, status="running")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines({"type": "system", "subtype": "init", "model": OBSERVED})
    )
    ingester.scan()
    assert client.get(f"/v1/runs/{run_id}").json()["model"] == OBSERVED

    write_meta(runs_dir, run_id, status="finished")
    # The meta-mtime guard is `<=`; force a strictly newer mtime so the
    # rewrite is actually re-ingested rather than skipped.
    meta_path = runs_dir / f"{run_id}.meta.json"
    st = meta_path.stat()
    os.utime(meta_path, (st.st_atime, st.st_mtime + 10))
    ingester.scan()

    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["status"] == "finished"  # the rewrite did land
    assert detail["model"] == OBSERVED


def test_first_init_event_wins(env):
    runs_dir, client, ingester = env
    run_id = "20260717T000000-ffffff"
    write_meta(runs_dir, run_id, status="finished")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init", "model": OBSERVED},
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
        )
    )
    ingester.scan()
    assert client.get(f"/v1/runs/{run_id}").json()["model"] == OBSERVED


# ============================================================================
# Replay — drop-and-replay backfills history (the point of the schema bump)
# ============================================================================


def test_replay_backfills_model_from_historical_spool(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    config = Config(
        runs_dir=runs_dir, db_path=tmp_path / "agmon.db",
        host="127.0.0.1", port=8400,
    )
    # Historical spool: metas predate model_requested; r1 carries the legacy
    # meta ``model`` key (differing from the init event's), r2 has no init.
    write_meta(runs_dir, "r1", status="finished", model="claude-sonnet-5")
    (runs_dir / "r1.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init", "model": OBSERVED},
            {"type": "result", "subtype": "success"},
        )
    )
    write_meta(runs_dir, "r2", status="error", result_subtype=None)
    (runs_dir / "r2.jsonl").write_text(
        jsonl_lines({"type": "assistant", "message": {"role": "assistant"}})
    )
    db.init_db(config.db_path)
    ing = Ingester(config)
    ing.scan()
    assert _db_model(ing.conn, "r1") == OBSERVED
    assert _db_model(ing.conn, "r2") is None
    ing.conn.close()

    # Simulate a stale db and restart: drop-and-replay must re-teach every
    # init-bearing run which model served it.
    conn = sqlite3.connect(config.db_path)
    conn.execute("UPDATE schema_meta SET version = 1")
    conn.commit()
    conn.close()

    db.init_db(config.db_path)
    ing2 = Ingester(config)
    try:
        assert ing2.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        ing2.scan()
        assert _db_model(ing2.conn, "r1") == OBSERVED
        assert _db_model(ing2.conn, "r2") is None
    finally:
        ing2.conn.close()


# ============================================================================
# Surfacing — the existing column renders in `agmon show` and via --fields
# ============================================================================

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)

SUMMARY = {
    "run": {"run_id": "20260717T000000-aaaaaa", "model": OBSERVED,
            "started_at": "2026-07-17T11:00:00+00:00", "prompt": "hi"},
    "status": {"effective_status": "finished"},
    "activity": {},
    "issues": [],
    "metrics": {},
}


class StubClient:
    def all_runs(self):
        return [SUMMARY["run"]]

    def get_summary(self, run_id):
        return SUMMARY


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, client=StubClient(), out=out, err=err, tty=False, now=NOW)
    return code, out.getvalue()


def test_show_renders_model():
    code, out = _run_cli(["show"])
    assert code == 0
    assert OBSERVED in out


def test_show_fields_reaches_model():
    code, out = _run_cli(["show", "--fields", "run.model"])
    assert code == 0
    header, row = out.splitlines()
    assert header == "run.model"
    assert row == OBSERVED
