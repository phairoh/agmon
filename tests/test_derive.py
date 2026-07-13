"""Tests for the pure derivation functions in agmon.derive.

Driven directly with plain dicts, injected ``now``, and stubbed ``pid_alive`` —
no database, no sleeping, no real pids.
"""

from __future__ import annotations

from agmon import derive

NOW = "2026-07-08T12:00:00+00:00"


def _assistant(seq, blocks):
    return {
        "seq": seq,
        "type": "assistant",
        "subtype": None,
        "payload": {"type": "assistant", "message": {"role": "assistant", "content": blocks}},
    }


def _user(seq, blocks):
    return {
        "seq": seq,
        "type": "user",
        "subtype": None,
        "payload": {"type": "user", "message": {"role": "user", "content": blocks}},
    }


def _result(seq, subtype, **extra):
    return {
        "seq": seq,
        "type": "result",
        "subtype": subtype,
        "payload": {"type": "result", "subtype": subtype, **extra},
    }


# -- status matrix -----------------------------------------------------------


def _status(status, **run):
    run["status"] = status
    return derive.derive_status(
        run,
        run.pop("_last", "2026-07-08T11:59:00+00:00"),
        run.pop("_alive", True),
        NOW,
        stall_seconds=300,
    )


def test_status_finished_passthrough():
    out = _status("finished", result_subtype="success", _alive=None)
    assert out["effective_status"] == "finished"
    assert out["stalled_seconds"] is None
    assert out["pid_alive"] is None


def test_status_error_vs_interrupted():
    # non-null subtype -> the task itself failed
    assert _status("error", result_subtype="error_x")["effective_status"] == "error"
    # null subtype -> stream ended with no result event -> retryable
    assert _status("error", result_subtype=None)["effective_status"] == "interrupted"


def test_status_died_when_pid_gone():
    out = _status("running", result_subtype=None, _alive=False)
    assert out["effective_status"] == "died"
    assert out["pid_alive"] is False


def test_status_stalled_when_quiet_past_threshold():
    # last event 10 minutes ago, threshold 300s -> stalled
    out = _status(
        "running", result_subtype=None, _alive=True, _last="2026-07-08T11:50:00+00:00"
    )
    assert out["effective_status"] == "stalled"
    assert out["stalled_seconds"] == 600


def test_status_running_when_recent():
    out = _status(
        "running", result_subtype=None, _alive=True, _last="2026-07-08T11:59:30+00:00"
    )
    assert out["effective_status"] == "running"
    assert out["stalled_seconds"] is None


# -- activity ----------------------------------------------------------------


def test_last_tool_prefers_file_path_over_command():
    events = [
        _assistant(1, [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]),
        _assistant(
            2,
            [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/y.py", "command": "ignored"}}],
        ),
    ]
    activity = derive.derive_activity(events)
    assert activity["last_tool"] == {"seq": 2, "tool": "Edit", "target": "/x/y.py"}


def test_last_tool_command_then_first_string():
    only_cmd = derive.derive_activity(
        [_assistant(1, [{"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}])]
    )
    assert only_cmd["last_tool"]["target"] == "echo hi"

    fallback = derive.derive_activity(
        [_assistant(1, [{"type": "tool_use", "name": "Weird", "input": {"n": 3, "q": "query-str"}}])]
    )
    assert fallback["last_tool"]["target"] == "query-str"


def test_last_tool_null_without_tool_calls():
    assert derive.derive_activity([_assistant(1, [{"type": "text", "text": "hi"}])])["last_tool"] is None


def test_progress_returns_latest_of_many():
    events = [
        _assistant(1, [{"type": "text", "text": "PROGRESS: step one\nnoise"}]),
        _assistant(2, [{"type": "text", "text": "some text"}]),
        _assistant(3, [{"type": "text", "text": "prefix\nPROGRESS: step two"}]),
    ]
    activity = derive.derive_activity(events)
    assert activity["progress"] == "step two"
    assert activity["last_text"] == "prefix\nPROGRESS: step two"


def test_progress_null_when_absent():
    assert derive.derive_activity([_assistant(1, [{"type": "text", "text": "hi"}])])["progress"] is None


# -- issues ------------------------------------------------------------------


def test_issue_tool_error_resolves_tool_name():
    events = [
        _assistant(1, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "boom"}}]),
        _user(2, [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "command failed: boom"}]),
    ]
    issues = derive.derive_issues(events)
    assert issues == [
        {"seq": 2, "category": "tool_error", "tool": "Bash", "snippet": "command failed: boom"}
    ]


def test_issue_permission_classification():
    events = [
        _assistant(1, [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "rm -rf /"}}]),
        _user(2, [{"type": "tool_result", "tool_use_id": "t9", "is_error": True, "content": "The user has not granted permission to run this tool."}]),
    ]
    issues = derive.derive_issues(events)
    assert issues[0]["category"] == "permission"
    assert issues[0]["tool"] == "Bash"


