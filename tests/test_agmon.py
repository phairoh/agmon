"""Tests for the agmon ingester and API.

Scans are driven directly via `app.state.ingester.scan()` rather than the
polling thread. The TestClient is *not* entered as a context manager, so the
lifespan (and its background thread) never starts.
"""

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


def test_complete_run(env):
    runs_dir, client, ingester = env
    run_id = "20260708T000000-aaaaaa"
    write_meta(
        runs_dir,
        run_id,
        prompt="say pong",
        status="finished",
        model="claude-opus-4-8",
        started_at="2026-07-08T00:00:00+00:00",
        num_turns=1,
        total_cost_usd=0.04,
        extra_field="kept-in-meta-json",
    )
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {"type": "assistant", "message": {"role": "assistant"}},
            {"type": "result", "subtype": "success", "total_cost_usd": 0.04},
        )
    )
    ingester.scan()

    # list endpoint
    r = client.get("/runs")
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 1
    item = runs[0]
    assert item["run_id"] == run_id
    assert item["status"] == "finished"
    assert item["git_branch"] == "main"
    assert item["event_count"] == 3
    assert item["last_event_type"] == "result"
    assert item["prompt_preview"] == "say pong"
    assert "prompt" not in item
    assert "meta_json" not in item
    assert item["pid_alive"] is None  # not running

    # detail endpoint
    r = client.get(f"/runs/{run_id}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["prompt"] == "say pong"
    assert detail["meta_json"]["extra_field"] == "kept-in-meta-json"
    assert detail["event_count"] == 3

    # events endpoint
    r = client.get(f"/runs/{run_id}/events")
    body = r.json()
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]
    assert body["events"][0]["type"] == "system"
    assert body["events"][0]["subtype"] == "init"
    assert body["events"][0]["payload"]["session_id"] == "s1"
    assert body["next_after"] == 3


def test_unknown_run_404(env):
    _, client, _ = env
    r = client.get("/runs/nope")
    assert r.status_code == 404
    assert r.json()["error"]


def test_partial_trailing_line(env):
    runs_dir, client, ingester = env
    run_id = "run-partial"
    path = runs_dir / f"{run_id}.jsonl"
    complete = json.dumps({"type": "system", "subtype": "init"}) + "\n"
    partial = json.dumps({"type": "assistant", "message": {}})  # no newline
    path.write_text(complete + partial)
    ingester.scan()

    r = client.get(f"/runs/{run_id}/events")
    events = r.json()["events"]
    assert len(events) == 1  # partial line not ingested
    assert events[0]["seq"] == 1

    # finish the partial line
    with path.open("a") as f:
        f.write("\n")
    ingester.scan()

    r = client.get(f"/runs/{run_id}/events")
    events = r.json()["events"]
    assert len(events) == 2  # exactly one added
    assert [e["seq"] for e in events] == [1, 2]  # contiguous


def test_append_resume(env):
    runs_dir, client, ingester = env
    run_id = "run-append"
    path = runs_dir / f"{run_id}.jsonl"
    path.write_text(jsonl_lines({"type": "system", "subtype": "init"}))
    ingester.scan()
    off1 = ingester.conn.execute(
        "SELECT byte_off FROM ingest_state WHERE run_id=?", (run_id,)
    ).fetchone()[0]

    with path.open("a") as f:
        f.write(jsonl_lines({"type": "assistant"}, {"type": "result"}))
    ingester.scan()
    off2 = ingester.conn.execute(
        "SELECT byte_off FROM ingest_state WHERE run_id=?", (run_id,)
    ).fetchone()[0]

    assert off2 > off1  # offset advanced
    events = client.get(f"/runs/{run_id}/events").json()["events"]
    assert [e["seq"] for e in events] == [1, 2, 3]  # no dupes, contiguous

    # a scan with no new bytes is a no-op
    ingester.scan()
    events = client.get(f"/runs/{run_id}/events").json()["events"]
    assert len(events) == 3


def test_stub_run_then_meta(env):
    runs_dir, client, ingester = env
    run_id = "run-stub"
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines({"type": "system", "subtype": "init"})
    )
    ingester.scan()

    # events ingested even though meta.json is absent
    detail = client.get(f"/runs/{run_id}").json()
    assert detail["run_id"] == run_id
    assert detail["status"] is None  # stub
    assert detail["event_count"] == 1

    # meta arrives later and fills the row in
    write_meta(runs_dir, run_id, prompt="hi", status="running", pid=999999999)
    ingester.scan()
    detail = client.get(f"/runs/{run_id}").json()
    assert detail["prompt"] == "hi"
    assert detail["status"] == "running"
    assert detail["event_count"] == 1  # events untouched
    # running + dead pid → surfaced honestly, not rewritten
    assert detail["pid_alive"] is False
    assert detail["status"] == "running"


def test_unparseable_line(env):
    runs_dir, client, ingester = env
    run_id = "run-bad"
    (runs_dir / f"{run_id}.jsonl").write_text(
        "{not json\n" + jsonl_lines({"type": "result"})
    )
    ingester.scan()
    events = client.get(f"/runs/{run_id}/events").json()["events"]
    assert events[0]["type"] == "_unparseable"
    assert events[0]["payload"] == "{not json"
    assert events[1]["type"] == "result"


def test_events_pagination(env):
    runs_dir, client, ingester = env
    run_id = "run-page"
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(*[{"type": "assistant", "n": i} for i in range(5)])
    )
    ingester.scan()

    body = client.get(f"/runs/{run_id}/events?limit=2").json()
    assert [e["seq"] for e in body["events"]] == [1, 2]
    assert body["next_after"] == 2

    body = client.get(
        f"/runs/{run_id}/events?after={body['next_after']}&limit=2"
    ).json()
    assert [e["seq"] for e in body["events"]] == [3, 4]
    assert body["next_after"] == 4

    body = client.get(f"/runs/{run_id}/events?after=4").json()
    assert [e["seq"] for e in body["events"]] == [5]
    assert body["next_after"] == 5

    # exhausted: empty page echoes the requested after
    body = client.get(f"/runs/{run_id}/events?after=5").json()
    assert body["events"] == []
    assert body["next_after"] == 5
