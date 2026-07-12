"""Stage-4a: labels (the spool primitive) and pipeline lineage (derived).

Five sections mirroring the spec: the strict wrapper, the lenient ingester,
pure lineage derivation, the API surface, and the CLI. Wrapper/derivation are
driven purely; ingest/API use the same direct-scan fixture as the other
integration suites; CLI injects a stub client + StringIO per the house pattern.
"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agmon import cli, db, derive, runner
from agmon.api import create_app
from agmon.client import resolve
from agmon.config import Config
from agmon.ingest import Ingester
from agmon.labels import MAX_LABELS, build_labels

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


# ============================================================================
# 1. Wrapper — build_labels (strict) and the meta.json round-trip
# ============================================================================


def test_valid_labels_compile_and_round_trip():
    labels = build_labels(["team=infra", "urgent=yes"])
    assert labels == {"team": "infra", "urgent": "yes"}
    # flat string->string, so JSON round-trips identically.
    assert json.loads(json.dumps(labels)) == labels


def test_sugar_compiles_to_reserved_labels():
    labels = build_labels(
        ["team=infra"], pipeline="p1", phase="build", parent="20260709T010101-abc123"
    )
    assert labels == {
        "team": "infra",
        "pipeline": "p1",
        "phase": "build",
        "parent": "20260709T010101-abc123",
    }


def test_no_labels_is_empty_dict():
    assert build_labels(None) == {}


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        (dict(label_args=["noequals"]), "expected KEY=VALUE"),
        (dict(label_args=["Bad=v"]), "invalid label key"),          # uppercase key
        (dict(label_args=["k=" ]), "non-empty"),                    # empty value
        (dict(label_args=["k=" + "x" * 257]), "exceeds 256"),       # value too long
        (dict(label_args=["k=a\tb"]), "control characters"),        # control char
        (dict(label_args=["k=1", "k=2"]), "duplicate label key"),   # dup explicit
    ],
)
def test_each_constraint_has_a_distinct_error(kwargs, needle):
    with pytest.raises(ValueError) as exc:
        build_labels(**kwargs)
    assert needle in str(exc.value)


def test_sugar_and_explicit_same_key_is_duplicate():
    with pytest.raises(ValueError) as exc:
        build_labels(["pipeline=a"], pipeline="b")
    assert "duplicate label key 'pipeline'" in str(exc.value)


def test_too_many_labels_rejected():
    ok = [f"k{i}=v" for i in range(MAX_LABELS)]
    assert len(build_labels(ok)) == MAX_LABELS
    with pytest.raises(ValueError) as exc:
        build_labels(ok + ["extra=v"])
    assert "too many labels" in str(exc.value)


def test_key_max_length_boundary():
    assert build_labels(["a" * 64 + "=v"])  # 64 ok
    with pytest.raises(ValueError):
        build_labels(["a" * 65 + "=v"])  # 65 rejected


def test_wrapper_writes_labels_to_meta_json(tmp_path, monkeypatch):
    """A dispatch stamps ``labels`` into meta.json (empty object when none)."""
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)

    def _boom(*a, **k):
        raise FileNotFoundError  # short-circuit before launching claude

    monkeypatch.setattr(runner.subprocess, "Popen", _boom)
    with pytest.raises(SystemExit):
        runner.main(["hello", "--cwd", str(tmp_path), "--pipeline", "p1", "--phase", "spec"])

    meta = json.loads(next(tmp_path.glob("*.meta.json")).read_text())
    assert meta["labels"] == {"pipeline": "p1", "phase": "spec"}


def test_wrapper_bad_label_exits_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    with pytest.raises(SystemExit) as exc:
        runner.main(["hello", "--label", "BAD=x"])
    assert "invalid label key" in str(exc.value)


# ============================================================================
# 2. Ingest — labels land in run_labels; lenient; replay repopulates
# ============================================================================


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


def _labels_in_db(conn: sqlite3.Connection, run_id: str) -> dict:
    return {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM run_labels WHERE run_id=?", (run_id,)
        )
    }


def test_ingest_labels_land_in_run_labels(tmp_path):
    config = _config(tmp_path)
    _write_meta(config.runs_dir, "r1", status="finished",
                labels={"pipeline": "p1", "phase": "build"})
    db.init_db(config.db_path)
    ing = Ingester(config)
    try:
        ing.scan()
        assert _labels_in_db(ing.conn, "r1") == {"pipeline": "p1", "phase": "build"}
    finally:
        ing.conn.close()


def test_ingest_is_lenient_on_bad_entries(tmp_path, caplog):
    """One invalid + two valid entries -> the two valid land, a line is logged,
    the file is not stalled."""
    config = _config(tmp_path)
    _write_meta(
        config.runs_dir, "r1", status="finished",
        labels={"pipeline": "p1", "BADKEY": "x", "phase": "review"},
    )
    db.init_db(config.db_path)
    ing = Ingester(config)
    try:
        with caplog.at_level("WARNING"):
            ing.scan()
        assert _labels_in_db(ing.conn, "r1") == {"pipeline": "p1", "phase": "review"}
        assert any("BADKEY" in rec.message or "skipping label" in rec.message
                   for rec in caplog.records)
    finally:
        ing.conn.close()


def test_ingest_non_object_labels_ignored(tmp_path):
    config = _config(tmp_path)
    _write_meta(config.runs_dir, "r1", status="finished", labels=["not", "a", "dict"])
    db.init_db(config.db_path)
    ing = Ingester(config)
    try:
        ing.scan()
        assert _labels_in_db(ing.conn, "r1") == {}
    finally:
        ing.conn.close()


def test_replay_after_version_bump_repopulates_labels(tmp_path):
    config = _config(tmp_path)
    _write_meta(config.runs_dir, "r1", status="finished",
                labels={"pipeline": "p1", "phase": "build"})
    db.init_db(config.db_path)
    ing = Ingester(config)
    ing.scan()
    before = _labels_in_db(ing.conn, "r1")
    ing.conn.close()

    # Simulate a stale db and restart: drop-and-replay must rebuild labels.
    conn = sqlite3.connect(config.db_path)
    conn.execute("UPDATE schema_meta SET version = 1")
    conn.commit()
    conn.close()

    db.init_db(config.db_path)
    ing2 = Ingester(config)
    try:
        assert ing2.conn.execute("SELECT COUNT(*) FROM run_labels").fetchone()[0] == 0
        ing2.scan()
        assert _labels_in_db(ing2.conn, "r1") == before == {"pipeline": "p1", "phase": "build"}
    finally:
        ing2.conn.close()


# ============================================================================
# 3. Derivation — derive_lineage (pure)
# ============================================================================


def _related(run_id, labels, effective_status="finished", started_at="2026-07-09T00:00:00+00:00"):
    return {"run_id": run_id, "labels": labels,
            "effective_status": effective_status, "started_at": started_at}


def test_lineage_null_without_reserved_keys():
    assert derive.derive_lineage("r1", {"team": "infra"}, []) is None
    assert derive.derive_lineage("r1", {}, []) is None


def test_lineage_children_from_parent_labels():
    related = [
        _related("child_a", {"parent": "r1"}),
        _related("child_b", {"parent": "r1"}),
        _related("other", {"parent": "somethingelse"}),
    ]
    lin = derive.derive_lineage("r1", {"phase": "spec"}, related)
    assert lin["children"] == ["child_a", "child_b"]
    assert lin["phase"] == "spec"
    assert lin["pipeline"] is None
    assert lin["siblings"] == []  # no pipeline -> no siblings


def test_lineage_siblings_share_pipeline_and_exclude_self():
    related = [
        _related("r1", {"pipeline": "P"}, started_at="2026-07-09T00:00:00+00:00"),  # self, ignored
        _related("sib_old", {"pipeline": "P", "phase": "spec"}, "finished", "2026-07-09T01:00:00+00:00"),
        _related("sib_new", {"pipeline": "P", "phase": "review"}, "running", "2026-07-09T03:00:00+00:00"),
        _related("elsewhere", {"pipeline": "Q"}, started_at="2026-07-09T02:00:00+00:00"),
    ]
    lin = derive.derive_lineage("r1", {"pipeline": "P", "phase": "build"}, related)
    # self excluded, other-pipeline excluded, oldest-first.
    assert [s["run_id"] for s in lin["siblings"]] == ["sib_old", "sib_new"]
    assert lin["siblings"][0] == {
        "run_id": "sib_old", "phase": "spec",
        "effective_status": "finished", "started_at": "2026-07-09T01:00:00+00:00",
    }
    assert lin["siblings"][1]["effective_status"] == "running"


def test_lineage_parent_nonexistent_renders_as_is():
    lin = derive.derive_lineage("r1", {"parent": "ghost-run", "phase": "build"}, [])
    assert lin["parent"] == "ghost-run"  # surfaced verbatim, not validated
    assert lin["children"] == []


# ============================================================================
# 4. API — filters, labels in payloads, summary lineage
# ============================================================================


@pytest.fixture()
def api_env(tmp_path: Path):
    config = _config(tmp_path)
    app = create_app(config)
    client = TestClient(app)
    ingester = app.state.ingester
    try:
        yield config.runs_dir, client, ingester
    finally:
        ingester.conn.close()


def test_label_filters_single_and_multiple_and(api_env):
    runs_dir, client, ingester = api_env
    _write_meta(runs_dir, "a", status="finished", started_at="2026-07-09T01:00:00+00:00",
                labels={"pipeline": "P", "phase": "build"})
    _write_meta(runs_dir, "b", status="finished", started_at="2026-07-09T02:00:00+00:00",
                labels={"pipeline": "P", "phase": "spec"})
    _write_meta(runs_dir, "c", status="finished", started_at="2026-07-09T03:00:00+00:00",
                labels={"pipeline": "Q", "phase": "build"})
    ingester.scan()

    # single filter
    got = client.get("/v1/runs", params={"label": "pipeline=P"}).json()["runs"]
    assert {r["run_id"] for r in got} == {"a", "b"}

    # AND across two filters
    got = client.get(
        "/v1/runs", params={"label": ["pipeline=P", "phase=build"]}
    ).json()["runs"]
    assert {r["run_id"] for r in got} == {"a"}
    assert got[0]["labels"] == {"pipeline": "P", "phase": "build"}


def test_malformed_label_filter_is_400(api_env):
    _, client, _ = api_env
    r = client.get("/v1/runs", params={"label": "nokeyvalue"})
    assert r.status_code == 400
    assert "expected key=value" in r.json()["error"]


def test_labels_in_list_and_detail(api_env):
    runs_dir, client, ingester = api_env
    _write_meta(runs_dir, "a", status="finished", labels={"team": "infra"})
    _write_meta(runs_dir, "b", status="finished")  # no labels
    ingester.scan()

    by_id = {r["run_id"]: r for r in client.get("/v1/runs").json()["runs"]}
    assert by_id["a"]["labels"] == {"team": "infra"}
    assert by_id["b"]["labels"] == {}  # empty object, not missing

    assert client.get("/v1/runs/a").json()["labels"] == {"team": "infra"}
    assert client.get("/v1/runs/b").json()["labels"] == {}


def test_summary_lineage_three_phase_pipeline(api_env):
    runs_dir, client, ingester = api_env
    _write_meta(runs_dir, "spec_run", status="finished",
                started_at="2026-07-09T01:00:00+00:00",
                labels={"pipeline": "P", "phase": "spec"})
    _write_meta(runs_dir, "build_run", status="finished",
                started_at="2026-07-09T02:00:00+00:00",
                labels={"pipeline": "P", "phase": "build", "parent": "spec_run"})
    _write_meta(runs_dir, "review_run", status="finished",
                started_at="2026-07-09T03:00:00+00:00",
                labels={"pipeline": "P", "phase": "review", "parent": "build_run"})
    ingester.scan()

    lin = client.get("/v1/runs/build_run/summary").json()["lineage"]
    assert lin["pipeline"] == "P"
    assert lin["phase"] == "build"
    assert lin["parent"] == "spec_run"
    assert lin["children"] == ["review_run"]  # review's parent = build_run
    assert [s["run_id"] for s in lin["siblings"]] == ["spec_run", "review_run"]
    assert {s["phase"] for s in lin["siblings"]} == {"spec", "review"}
    # every sibling carries a status + started stamp
    assert all(s["effective_status"] and s["started_at"] for s in lin["siblings"])


def test_summary_lineage_null_when_unlabeled(api_env):
    runs_dir, client, ingester = api_env
    _write_meta(runs_dir, "plain", status="finished")
    ingester.scan()
    assert client.get("/v1/runs/plain/summary").json()["lineage"] is None


def test_pipeline_and_resume_lineage_not_conflated(api_env):
    """A run can carry both a session resume chain and a pipeline; the summary
    keeps them in separate blocks."""
    runs_dir, client, ingester = api_env
    _write_meta(runs_dir, "r1", status="finished", session_id="s1",
                started_at="2026-07-09T01:00:00+00:00",
                labels={"pipeline": "P", "phase": "build"})
    ingester.scan()
    body = client.get("/v1/runs/r1/summary").json()
    assert body["lineage"]["pipeline"] == "P"        # pipeline lineage
    assert body["run"]["session_id"] == "s1"          # resume lineage lives elsewhere
    assert "session_id" not in body["lineage"]


# ============================================================================
# 5. CLI — ls filter plumbing; show Pipeline section
# ============================================================================


class LsStub:
    def __init__(self, runs):
        self._runs = runs
        self.last_labels = None

    def list_runs(self, *, status=None, limit=50, session=None, labels=None):
        self.last_labels = labels
        return self._runs[:limit]


class ShowStub:
    def __init__(self, runs, summary):
        self._runs = runs
        self._summary = summary

    def all_runs(self):
        return self._runs

    def get_summary(self, run_id):
        return self._summary


def _run_cli(argv, client, *, tty=False):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, client=client, out=out, err=err, tty=tty, now=NOW,
                    sleep=lambda *_: None)
    return code, out.getvalue(), err.getvalue()


def test_ls_label_flags_plumb_to_query():
    stub = LsStub([])
    code, _, _ = _run_cli(
        ["ls", "--pipeline", "P", "--phase", "build", "--label", "team=infra"], stub
    )
    assert code == 0
    assert set(stub.last_labels) == {"pipeline=P", "phase=build", "team=infra"}


def test_ls_pipeline_filter_shows_phase_column():
    stub = LsStub([
        {"run_id": "20260709T010000-aaaaaa", "labels": {"phase": "build"},
         "effective_status": "finished", "issue_count": 0},
    ])
    _, out, _ = _run_cli(["ls", "--pipeline", "P", "--plain"], stub)
    header = out.splitlines()[0].split("\t")
    assert "phase" in header
    assert "build" in out


def test_ls_unlabeled_fleet_has_no_labels_column():
    stub = LsStub([
        {"run_id": "20260709T010000-aaaaaa", "effective_status": "finished",
         "issue_count": 0},
    ])
    _, out, _ = _run_cli(["ls", "--plain"], stub)
    header = out.splitlines()[0].split("\t")
    assert header == cli.render.LS_HEADERS  # no labels/phase noise


_LINEAGE_SUMMARY = {
    "run": {"run_id": "20260709T115700-a3f9c1", "session_id": "s1",
            "started_at": "2026-07-09T11:57:00+00:00", "prompt": "hi"},
    "status": {"effective_status": "finished", "stalled_seconds": None},
    "activity": {"last_tool": None, "last_text": None, "progress": None},
    "issues": [], "metrics": {}, "result_text": None,
    "lineage": {
        "pipeline": "P", "phase": "build", "parent": "20260709T090000-parent1",
        "children": ["20260709T130000-child01"],
        "siblings": [
            {"run_id": "20260709T090000-spec001", "phase": "spec",
             "effective_status": "finished", "started_at": "2026-07-09T09:00:00+00:00"},
        ],
    },
}


def test_show_renders_pipeline_section_when_lineage_present():
    runs = [{"run_id": "20260709T115700-a3f9c1", "session_id": "s1",
             "started_at": "2026-07-09T11:57:00+00:00"}]
    stub = ShowStub(runs, _LINEAGE_SUMMARY)
    _, out, _ = _run_cli(["show", "a3f9c1"], stub, tty=False)
    assert "Pipeline" in out
    assert "build" in out            # this run's phase
    assert "parent1" in out          # short parent id
    assert "child01" in out          # short child id
    assert "spec001" in out          # sibling short id


def test_show_omits_pipeline_section_when_no_lineage():
    summary = dict(_LINEAGE_SUMMARY, lineage=None)
    runs = [{"run_id": "20260709T115700-a3f9c1", "session_id": "s1",
             "started_at": "2026-07-09T11:57:00+00:00"}]
    stub = ShowStub(runs, summary)
    _, out, _ = _run_cli(["show", "a3f9c1"], stub, tty=False)
    assert "Pipeline" not in out
