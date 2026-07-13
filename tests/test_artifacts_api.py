"""Stage-4b server surface: the artifact catalog + content endpoints, summary
``decisions``, and the model-harvest ingest change.

Scans are driven directly via the ingester; the TestClient lifespan (and its
polling thread) is never entered.
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


def _assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _write_block(path, content, _id="w"):
    return {"type": "tool_use", "id": _id, "name": "Write",
            "input": {"file_path": path, "content": content}}


def _edit_block(path, old, new, replace_all=False, _id="e"):
    return {"type": "tool_use", "id": _id, "name": "Edit",
            "input": {"replace_all": replace_all, "file_path": path,
                      "old_string": old, "new_string": new}}


PROMPT = "task text\n\n## FOCUS\nfocus body\n\n## OVERRIDES\noverride body\n"
RESULT = "final answer\n\n## DECISIONS\ndecided this\n"


def _full_run(runs_dir, ingester, run_id="20260709T000000-full"):
    write_meta(runs_dir, run_id, prompt=PROMPT, status="finished",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init", "model": "claude-opus-4-8[1m]"},
            _assistant(_write_block("/wt/REVIEW.md", "the review text")),
            {"type": "result", "subtype": "success", "result": RESULT},
        )
    )
    ingester.scan()
    return run_id


# -- catalog endpoint --------------------------------------------------------


def test_artifacts_catalog_lists_all_families(env):
    runs_dir, client, ingester = env
    run_id = _full_run(runs_dir, ingester)
    body = client.get(f"/v1/runs/{run_id}/artifacts").json()
    by_name = {a["name"]: a for a in body["artifacts"]}
    assert by_name["prompt"]["kind"] == "dispatch"
    assert by_name["prompt.focus"]["kind"] == "section" and by_name["prompt.focus"]["available"]
    assert by_name["prompt.overrides"]["available"] is True
    assert by_name["result.decisions"]["available"] is True
    rev = by_name["/wt/REVIEW.md"]
    assert rev["kind"] == "file" and rev["available"] is True
    assert rev["first_op"] == "write" and rev["reconstructable"] is True


def test_artifacts_bare_run_available_false_with_reasons(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-bare"
    write_meta(runs_dir, run_id, prompt="plain prompt", status="running",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines({"type": "system", "subtype": "init"})
    )
    ingester.scan()
    body = client.get(f"/v1/runs/{run_id}/artifacts").json()
    by_name = {a["name"]: a for a in body["artifacts"]}
    assert by_name["prompt"]["available"] is True
    assert by_name["result"]["available"] is False and by_name["result"]["reason"]
    assert by_name["prompt.focus"]["available"] is False and by_name["prompt.focus"]["reason"]
    assert not any(a["kind"] == "file" for a in body["artifacts"])


def test_artifacts_unknown_run_404(env):
    _, client, _ = env
    assert client.get("/v1/runs/nope/artifacts").status_code == 404


# -- content endpoint --------------------------------------------------------


def test_content_dispatch_and_section(env):
    runs_dir, client, ingester = env
    run_id = _full_run(runs_dir, ingester)
    r = client.get(f"/v1/runs/{run_id}/artifacts/content", params={"name": "prompt"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == PROMPT
    r = client.get(f"/v1/runs/{run_id}/artifacts/content",
                   params={"name": "prompt.overrides"})
    assert r.text == "override body"


def test_content_file_by_basename(env):
    runs_dir, client, ingester = env
    run_id = _full_run(runs_dir, ingester)
    r = client.get(f"/v1/runs/{run_id}/artifacts/content", params={"name": "REVIEW.md"})
    assert r.status_code == 200
    assert r.text == "the review text"


def test_content_unknown_name_404(env):
    runs_dir, client, ingester = env
    run_id = _full_run(runs_dir, ingester)
    r = client.get(f"/v1/runs/{run_id}/artifacts/content", params={"name": "zzz"})
    assert r.status_code == 404
    assert r.json()["error"]


def test_content_unavailable_section_409(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-nofocus"
    write_meta(runs_dir, run_id, prompt="no markers here", status="finished",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines({"type": "system", "subtype": "init"})
    )
    ingester.scan()
    r = client.get(f"/v1/runs/{run_id}/artifacts/content",
                   params={"name": "prompt.focus"})
    assert r.status_code == 409
    assert r.json()["reason"]


def test_content_unavailable_edit_only_file_409(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-editonly"
    write_meta(runs_dir, run_id, prompt="p", status="finished",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init"},
            _assistant(_edit_block("/wt/notes.txt", "a", "b")),
        )
    )
    ingester.scan()
    body = client.get(f"/v1/runs/{run_id}/artifacts").json()
    entry = next(a for a in body["artifacts"] if a["name"] == "/wt/notes.txt")
    assert entry["available"] is False
    r = client.get(f"/v1/runs/{run_id}/artifacts/content",
                   params={"name": "/wt/notes.txt"})
    assert r.status_code == 409


def test_content_ambiguous_fragment_400(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-ambig"
    write_meta(runs_dir, run_id, prompt="p", status="finished",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init"},
            _assistant(_write_block("/a/REVIEW.md", "one", _id="w1")),
            _assistant(_write_block("/b/REVIEW.md", "two", _id="w2")),
        )
    )
    ingester.scan()
    r = client.get(f"/v1/runs/{run_id}/artifacts/content",
                   params={"name": "REVIEW.md"})
    assert r.status_code == 400
    assert set(r.json()["candidates"]) == {"/a/REVIEW.md", "/b/REVIEW.md"}


# -- summary decisions -------------------------------------------------------


def test_summary_includes_decisions(env):
    runs_dir, client, ingester = env
    run_id = _full_run(runs_dir, ingester)
    summary = client.get(f"/v1/runs/{run_id}/summary").json()
    assert summary["decisions"] == "decided this"
    assert summary["result_text"] == RESULT


def test_summary_decisions_null_when_absent(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-nodec"
    write_meta(runs_dir, run_id, prompt="p", status="finished",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success", "result": "just a plain result"},
        )
    )
    ingester.scan()
    summary = client.get(f"/v1/runs/{run_id}/summary").json()
    assert summary["decisions"] is None


# -- model harvest (§6) ------------------------------------------------------


def test_model_harvested_from_init_event(env):
    runs_dir, client, ingester = env
    run_id = _full_run(runs_dir, ingester)
    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["model"] == "claude-opus-4-8[1m]"


def test_model_null_without_init(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-noinit"
    # meta carries a requested model, but no init event ever observed one
    write_meta(runs_dir, run_id, prompt="p", status="running",
               model_requested="claude-opus-4-8", started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(_assistant({"type": "text", "text": "hi"}))
    )
    ingester.scan()
    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["model"] is None


def test_model_requested_never_used_as_fallback(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-reqonly"
    # requested model present in meta; init event has NO model -> stays null
    write_meta(runs_dir, run_id, prompt="p", status="finished",
               model_requested="claude-sonnet-5", started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init"},  # no model key
            {"type": "result", "subtype": "success", "result": "ok"},
        )
    )
    ingester.scan()
    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["model"] is None
    # requested intent stays retrievable via the meta passthrough
    assert detail["meta_json"]["model_requested"] == "claude-sonnet-5"


def test_meta_rescan_does_not_clobber_harvested_model(env):
    runs_dir, client, ingester = env
    run_id = _full_run(runs_dir, ingester)
    # a later meta rewrite (e.g. run finishing) must not reset the observed model
    write_meta(runs_dir, run_id, prompt=PROMPT, status="finished",
               started_at="2026-07-09T00:00:00+00:00", ended_at="2026-07-09T00:05:00+00:00")
    import time as _t
    _t.sleep(0.01)
    (runs_dir / f"{run_id}.meta.json").write_text(
        json.dumps({"run_id": run_id, "git": {"branch": "main"}, "prompt": PROMPT,
                    "status": "finished", "started_at": "2026-07-09T00:00:00+00:00",
                    "ended_at": "2026-07-09T00:05:00+00:00"})
    )
    ingester.scan()
    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["model"] == "claude-opus-4-8[1m]"
