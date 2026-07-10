"""Regression tests for stage-3 CLI review findings.

F1: `agmon tail --plain` must emit no ANSI (colour or bold) on a TTY.
F3: `to_tsv` must keep every row one physical line even when a cell contains a
tab/newline/CR.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from agmon import cli, render

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


class _TailStub:
    """Minimal tail client: one batch (a PROGRESS line + a success result)."""

    def resolve_run_id(self, fragment):
        return "run1"

    def get_events(self, run_id, *, after=0, limit=200, errors_only=False):
        if after == 0:
            return {
                "events": [
                    {
                        "seq": 1,
                        "type": "assistant",
                        "ingested_at": "2026-07-09T11:58:00+00:00",
                        "payload": {
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": "PROGRESS: working"}]},
                        },
                    },
                    {
                        "seq": 2,
                        "type": "result",
                        "ingested_at": "2026-07-09T11:59:00+00:00",
                        "payload": {"type": "result", "subtype": "success",
                                    "num_turns": 1, "total_cost_usd": 0.04},
                    },
                ],
                "next_after": 2,
            }
        return {"events": [], "next_after": after}

    def get_summary(self, run_id):
        return {"status": {"effective_status": "finished"},
                "metrics": {"total_cost_usd": 0.04, "duration_seconds": 120}}


def test_tail_plain_suppresses_color_on_tty():
    """`agmon tail --plain` (help: "no color") must emit no ANSI on a TTY.

    cmd_tail never reads args.plain, so colour is still written."""
    out, err = io.StringIO(), io.StringIO()
    cli.main(["tail", "run1", "--plain"], client=_TailStub(), out=out, err=err,
             tty=True, now=NOW, sleep=lambda *_: None)
    assert "\x1b[" not in out.getvalue()


def test_tsv_row_stays_one_line_with_newline_cell():
    """to_tsv promises "a header line then one tab-joined row each"; a cell with
    an embedded newline must not split one row into several output lines."""
    tsv = render.to_tsv(["a", "b"], [["x\nY", "z"]])
    assert len(tsv.splitlines()) == 2  # header + exactly one data row
