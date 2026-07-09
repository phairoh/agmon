"""Client-side policy: run-id resolution and resume lineage (both pure)."""

from __future__ import annotations

import pytest

from agmon.client import (
    AmbiguousRunId,
    RunNotFound,
    compute_lineage,
    resolve,
)

# Newest-first, as the API returns. Two runs share the 20260709 date prefix;
# the hex suffixes are the distinguishing part.
RUNS = [
    {"run_id": "20260709T120000-a3f9c1", "session_id": "s2", "started_at": "2026-07-09T12:00:00+00:00"},
    {"run_id": "20260709T090000-bb0e22", "session_id": "s1", "started_at": "2026-07-09T09:00:00+00:00"},
    {"run_id": "20260708T090000-a3f900", "session_id": "s1", "started_at": "2026-07-08T09:00:00+00:00"},
]


def test_omitted_resolves_to_latest():
    assert resolve(RUNS, None) == "20260709T120000-a3f9c1"


def test_unique_hex_substring_hit():
    # 'a3f9c1' is a unique hex-suffix fragment.
    assert resolve(RUNS, "a3f9c1") == "20260709T120000-a3f9c1"
    # 'bb0e22' likewise.
    assert resolve(RUNS, "bb0e22") == "20260709T090000-bb0e22"


def test_exact_full_id_wins():
    assert resolve(RUNS, "20260708T090000-a3f900") == "20260708T090000-a3f900"


def test_ambiguous_fragment_lists_candidates():
    # 'a3f9' is shared by two ids.
    with pytest.raises(AmbiguousRunId) as exc:
        resolve(RUNS, "a3f9")
    assert set(exc.value.candidates) == {
        "20260709T120000-a3f9c1",
        "20260708T090000-a3f900",
    }
    # The candidate ids appear in the message.
    assert "a3f9c1" in str(exc.value)
    assert "a3f900" in str(exc.value)


def test_date_prefix_ambiguous():
    with pytest.raises(AmbiguousRunId):
        resolve(RUNS, "20260709")


def test_no_match_errors():
    with pytest.raises(RunNotFound):
        resolve(RUNS, "deadbeef")


def test_no_runs_omitted_errors():
    with pytest.raises(RunNotFound):
        resolve([], None)


# -- lineage -----------------------------------------------------------------


def test_lineage_middle_of_chain():
    # s1 chain: a3f900 (older) -> bb0e22 (newer).
    lin = compute_lineage(RUNS, "20260709T090000-bb0e22")
    assert lin["resumed_from"] == "20260708T090000-a3f900"
    assert lin["resumed_by"] == []


def test_lineage_head_of_chain_resumed_by():
    lin = compute_lineage(RUNS, "20260708T090000-a3f900")
    assert lin["resumed_from"] is None
    assert lin["resumed_by"] == ["20260709T090000-bb0e22"]


def test_lineage_solo_session():
    lin = compute_lineage(RUNS, "20260709T120000-a3f9c1")
    assert lin == {"resumed_from": None, "resumed_by": []}


def test_lineage_multiple_children():
    runs = [
        {"run_id": "c", "session_id": "x", "started_at": "2026-07-09T03:00:00+00:00"},
        {"run_id": "b", "session_id": "x", "started_at": "2026-07-09T02:00:00+00:00"},
        {"run_id": "a", "session_id": "x", "started_at": "2026-07-09T01:00:00+00:00"},
    ]
    lin = compute_lineage(runs, "a")
    assert lin["resumed_from"] is None
    assert lin["resumed_by"] == ["b", "c"]


def test_lineage_null_session():
    runs = [{"run_id": "solo", "session_id": None, "started_at": "2026-07-09T01:00:00+00:00"}]
    assert compute_lineage(runs, "solo") == {"resumed_from": None, "resumed_by": []}