def test_issue_unresolvable_tool_is_null():
    events = [
        _user(1, [{"type": "tool_result", "tool_use_id": "missing", "is_error": True, "content": "err"}]),
    ]
    assert derive.derive_issues(events)[0]["tool"] is None


def test_issue_run_error_from_non_success_result():
    events = [_result(5, "error_max_turns", result="ran out of turns")]
    issues = derive.derive_issues(events)
    assert issues == [
        {"seq": 5, "category": "run_error", "tool": None, "snippet": "ran out of turns"}
    ]


def test_issues_capped_at_50_most_recent():
    events = [
        _user(i, [{"type": "tool_result", "tool_use_id": "x", "is_error": True, "content": f"e{i}"}])
        for i in range(1, 61)
    ]
    issues = derive.derive_issues(events)
    assert len(issues) == 50
    assert issues[0]["seq"] == 11  # oldest kept
    assert issues[-1]["seq"] == 60  # newest


# -- metrics -----------------------------------------------------------------


def test_metrics_counts_duration_and_usage():
    events = [
        _assistant(1, [{"type": "tool_use", "name": "Bash", "input": {"command": "a"}}]),
        _assistant(2, [{"type": "tool_use", "name": "Bash", "input": {"command": "b"}}]),
        _assistant(3, [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/z"}}]),
        _result(4, "success", usage={"input_tokens": 10, "output_tokens": 2}),
    ]
    run = {
        "started_at": "2026-07-08T00:00:00+00:00",
        "ended_at": "2026-07-08T00:05:00+00:00",
        "num_turns": 4,
        "total_cost_usd": 0.12,
    }
    m = derive.derive_metrics(run, events, NOW)
    assert m["num_events"] == 4
    assert m["tool_counts"] == {"Bash": 2, "Edit": 1}
    assert m["duration_seconds"] == 300
    assert m["num_turns"] == 4
    assert m["total_cost_usd"] == 0.12
    assert m["usage"] == {"input_tokens": 10, "output_tokens": 2}


def test_metrics_duration_to_now_when_running():
    run = {"started_at": "2026-07-08T11:00:00+00:00", "ended_at": None}
    m = derive.derive_metrics(run, [], NOW)
    assert m["duration_seconds"] == 3600
    assert m["usage"] is None


# -- section extraction (derive_section) --------------------------------------


def test_section_bare_marker_no_heading_no_colon():
    text = "intro text\nDECISIONS\nfirst point\nsecond point\n"
    assert derive.derive_section(text, "DECISIONS") == "first point\nsecond point"


def test_section_heading_prefix_variants():
    for prefix in ("#", "##", "###"):
        text = f"intro\n{prefix} DECISIONS\nbody here\n"
        assert derive.derive_section(text, "DECISIONS") == "body here"


def test_section_trailing_colon():
    text = "intro\nDECISIONS:\nbody here\n"
    assert derive.derive_section(text, "DECISIONS") == "body here"


def test_section_heading_and_colon_combined():
    text = "intro\n## DECISIONS:\nbody here\n"
    assert derive.derive_section(text, "DECISIONS") == "body here"


def test_section_last_occurrence_wins():
    text = "DECISIONS\nfirst attempt\nDECISIONS\nfinal answer\n"
    assert derive.derive_section(text, "DECISIONS") == "final answer"


def test_section_runs_to_next_marker():
    text = "FOCUS\nlook at X\nOVERRIDES\nskip Y\n"
    assert derive.derive_section(text, "FOCUS") == "look at X"
    assert derive.derive_section(text, "OVERRIDES") == "skip Y"


def test_section_runs_to_eof_when_no_next_marker():
    text = "intro\nDECISIONS\nline one\nline two"
    assert derive.derive_section(text, "DECISIONS") == "line one\nline two"


def test_section_marker_mid_prose_does_not_trigger():
    text = "This paragraph discusses DECISIONS made previously in detail.\nmore text\n"
    assert derive.derive_section(text, "DECISIONS") is None


def test_section_absent_marker_returns_none():
    assert derive.derive_section("no markers here at all\n", "DECISIONS") is None


def test_section_null_text_returns_none():
    assert derive.derive_section(None, "DECISIONS") is None


def test_section_empty_between_marker_and_next():
    text = "DECISIONS\nFOCUS\nbody\n"
    assert derive.derive_section(text, "DECISIONS") == ""
