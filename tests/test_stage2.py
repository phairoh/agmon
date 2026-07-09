"""Integration tests for stage-2 endpoints and replay-as-migration.

Scans are driven directly via ``ingester.scan()``; the polling thread and
lifespan are never started.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agmon import db
from agmon.api import create_app
from agmon.config import Config
from agmon.ingest import Ingester


@pytest.fixture()
def env(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    config = Config(
        runs_dir=runs_dir,
        db_path=tmp_path / "agmon.db",
        host="127.0.0.1",
        port=8400,
        stall_seconds=300,
    )
    app = create_app(config)
    client = TestClient(app)
    ingester = app.state.ingester
    try:
        yield runs_dir, client, ingester, config
    finally:
        ingester.conn.close()


def write_meta(runs_dir: Path, run_id: str, **fields) -> None:
    meta = {"run_id": run_id, "git": {"branch": "main", "commit": "abc123"}}
    meta.update(fields)
    (runs_dir / f"{run_id}.meta.json").write_text(json.dumps(meta))


def jsonl_lines(*events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


# -- list endpoint: effective_status + issue_count ---------------------------


def test_list_effective_status_and_issue_count(env):
    runs_dir, client, ingester, _ = env
    run_id = "20260708T000000-aaaaaa"
    write_meta(
        runs_dir,
        run_id,
        prompt="do a thing",
        status="error",
        result_subtype=None,  # -> interrupted
        started_at="2026-07-08T00:00:00+00:00",
    )
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "boom"}}
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "nope"}
            ]}},
            {"type": "result", "subtype": "error_during_execution"},
        )
    )
    ingester.scan()

    item = client.get("/v1/runs").json()["runs"][0]
    assert item["effective_status"] == "interrupted"
    assert item["stalled_seconds"] is None
    # two is_error events: the tool_result and the non-success result
    assert item["issue_count"] == 2

    # summary agrees
    summary = client.get(f"/v1/runs/{run_id}/summary").json()
    assert summary["status"]["effective_status"] == "interrupted"
    cats = {i["category"] for i in summary["issues"]}
    assert cats == {"tool_error", "run_error"}
    assert summary["metrics"]["tool_counts"] == {"Bash": 1}


def test_events_errors_only_filter(env):
    runs_dir, client, ingester, _ = env
    run_id = "run-errs"
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init"},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "bad"}
            ]}},
            {"type": "result", "subtype": "success"},
        )
    )
    ingester.scan()

    all_events = client.get(f"/v1/runs/{run_id}/events").json()["events"]
    assert len(all_events) == 3
    errs = client.get(f"/v1/runs/{run_id}/events?errors_only=true").json()["events"]
    assert [e["seq"] for e in errs] == [2]


# -- cost rollup -------------------------------------------------------------


def test_cost_rollup_three_days_with_null(env):
    runs_dir, client, ingester, _ = env
    rows = [
        ("r1", "2026-07-06T01:00:00+00:00", 0.10, 3),
        ("r2", "2026-07-06T05:00:00+00:00", 0.20, 2),
        ("r3", "2026-07-07T09:00:00+00:00", None, 5),  # null cost still counts as a run
        ("r4", "2026-07-08T23:30:00+00:00", 0.05, 1),
    ]
    for run_id, started, cost, turns in rows:
        write_meta(
            runs_dir, run_id, status="finished", started_at=started,
            total_cost_usd=cost, num_turns=turns,
        )
    ingester.scan()

    body = client.get(
        "/v1/stats/costs?since=2026-07-01T00:00:00+00:00&until=2026-07-09T00:00:00+00:00"
    ).json()
    buckets = {b["bucket"]: b for b in body["buckets"]}
    assert set(buckets) == {"2026-07-06", "2026-07-07", "2026-07-08"}
    assert buckets["2026-07-06"]["runs"] == 2
    assert buckets["2026-07-06"]["total_cost_usd"] == pytest.approx(0.30)
    assert buckets["2026-07-06"]["total_turns"] == 5
    assert buckets["2026-07-07"]["runs"] == 1
    assert buckets["2026-07-07"]["total_cost_usd"] == 0  # null contributes 0
    assert buckets["2026-07-08"]["total_cost_usd"] == 0.05

    assert body["totals"]["runs"] == 4
    assert body["totals"]["total_cost_usd"] == pytest.approx(0.35)
    assert body["totals"]["total_turns"] == 11


# -- replay-as-migration -----------------------------------------------------


def _all_events(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT run_id, seq, type, subtype, payload, is_error FROM events "
        "ORDER BY run_id, seq"
    ).fetchall()


def test_replay_as_migration_rebuilds_identical_events(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    config = Config(
        runs_dir=runs_dir, db_path=tmp_path / "agmon.db",
        host="127.0.0.1", port=8400, stall_seconds=300,
    )
    write_meta(runs_dir, "m1", status="finished", started_at="2026-07-08T00:00:00+00:00")
    (runs_dir / "m1.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init"},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "bad"}
            ]}},
            {"type": "result", "subtype": "success"},
        )
    )

    db.init_db(config.db_path)
    ing = Ingester(config)
    ing.scan()
    before = [tuple(r) for r in _all_events(ing.conn)]
    ing.conn.close()
    assert any(r[5] == 1 for r in before)  # is_error populated

    # Simulate a version mismatch: stamp an older schema version into the db.
    conn = sqlite3.connect(config.db_path)
    conn.execute("UPDATE schema_meta SET version = 1")
    conn.commit()
    conn.close()

    # Restart: init_db must drop-and-replay, next scan rebuilds from the spool.
    db.init_db(config.db_path)
    assert db._stored_version(config.db_path) == db.SCHEMA_VERSION
    ing2 = Ingester(config)
    # offsets were reset (db dropped), so a fresh scan re-ingests everything.
    assert ing2.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    ing2.scan()
    after = [tuple(r) for r in _all_events(ing2.conn)]
    ing2.conn.close()

    assert after == before  # same seqs, payloads, is_error flags
