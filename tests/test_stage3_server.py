"""Stage-3 server-side changes: summary.result_text and the healthz move."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agmon.api import create_app
from agmon.config import Config


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
        yield runs_dir, client, ingester
    finally:
        ingester.conn.close()


def write_meta(runs_dir: Path, run_id: str, **fields) -> None:
    meta = {"run_id": run_id, "git": {"branch": "main", "commit": "abc123"}}
    meta.update(fields)
    (runs_dir / f"{run_id}.meta.json").write_text(json.dumps(meta))


def jsonl_lines(*events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def test_summary_includes_result_text(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-aaaaaa"
    write_meta(runs_dir, run_id, status="finished",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success",
             "result": "All done: the answer is 42."},
        )
    )
    ingester.scan()

    summary = client.get(f"/v1/runs/{run_id}/summary").json()
    assert summary["result_text"] == "All done: the answer is 42."


def test_summary_result_text_null_when_absent(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-bbbbbb"
    write_meta(runs_dir, run_id, status="running",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines({"type": "system", "subtype": "init"})
    )
    ingester.scan()

    summary = client.get(f"/v1/runs/{run_id}/summary").json()
    assert summary["result_text"] is None


def test_healthz_unversioned_and_v1_gone(env):
    _, client, _ = env
    ok = client.get("/healthz")
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert client.get("/v1/healthz").status_code == 404
