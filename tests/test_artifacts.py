"""Tests for the pure artifact layer (agmon.artifacts).

Section extraction, file reconstruction, catalog assembly, and name resolution
are all pure functions driven with plain event dicts — no db, no fastapi, no os.
Tool-call fixtures mirror the real spool payload shapes discovered in
``~/agent-runs`` (Write: file_path+content; Edit: replace_all+file_path+
old_string+new_string).
"""

from __future__ import annotations

import pytest

from agmon import artifacts


# -- event fixtures (real payload shapes) ------------------------------------


def _assistant(seq, *blocks):
    return {
        "seq": seq,
        "type": "assistant",
        "subtype": None,
        "payload": {
            "type": "assistant",
            "message": {"role": "assistant", "content": list(blocks)},
        },
    }


def _write(path, content, _id="w"):
    return {
        "type": "tool_use",
        "id": _id,
        "name": "Write",
        "input": {"file_path": path, "content": content},
    }


def _edit(path, old, new, replace_all=False, _id="e"):
    return {
        "type": "tool_use",
        "id": _id,
        "name": "Edit",
        "input": {
            "replace_all": replace_all,
            "file_path": path,
            "old_string": old,
            "new_string": new,
        },
    }


def _bash(command, _id="b"):
    return {
        "type": "tool_use",
        "id": _id,
        "name": "Bash",
        "input": {"command": command},
    }


def _tool_error(seq, tool_use_id, text="permission denied"):
    """A user event carrying an errored tool_result (real spool shape: a
    ``user`` message whose content is a ``tool_result`` block with
    ``is_error: True`` referencing an earlier tool_use id)."""
    return {
        "seq": seq,
        "type": "user",
        "subtype": None,
        "payload": {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "is_error": True,
                        "content": text,
                    }
                ],
            },
        },
    }


# -- F1: rejected (errored) file ops must not be surfaced --------------------


@pytest.mark.xfail(strict=True, reason="F1")
def test_rejected_write_not_surfaced_as_content():
    # A permission-denied / rejected Write never touched disk — the run's
    # tool_result flags is_error True (observed in ~/agent-runs, e.g.
    # 20260708T220925-daadb1 writing /tmp/check_json.py). Its content must not
    # be surfaced as the file's reconstructed content.
    events = [
        _assistant(1, _write("/tmp/check.py", "PHANTOM CONTENT\n", _id="w1")),
        _tool_error(2, "w1"),
    ]
    try:
        content = artifacts.reconstruct_file(events, "/tmp/check.py")
    except (artifacts.ArtifactUnknown, artifacts.ArtifactUnavailable):
        return  # correct: the write was rejected, nothing to reconstruct
    assert "PHANTOM" not in content


# -- section parser ----------------------------------------------------------


def test_section_bare_marker():
    text = "intro line\nDECISIONS\nbody line\n"
    assert artifacts.derive_section(text, "DECISIONS") == "body line"


def test_section_heading_prefix_and_colon():
    text = "pre\n## DECISIONS:\nb1\nb2\n"
    assert artifacts.derive_section(text, "DECISIONS") == "b1\nb2"


def test_section_three_hashes():
    text = "pre\n### FOCUS\nf body\n"
    assert artifacts.derive_section(text, "FOCUS") == "f body"


def test_section_last_occurrence_wins():
    text = "DECISIONS\nfirst\n\nDECISIONS\nsecond\n"
    assert artifacts.derive_section(text, "DECISIONS") == "second"


def test_section_runs_to_next_marker():
    text = "## FOCUS\nfocus body\n## OVERRIDES\noverride body\n"
    assert artifacts.derive_section(text, "FOCUS") == "focus body"
    assert artifacts.derive_section(text, "OVERRIDES") == "override body"


def test_section_runs_to_eof():
    text = "chatter\n## DECISIONS\nline one\nline two\nline three"
    assert artifacts.derive_section(text, "DECISIONS") == "line one\nline two\nline three"


def test_section_word_mid_prose_does_not_trigger():
    text = (
        "Consider the FOCUS of your work.\n"
        "More prose about DECISIONS here, inline.\n"
    )
    assert artifacts.derive_section(text, "FOCUS") is None
    assert artifacts.derive_section(text, "DECISIONS") is None


def test_section_null_text_is_null():
    assert artifacts.derive_section(None, "FOCUS") is None


