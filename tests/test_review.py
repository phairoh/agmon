"""Adversarial tests from the stage-2 review (see REVIEW.md).

Each xfail encodes a demonstrated defect; it must report XFAIL (strict), not
PASS or ERROR. Payloads mirror real specimens in ~/agent-runs where possible.
"""

from __future__ import annotations

import pytest

from agmon import derive
from agmon.ingest import _is_error_event


# -- F1: a result event that self-reports is_error but keeps subtype "success"
# (the real 529-overload specimen 20260709T030855-b6c59d) is never flagged and
# produces no issue, so the run shows issue_count=0 / issues=[] despite a hard
# failure. Both ingest classification and derive_issues key only on `subtype`,
# ignoring the event's own `is_error: true`.


def test_ingest_flags_result_event_with_is_error_true():
    obj = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "API Error: 529 Overloaded.",
    }
    # Documented current behavior: not flagged (kept as a living record).
    assert _is_error_event(obj) is False


@pytest.mark.xfail(strict=True, reason="F1")
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
