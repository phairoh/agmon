"""Stage-4b: the artifact catalog — sections, file reconstruction, name
resolution, the API, the CLI, and the model harvest.

Sections mirror the spec (specs/006-artifacts.md): file reconstruction is
driven purely with plain event dicts (Write/Edit tool_use payload shapes
lifted from real ~/agent-runs spool files — file_path/content for Write,
file_path/old_string/new_string/replace_all for Edit); catalog + resolution
are pure; API/CLI use the same direct-scan / stub-client fixtures as the
other integration suites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agmon import artifacts
from agmon.api import create_app
from agmon.config import Config


def _write(seq, file_path, content, tool_use_id="t"):
    return {
        "seq": seq,
        "type": "assistant",
        "subtype": None,
        "payload": {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"{tool_use_id}{seq}",
                        "name": "Write",
                        "input": {"file_path": file_path, "content": content},
                    }
                ],
            },
        },
    }


def _edit(seq, file_path, old_string, new_string, *, replace_all=False, tool_use_id="t"):
    return {
        "seq": seq,
        "type": "assistant",
        "subtype": None,
        "payload": {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"{tool_use_id}{seq}",
                        "name": "Edit",
                        "input": {
                            "file_path": file_path,
                            "old_string": old_string,
                            "new_string": new_string,
                            "replace_all": replace_all,
                        },
                    }
                ],
            },
        },
    }


def _bash(seq, command):
    return {
        "seq": seq,
        "type": "assistant",
        "subtype": None,
        "payload": {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"b{seq}",
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
    }


# ============================================================================
# 1. File reconstruction — derive_file_artifacts / reconstruct_file
# ============================================================================


def test_write_then_edits_yields_final_content():
    events = [
        _write(1, "/repo/a.py", "hello world\n"),
        _edit(2, "/repo/a.py", "hello", "goodbye"),
    ]
    assert artifacts.reconstruct_file(events, "/repo/a.py") == "goodbye world\n"


def test_interleaved_files_reconstruct_independently():
    events = [
        _write(1, "/repo/a.py", "AAA\n"),
        _write(2, "/repo/b.py", "BBB\n"),
        _edit(3, "/repo/a.py", "AAA", "aaa"),
        _edit(4, "/repo/b.py", "BBB", "bbb"),
    ]
    assert artifacts.reconstruct_file(events, "/repo/a.py") == "aaa\n"
    assert artifacts.reconstruct_file(events, "/repo/b.py") == "bbb\n"


def test_edit_applies_against_current_reconstructed_state():
    # Second edit's old_string only exists after the first edit has run.
    events = [
        _write(1, "/repo/a.py", "one\n"),
        _edit(2, "/repo/a.py", "one", "two"),
        _edit(3, "/repo/a.py", "two", "three"),
    ]
    assert artifacts.reconstruct_file(events, "/repo/a.py") == "three\n"


def test_replace_all_semantics():
    events = [
        _write(1, "/repo/a.py", "x x x\n"),
        _edit(2, "/repo/a.py", "x", "y", replace_all=True),
    ]
    assert artifacts.reconstruct_file(events, "/repo/a.py") == "y y y\n"


def test_replace_all_false_replaces_only_first():
    events = [
        _write(1, "/repo/a.py", "x x x\n"),
        _edit(2, "/repo/a.py", "x", "y", replace_all=False),
    ]
    assert artifacts.reconstruct_file(events, "/repo/a.py") == "y x x\n"


def test_multibyte_content_survives_byte_exact():
    content = "héllo wörld — 日本語\n"
    events = [_write(1, "/repo/a.py", content)]
    assert artifacts.reconstruct_file(events, "/repo/a.py") == content
    fa = artifacts.derive_file_artifacts(events)
    assert fa[0]["bytes"] == len(content.encode("utf-8"))


def test_review_md_write_then_deleted_reconstructs():
    events = [
        _write(1, "/worktree/REVIEW.md", "# Review\n\nAll good.\n"),
        _bash(2, "rm /worktree/REVIEW.md"),
    ]
    assert artifacts.reconstruct_file(events, "/worktree/REVIEW.md") == "# Review\n\nAll good.\n"
    fa = artifacts.derive_file_artifacts(events)
    assert len(fa) == 1
    assert fa[0]["reconstructable"] is True


def test_derive_file_artifacts_fields():
    events = [
        _write(1, "/repo/a.py", "one\n"),
        _edit(5, "/repo/a.py", "one", "two"),
    ]
    fa = artifacts.derive_file_artifacts(events)
    assert fa == [
        {
            "path": "/repo/a.py",
            "ops": 2,
            "first_op": "write",
            "last_seq": 5,
            "reconstructable": True,
            "bytes": len("two\n".encode("utf-8")),
        }
    ]


# -- not reconstructable ------------------------------------------------------


def test_edit_without_write_not_reconstructable():
    events = [_edit(1, "/repo/a.py", "old", "new")]
    fa = artifacts.derive_file_artifacts(events)
    assert fa[0]["first_op"] == "edit"
    assert fa[0]["reconstructable"] is False
    assert fa[0]["bytes"] is None


def test_reconstruct_non_reconstructable_raises():
    events = [_edit(1, "/repo/a.py", "old", "new")]
    import pytest

    with pytest.raises(artifacts.NotReconstructableError):
        artifacts.reconstruct_file(events, "/repo/a.py")


def test_reconstruct_unknown_path_raises():
    import pytest

    with pytest.raises(artifacts.ArtifactNotFound):
        artifacts.reconstruct_file([], "/nope.py")


# ============================================================================
# 2. Catalog + name resolution — build_catalog / resolve_artifact_content
# ============================================================================


def _result_event(seq, text):
    return {
        "seq": seq,
        "type": "result",
        "subtype": "success",
        "payload": {"type": "result", "subtype": "success", "result": text},
    }


FULL_PROMPT = "do the thing\n\nFOCUS\nfocus on X\n\nOVERRIDES\nignore Y\n"
FULL_RESULT = "the task is done\n\nDECISIONS\npicked approach A\n"


def _full_run_events():
    return [
        _write(1, "/worktree/REVIEW.md", "# Review\n\nlooks good\n"),
        _result_event(2, FULL_RESULT),
    ]


def test_catalog_full_run_lists_all_artifacts():
    run = {"prompt": FULL_PROMPT}
    events = _full_run_events()
    catalog = artifacts.build_catalog(run, events)
    by_name = {a["name"]: a for a in catalog}

    assert set(by_name) == {
        "prompt", "prompt.focus", "prompt.overrides",
        "result", "result.decisions", "/worktree/REVIEW.md",
    }
    assert by_name["prompt"]["kind"] == "dispatch"
    assert by_name["prompt"]["available"] is True
    assert by_name["prompt.focus"]["kind"] == "section"
    assert by_name["prompt.focus"]["available"] is True
    assert by_name["prompt.overrides"]["available"] is True
    assert by_name["result"]["available"] is True
    assert by_name["result.decisions"]["available"] is True
    review = by_name["/worktree/REVIEW.md"]
    assert review["kind"] == "file"
    assert review["available"] is True
    assert review["reconstructable"] is True
    assert review["bytes"] == len("# Review\n\nlooks good\n".encode("utf-8"))


def test_catalog_bare_run_dispatch_only_unavailable_with_reasons():
    run = {"prompt": None}
    events = []
    catalog = artifacts.build_catalog(run, events)
    assert [a["name"] for a in catalog] == [
        "prompt", "prompt.focus", "prompt.overrides", "result", "result.decisions",
    ]
    for item in catalog:
        assert item["available"] is False
        assert item["reason"]
        assert "bytes" not in item


def test_catalog_dispatch_order_stable():
    run = {"prompt": FULL_PROMPT}
    catalog = artifacts.build_catalog(run, _full_run_events())
    dispatch_names = [a["name"] for a in catalog if a["kind"] in ("dispatch", "section")]
    assert dispatch_names == [
        "prompt", "prompt.focus", "prompt.overrides", "result", "result.decisions",
    ]


def test_catalog_marker_absent_is_unavailable_with_reason():
    run = {"prompt": "no sections here\n"}
    catalog = artifacts.build_catalog(run, [])
    by_name = {a["name"]: a for a in catalog}
    assert by_name["prompt.focus"]["available"] is False
    assert "FOCUS" in by_name["prompt.focus"]["reason"]


def test_catalog_edit_only_file_listed_unavailable():
    run = {"prompt": None}
    events = [_edit(1, "/repo/a.py", "x", "y")]
    catalog = artifacts.build_catalog(run, events)
    by_name = {a["name"]: a for a in catalog}
    file_item = by_name["/repo/a.py"]
    assert file_item["kind"] == "file"
    assert file_item["available"] is False
    assert file_item["reason"]
    assert "bytes" not in file_item


# -- resolve_artifact_content -------------------------------------------------


def test_resolve_dispatch_exact_name():
    run = {"prompt": FULL_PROMPT}
    events = _full_run_events()
    assert artifacts.resolve_artifact_content(run, events, "prompt") == FULL_PROMPT
    assert artifacts.resolve_artifact_content(run, events, "prompt.focus") == "focus on X"
    assert artifacts.resolve_artifact_content(run, events, "result.decisions") == "picked approach A"


def test_resolve_exact_file_path():
    run = {"prompt": None}
    events = _full_run_events()
    content = artifacts.resolve_artifact_content(run, events, "/worktree/REVIEW.md")
    assert content == "# Review\n\nlooks good\n"


def test_resolve_unique_basename():
    run = {"prompt": None}
    events = _full_run_events()
    content = artifacts.resolve_artifact_content(run, events, "REVIEW.md")
    assert content == "# Review\n\nlooks good\n"


def test_resolve_unique_substring():
    run = {"prompt": None}
    events = _full_run_events()
    content = artifacts.resolve_artifact_content(run, events, "EVIEW")
    assert content == "# Review\n\nlooks good\n"


def test_resolve_ambiguous_fragment_lists_candidates():
    run = {"prompt": None}
    events = [
        _write(1, "/a/REVIEW.md", "one"),
        _write(2, "/b/REVIEW.md", "two"),
    ]
    import pytest

    with pytest.raises(artifacts.AmbiguousArtifactName) as exc:
        artifacts.resolve_artifact_content(run, events, "REVIEW.md")
    assert set(exc.value.candidates) == {"/a/REVIEW.md", "/b/REVIEW.md"}


def test_resolve_unknown_name_raises_not_found():
    run = {"prompt": None}
    import pytest

    with pytest.raises(artifacts.ArtifactNotFound):
        artifacts.resolve_artifact_content(run, [], "nope.md")


def test_resolve_unavailable_section_raises_with_reason():
    run = {"prompt": "no sections\n"}
    import pytest

    with pytest.raises(artifacts.ArtifactUnavailable) as exc:
        artifacts.resolve_artifact_content(run, [], "prompt.focus")
    assert exc.value.reason


def test_resolve_unavailable_result_when_absent():
    run = {"prompt": None}
    import pytest

    with pytest.raises(artifacts.ArtifactUnavailable):
        artifacts.resolve_artifact_content(run, [], "result")


def test_resolve_non_reconstructable_file_raises_unavailable():
    run = {"prompt": None}
    events = [_edit(1, "/repo/a.py", "x", "y")]
    import pytest

    with pytest.raises(artifacts.ArtifactUnavailable):
        artifacts.resolve_artifact_content(run, events, "/repo/a.py")


def test_resolve_ambiguous_basename_beats_substring_tier():
    # An exact-basename match should resolve even though a longer path also
    # contains the fragment as a substring — the basename tier wins outright.
    run = {"prompt": None}
    events = [
        _write(1, "/a/REVIEW.md", "short"),
        _write(2, "/b/OLD_REVIEW.md", "long"),
    ]
    content = artifacts.resolve_artifact_content(run, events, "REVIEW.md")
    assert content == "short"


# ============================================================================
# 3. API — /v1/runs/{id}/artifacts, /artifacts/content, summary.decisions
# ============================================================================


@pytest.fixture()
def env(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    config = Config(
        runs_dir=runs_dir, db_path=tmp_path / "agmon.db",
        host="127.0.0.1", port=8400, stall_seconds=300,
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


def _write_event(seq, file_path, content):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"t{seq}", "name": "Write",
                 "input": {"file_path": file_path, "content": content}},
            ],
        },
    }


def _full_run(runs_dir: Path, ingester, run_id: str) -> None:
    write_meta(
        runs_dir, run_id, status="finished", prompt=FULL_PROMPT,
        started_at="2026-07-09T00:00:00+00:00",
    )
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init"},
            _write_event(1, "/worktree/REVIEW.md", "# Review\n\nlooks good\n"),
            {"type": "result", "subtype": "success", "result": FULL_RESULT},
        )
    )
    ingester.scan()


def test_api_artifacts_catalog(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-aaaaaa"
    _full_run(runs_dir, ingester, run_id)

    resp = client.get(f"/v1/runs/{run_id}/artifacts")
    assert resp.status_code == 200
    by_name = {a["name"]: a for a in resp.json()["artifacts"]}
    assert by_name["prompt"]["available"] is True
    assert by_name["prompt.focus"]["available"] is True
    assert by_name["result.decisions"]["available"] is True
    assert by_name["/worktree/REVIEW.md"]["kind"] == "file"
    assert by_name["/worktree/REVIEW.md"]["available"] is True


def test_api_artifacts_unknown_run_404(env):
    _, client, _ = env
    assert client.get("/v1/runs/nope/artifacts").status_code == 404
    assert client.get("/v1/runs/nope/artifacts/content?name=prompt").status_code == 404


def test_api_artifact_content_dispatch(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-bbbbbb"
    _full_run(runs_dir, ingester, run_id)

    resp = client.get(f"/v1/runs/{run_id}/artifacts/content", params={"name": "prompt.focus"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "focus on X"


def test_api_artifact_content_file_by_basename(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-cccccc"
    _full_run(runs_dir, ingester, run_id)

    resp = client.get(f"/v1/runs/{run_id}/artifacts/content", params={"name": "REVIEW.md"})
    assert resp.status_code == 200
    assert resp.text == "# Review\n\nlooks good\n"


def test_api_artifact_content_unknown_name_404(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-dddddd"
    _full_run(runs_dir, ingester, run_id)

    resp = client.get(f"/v1/runs/{run_id}/artifacts/content", params={"name": "nope.md"})
    assert resp.status_code == 404
    assert resp.json()["error"]


def test_api_artifact_content_unavailable_409(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-eeeeee"
    write_meta(runs_dir, run_id, status="finished", prompt="no sections here",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines({"type": "system", "subtype": "init"})
    )
    ingester.scan()

    resp = client.get(f"/v1/runs/{run_id}/artifacts/content", params={"name": "prompt.focus"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]
    assert body["reason"]


def test_api_artifact_content_ambiguous_400(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-ffffff"
    write_meta(runs_dir, run_id, status="finished", prompt=None,
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines(
            {"type": "system", "subtype": "init"},
            _write_event(1, "/a/REVIEW.md", "one"),
            _write_event(2, "/b/REVIEW.md", "two"),
        )
    )
    ingester.scan()

    resp = client.get(f"/v1/runs/{run_id}/artifacts/content", params={"name": "REVIEW.md"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]
    assert set(body["candidates"]) == {"/a/REVIEW.md", "/b/REVIEW.md"}


def test_summary_includes_decisions(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-999999"
    _full_run(runs_dir, ingester, run_id)

    resp = client.get(f"/v1/runs/{run_id}/summary")
    assert resp.status_code == 200
    assert resp.json()["decisions"] == "picked approach A"


def test_summary_decisions_null_when_absent(env):
    runs_dir, client, ingester = env
    run_id = "20260709T000000-888888"
    write_meta(runs_dir, run_id, status="running", prompt="hi",
               started_at="2026-07-09T00:00:00+00:00")
    (runs_dir / f"{run_id}.jsonl").write_text(
        jsonl_lines({"type": "system", "subtype": "init"})
    )
    ingester.scan()

    resp = client.get(f"/v1/runs/{run_id}/summary")
    assert resp.json()["decisions"] is None