def test_section_absent_marker_is_null():
    assert artifacts.derive_section("no markers at all here", "FOCUS") is None


def test_section_empty_body_is_available_empty_string():
    # marker present but nothing follows -> present (empty), not absent (None)
    text = "prose\n## FOCUS\n"
    assert artifacts.derive_section(text, "FOCUS") == ""


# -- file reconstruction -----------------------------------------------------


def test_reconstruct_write_then_edits():
    events = [
        _assistant(1, _write("/w/a.txt", "hello world\n")),
        _assistant(2, _edit("/w/a.txt", "hello", "goodbye")),
        _assistant(3, _edit("/w/a.txt", "world", "there")),
    ]
    assert artifacts.reconstruct_file(events, "/w/a.txt") == "goodbye there\n"


def test_reconstruct_edit_applies_to_current_state_not_original():
    # The third edit only succeeds if the second edit's output is the input.
    events = [
        _assistant(1, _write("/f", "aaa")),
        _assistant(2, _edit("/f", "aaa", "bbb")),
        _assistant(3, _edit("/f", "bbb", "ccc")),
    ]
    assert artifacts.reconstruct_file(events, "/f") == "ccc"


def test_reconstruct_replace_all_vs_single():
    all_events = [
        _assistant(1, _write("/f", "x x x")),
        _assistant(2, _edit("/f", "x", "y", replace_all=True)),
    ]
    assert artifacts.reconstruct_file(all_events, "/f") == "y y y"

    single_events = [
        _assistant(1, _write("/f", "x x x")),
        _assistant(2, _edit("/f", "x", "y")),
    ]
    assert artifacts.reconstruct_file(single_events, "/f") == "y x x"


def test_reconstruct_interleaved_files_are_independent():
    events = [
        _assistant(1, _write("/a", "A1")),
        _assistant(2, _write("/b", "B1")),
        _assistant(3, _edit("/a", "A1", "A2")),
        _assistant(4, _edit("/b", "B1", "B2")),
    ]
    assert artifacts.reconstruct_file(events, "/a") == "A2"
    assert artifacts.reconstruct_file(events, "/b") == "B2"


def test_reconstruct_multibyte_byte_exact():
    content = "café — naïve — 日本語\n"
    events = [
        _assistant(1, _write("/f", content)),
        _assistant(2, _edit("/f", "café", "tea")),
    ]
    result = artifacts.reconstruct_file(events, "/f")
    assert result == "tea — naïve — 日本語\n"
    # byte-exact survival of multibyte content
    assert result.encode("utf-8") == "tea — naïve — 日本語\n".encode("utf-8")


def test_reconstruct_review_written_then_deleted():
    review = "# Review\n\n## F1 — a real bug\n\nThe thing is broken.\n"
    events = [
        _assistant(1, _write("/wt/REVIEW.md", review)),
        _assistant(2, _bash("rm REVIEW.md")),
    ]
    # deletion on disk does not erase the spool's record of the write
    assert artifacts.reconstruct_file(events, "/wt/REVIEW.md") == review


def test_reconstruct_unknown_path_raises():
    events = [_assistant(1, _write("/f", "x"))]
    with pytest.raises(artifacts.ArtifactUnknown):
        artifacts.reconstruct_file(events, "/nope")


def test_reconstruct_edit_only_raises_not_reconstructable():
    events = [_assistant(1, _edit("/f", "a", "b"))]
    with pytest.raises(artifacts.ArtifactUnavailable):
        artifacts.reconstruct_file(events, "/f")


# -- file artifact catalog rows ----------------------------------------------


def test_file_artifacts_write_then_edits_fields():
    final = "goodbye there\n"
    events = [
        _assistant(1, _write("/w/a.txt", "hello world\n")),
        _assistant(2, _edit("/w/a.txt", "hello", "goodbye")),
        _assistant(3, _edit("/w/a.txt", "world", "there")),
    ]
    (entry,) = artifacts.derive_file_artifacts(events)
    assert entry["path"] == "/w/a.txt"
    assert entry["ops"] == 3
    assert entry["first_op"] == "write"
    assert entry["last_seq"] == 3
    assert entry["reconstructable"] is True
    assert entry["bytes"] == len(final.encode("utf-8"))


