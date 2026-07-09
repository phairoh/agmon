"""Regression tests from the stage-2 review (F1, F4).

Payloads mirror real specimens in ~/agent-runs where possible.
"""

from __future__ import annotations

from agmon import derive
from agmon.ingest import _is_error_event


# -- F1: a result event that self-reports is_error but keeps subtype "success"
# (the real 529-overload specimen 20260709T030855-b6c59d) was never flagged and
# produced no issue, so the run showed issue_count=0 / issues=[] despite a hard
# failure. Both ingest classification and derive_issues now treat a result
# event's own `is_error: true` as an error regardless of subtype.


def test_ingest_flags_result_event_with_is_error_true():
    obj = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "API Error: 529 Overloaded.",
    }
    assert _is_error_event(obj) is True


def test_result_is_error_true_surfaces_as_issue():
    ev = {
        "seq": 42,
        "type": "result",
        "subtype": "success",
        "payload": {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "API Error: 529 Overloaded. This is a server-side issue.",
        },
    }
    issues = derive.derive_issues([ev])
    assert issues, "errored result event (is_error=true) should surface as an issue"
    assert issues[0]["category"] == "run_error"
    assert "529" in issues[0]["snippet"]


# -- F4: `progress` (self-reported PROGRESS: line) must come from assistant text
# only. A PROGRESS: marker echoed back inside a user/tool_result message must not
# be surfaced as the run's own progress.


def test_progress_ignores_non_assistant_events():
    ev = {
        "seq": 1,
        "type": "user",
        "subtype": None,
        "payload": {"type": "user", "message": {"content": "PROGRESS: from a tool echo"}},
    }
    assert derive.derive_activity([ev])["progress"] is None


def test_progress_reads_assistant_text():
    ev = {
        "seq": 1,
        "type": "assistant",
        "subtype": None,
        "payload": {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "PROGRESS: building index"}]},
        },
    }
    assert derive.derive_activity([ev])["progress"] == "building index"
