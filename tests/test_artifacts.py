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

from agmon import artifacts


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