def test_file_artifacts_edit_only_listed_but_marked():
    events = [_assistant(1, _edit("/f", "a", "b"))]
    (entry,) = artifacts.derive_file_artifacts(events)
    assert entry["path"] == "/f"
    assert entry["first_op"] == "edit"
    assert entry["reconstructable"] is False
    assert entry["bytes"] is None


# -- catalog assembly --------------------------------------------------------

PROMPT = "do the thing\n\n## FOCUS\nfocus here\n\n## OVERRIDES\noverride here\n"
RESULT = "did it\n\n## DECISIONS\ndecided X over Y\n"


def _by_name(catalog):
    return {a["name"]: a for a in catalog}


def test_catalog_full_run_lists_every_family():
    events = [_assistant(1, _write("/wt/REVIEW.md", "review body"))]
    cat = artifacts.build_catalog(PROMPT, RESULT, events)
    n = _by_name(cat)
    assert n["prompt"]["kind"] == "dispatch" and n["prompt"]["available"] is True
    assert n["prompt.focus"]["kind"] == "section" and n["prompt.focus"]["available"]
    assert n["prompt.overrides"]["kind"] == "section" and n["prompt.overrides"]["available"]
    assert n["result"]["kind"] == "dispatch" and n["result"]["available"] is True
    assert n["result.decisions"]["kind"] == "section" and n["result.decisions"]["available"]
    rev = n["/wt/REVIEW.md"]
    assert rev["kind"] == "file" and rev["available"] is True
    assert rev["reconstructable"] is True


def test_catalog_bare_run_lists_dispatch_with_reasons():
    cat = artifacts.build_catalog("just a prompt", None, [])
    n = _by_name(cat)
    assert n["prompt"]["available"] is True
    assert n["prompt.focus"]["available"] is False and n["prompt.focus"]["reason"]
    assert n["prompt.overrides"]["available"] is False and n["prompt.overrides"]["reason"]
    assert n["result"]["available"] is False and n["result"]["reason"]
    assert n["result.decisions"]["available"] is False and n["result.decisions"]["reason"]
    assert not any(a["kind"] == "file" for a in cat)


def test_catalog_dispatch_and_sections_always_present():
    # even a fully bare run lists all five named dispatch/section artifacts
    cat = artifacts.build_catalog(None, None, [])
    names = {a["name"] for a in cat}
    assert names == {"prompt", "prompt.focus", "prompt.overrides", "result", "result.decisions"}


# -- name resolution ---------------------------------------------------------


def test_resolve_dispatch_name():
    assert artifacts.resolve_content(PROMPT, RESULT, [], "prompt") == PROMPT
    assert artifacts.resolve_content(PROMPT, RESULT, [], "result.decisions") == "decided X over Y"


def test_resolve_exact_path():
    events = [_assistant(1, _write("/wt/REVIEW.md", "review body"))]
    assert artifacts.resolve_content(PROMPT, RESULT, events, "/wt/REVIEW.md") == "review body"


def test_resolve_unique_basename_and_substring():
    events = [_assistant(1, _write("/deep/worktree/REVIEW.md", "review body"))]
    assert artifacts.resolve_content(PROMPT, RESULT, events, "REVIEW.md") == "review body"
    assert artifacts.resolve_content(PROMPT, RESULT, events, "REVIEW") == "review body"


def test_resolve_ambiguous_fragment():
    events = [
        _assistant(1, _write("/a/REVIEW.md", "one")),
        _assistant(2, _write("/b/REVIEW.md", "two")),
    ]
    with pytest.raises(artifacts.ArtifactAmbiguous) as exc:
        artifacts.resolve_content(PROMPT, RESULT, events, "REVIEW.md")
    assert set(exc.value.candidates) == {"/a/REVIEW.md", "/b/REVIEW.md"}


def test_resolve_unknown_raises():
    with pytest.raises(artifacts.ArtifactUnknown):
        artifacts.resolve_content(PROMPT, RESULT, [], "does-not-exist")


def test_resolve_unavailable_section_raises_409_style():
    with pytest.raises(artifacts.ArtifactUnavailable):
        artifacts.resolve_content("bare prompt", None, [], "prompt.focus")


def test_resolve_unavailable_file_raises():
    events = [_assistant(1, _edit("/f", "a", "b"))]
    with pytest.raises(artifacts.ArtifactUnavailable):
        artifacts.resolve_content(PROMPT, RESULT, events, "/f")
