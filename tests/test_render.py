"""render.py: event compaction, field flattening, and scalar formatting.

Event shapes are lifted from real spool lines (see ~/agent-runs/*.jsonl).
"""

from __future__ import annotations

from datetime import datetime, timezone

from agmon import render

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


# -- event compaction --------------------------------------------------------


def test_summarize_tool_use():
    event = {
        "seq": 3,
        "type": "assistant",
        "payload": {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "Bash",
                        "input": {"command": "ls -la && echo hi", "description": "x"},
                    }
                ]
            },
        },
    }
    s = render.summarize_event(event)
    assert s.text.startswith("→ Bash ls -la")
    assert s.style == "dim"


def test_summarize_multiple_tool_uses_notes_extra():
    event = {
        "type": "assistant",
        "payload": {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x"}},
                    {"type": "tool_use", "id": "b", "name": "Edit", "input": {"file_path": "/y"}},
                ]
            },
        },
    }
    s = render.summarize_event(event)
    assert s.text.startswith("→ Read /x")
    assert "(+1 more)" in s.text


def test_summarize_progress_highlighted():
    event = {
        "type": "assistant",
        "payload": {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "PROGRESS: reading the code"}]},
        },
    }
    s = render.summarize_event(event)
    assert s.text == "PROGRESS: reading the code"
    assert "cyan" in s.style


def test_summarize_errored_tool_result_is_red():
    event = {
        "type": "user",
        "payload": {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": True,
                        "content": "Permission denied: cannot write",
                    }
                ]
            },
        },
    }
    s = render.summarize_event(event)
    assert s.style == "red"
    assert "Permission denied" in s.text


def test_summarize_result_success_green():
    event = {
        "type": "result",
        "subtype": "success",
        "payload": {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "pong",
            "num_turns": 1,
            "total_cost_usd": 0.0423215,
        },
    }
    s = render.summarize_event(event)
    assert s.style == "green"
    assert "result: success" in s.text
    assert "$0.04" in s.text
    assert "1 turns" in s.text


def test_summarize_result_error_red():
    event = {
        "type": "result",
        "subtype": "error_during_execution",
        "payload": {"type": "result", "subtype": "error_during_execution", "is_error": True},
    }
    s = render.summarize_event(event)
    assert s.style == "red"
    assert "error_during_execution" in s.text


def test_summarize_system_init():
    event = {"type": "system", "subtype": "init", "payload": {"type": "system", "subtype": "init"}}
    s = render.summarize_event(event)
    assert s.text == "system: init"


def test_summarize_unparseable():
    event = {"type": "_unparseable", "payload": "{not json"}
    s = render.summarize_event(event)
    assert s.style == "red"


# -- scalar formatting -------------------------------------------------------


def test_relative_time():
    assert render.relative_time("2026-07-09T11:57:00+00:00", NOW) == "3m ago"
    assert render.relative_time("2026-07-09T09:00:00+00:00", NOW) == "3h ago"
    assert render.relative_time(None, NOW) == "-"


def test_format_duration():
    assert render.format_duration(760) == "12m40s"
    assert render.format_duration(45) == "45s"
    assert render.format_duration(None) == "-"


def test_short_id_and_project():
    assert render.short_id("20260709T120000-a3f9c1") == "a3f9c1"
    assert render.project_basename("/home/aaron/src/agmon") == "agmon"


# -- fields flattening -------------------------------------------------------


def test_flatten_one_level():
    obj = {"run_id": "x", "status": {"effective_status": "running", "pid_alive": True},
           "metrics": {"total_cost_usd": 0.04}}
    flat = render.flatten_one(obj)
    assert flat["run_id"] == "x"
    assert flat["status.effective_status"] == "running"
    assert flat["metrics.total_cost_usd"] == 0.04
    # only one level deep — nested-nested stays as a value under its dotted key
    obj2 = {"a": {"b": {"c": 1}}}
    assert render.flatten_one(obj2) == {"a.b": {"c": 1}}


def test_field_names_union_order():
    objs = [{"a": 1, "b": {"x": 2}}, {"a": 1, "c": 3}]
    assert render.field_names(objs) == ["a", "b.x", "c"]


def test_project_rows_raw_values():
    objs = [{"run_id": "r1", "status": {"effective_status": "running"}, "started_at": "2026-07-09T09:00:00+00:00"}]
    headers, rows = render.project_rows(objs, ["run_id", "status.effective_status", "started_at"])
    assert headers == ["run_id", "status.effective_status", "started_at"]
    # raw ISO preserved (not relativized) under --fields
    assert rows[0] == ["r1", "running", "2026-07-09T09:00:00+00:00"]


# -- tsv vs table ------------------------------------------------------------


def test_to_tsv_is_plain_tab_joined():
    headers, rows = render.ls_rows(
        [{"run_id": "20260709T120000-a3f9c1", "effective_status": "running",
          "started_at": "2026-07-09T11:57:00+00:00", "cwd": "/home/aaron/src/agmon",
          "last_event_type": "assistant", "issue_count": 0, "total_cost_usd": 0.04}],
        NOW,
    )
    tsv = render.to_tsv(headers, rows)
    line0, line1 = tsv.splitlines()
    assert line0.split("\t") == render.LS_HEADERS
    cells = line1.split("\t")
    assert cells[0] == "a3f9c1"
    assert cells[1] == "running"  # style stripped in TSV
    # no rich box-drawing / decoration
    assert "│" not in tsv and "┃" not in tsv


# -- thinking compaction (spec: summarized thinking display) ------------------


def test_summarize_thinking_block_shows_snippet():
    event = {
        "seq": 2,
        "type": "assistant",
        "payload": {
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking",
                 "thinking": "I'm working through the well puzzle.\nNet gain per cycle is 1m."}
            ]},
        },
    }
    s = render.summarize_event(event)
    assert s.text == "thinking: I'm working through the well puzzle. Net gain per cycle is 1m."
    assert s.style == "dim italic"


def test_summarize_empty_thinking_stays_bare_assistant():
    # Spools recorded before --thinking-display summarized: bodies are empty.
    event = {
        "seq": 2,
        "type": "assistant",
        "payload": {
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": ""}]},
        },
    }
    s = render.summarize_event(event)
    assert s.text == "assistant"


def test_summarize_text_beats_thinking():
    event = {
        "seq": 3,
        "type": "assistant",
        "payload": {
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking", "thinking": "hidden reasoning"},
                {"type": "text", "text": "the answer"},
            ]},
        },
    }
    s = render.summarize_event(event)
    assert s.text == "the answer"
