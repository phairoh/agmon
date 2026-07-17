"""CLI wiring: output layering (TSV/table/fields), the tail loop, run smoke.

Every test injects a stub client + StringIO writer + explicit TTY flag, so
nothing touches the network or a real terminal.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from agmon import cli, runner
from agmon.client import resolve

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)

RUN_ITEMS = [
    {
        "run_id": "20260709T115700-a3f9c1",
        "session_id": "s1",
        "cwd": "/home/aaron/src/agmon",
        "started_at": "2026-07-09T11:57:00+00:00",
        "ended_at": None,
        "effective_status": "running",
        "last_event_type": "assistant",
        "issue_count": 0,
        "total_cost_usd": 0.04,
    }
]

SUMMARY = {
    "run": {
        "run_id": "20260709T115700-a3f9c1",
        "session_id": "s1",
        "cwd": "/home/aaron/src/agmon",
        "started_at": "2026-07-09T11:57:00+00:00",
        "ended_at": "2026-07-09T11:59:00+00:00",
        "model": "claude-opus-4-8",
        "git_branch": "main",
        "prompt": "line1\nline2",
    },
    "status": {"effective_status": "finished", "stalled_seconds": None},
    "activity": {"last_tool": None, "last_text": None, "progress": "reading code"},
    "issues": [],
    "metrics": {"num_events": 5, "duration_seconds": 120, "num_turns": 3, "total_cost_usd": 0.04},
    "result_text": "done: **42**",
}


class StubClient:
    def __init__(self, runs=RUN_ITEMS, summary=SUMMARY):
        self._runs = runs
        self._summary = summary

    def list_runs(self, *, status=None, limit=50, session=None, labels=None):
        self.last_labels = labels
        runs = self._runs
        if session is not None:
            runs = [r for r in runs if r.get("session_id") == session]
        return runs[:limit]

    def all_runs(self):
        return self._runs

    def resolve_run_id(self, fragment):
        return resolve(self._runs, fragment)

    def get_summary(self, run_id):
        return self._summary


def run_cli(argv, client, *, tty=False):
    out = io.StringIO()
    err = io.StringIO()
    code = cli.main(argv, client=client, out=out, err=err, tty=tty, now=NOW,
                    sleep=lambda *_: None)
    return code, out.getvalue(), err.getvalue()


# -- TSV vs table selection --------------------------------------------------


def test_ls_piped_is_tsv():
    code, out, _ = run_cli(["ls"], StubClient(), tty=False)
    assert code == 0
    assert "\t" in out  # tab-separated
    assert "│" not in out and "─" not in out  # no rich table borders
    header = out.splitlines()[0].split("\t")
    assert header == cli.render.LS_HEADERS


def test_ls_tty_is_rich_table():
    _, out, _ = run_cli(["ls"], StubClient(), tty=True)
    assert "\t" not in out
    assert "─" in out or "━" in out  # rich draws a table


def test_plain_forces_tsv_on_tty():
    _, out, _ = run_cli(["ls", "--plain"], StubClient(), tty=True)
    assert "\t" in out
    assert "─" not in out


# -- fields ------------------------------------------------------------------


def test_bare_fields_lists_names():
    code, out, _ = run_cli(["show", "--fields"], StubClient(), tty=False)
    assert code == 0
    names = out.split()
    # one-level dotted names from the nested summary
    assert "status.effective_status" in names
    assert "metrics.total_cost_usd" in names
    assert "result_text" in names


def test_fields_projection_raw_values():
    code, out, _ = run_cli(
        ["show", "--fields", "run.run_id,status.effective_status,metrics.num_turns"],
        StubClient(), tty=False,
    )
    assert code == 0
    header, row = out.splitlines()
    assert header.split("\t") == ["run.run_id", "status.effective_status", "metrics.num_turns"]
    assert row.split("\t") == ["20260709T115700-a3f9c1", "finished", "3"]


def test_json_emits_underlying_object():
    import json

    code, out, _ = run_cli(["show", "--json"], StubClient(), tty=False)
    assert code == 0
    assert json.loads(out) == SUMMARY


# -- tail loop ---------------------------------------------------------------


class TailStub:
    """Yields event batches, then reports a status. Records event-poll cursors."""

    def __init__(self, batches, summaries):
        self._batches = list(batches)
        self._summaries = list(summaries)
        self.event_cursors: list[int] = []

    def resolve_run_id(self, fragment):
        return "run1"

    def get_events(self, run_id, *, after=0, limit=200, errors_only=False):
        self.event_cursors.append(after)
        if self._batches:
            return self._batches.pop(0)
        return {"events": [], "next_after": after}

    def get_summary(self, run_id):
        return self._summaries.pop(0) if self._summaries else self._summaries_last


def _assistant(seq, text):
    return {"seq": seq, "type": "assistant", "ingested_at": "2026-07-09T11:58:00+00:00",
            "payload": {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}}


def _result(seq, subtype):
    return {"seq": seq, "type": "result", "ingested_at": "2026-07-09T11:59:00+00:00",
            "payload": {"type": "result", "subtype": subtype, "num_turns": 1, "total_cost_usd": 0.04}}


def test_tail_finished_via_result_event_exit_0():
    stub = TailStub(
        batches=[{"events": [_assistant(1, "PROGRESS: working"), _result(2, "success")],
                  "next_after": 2}],
        summaries=[{"status": {"effective_status": "finished"},
                    "metrics": {"total_cost_usd": 0.04, "duration_seconds": 120}}],
    )
    code, out, _ = run_cli(["tail", "run1"], stub, tty=False)
    assert code == 0
    assert stub.event_cursors == [0]  # started from seq 0
    assert "PROGRESS: working" in out  # rendered per event


def test_tail_error_via_status_exit_1_cursor_advances():
    stub = TailStub(
        batches=[{"events": [_assistant(1, "thinking")], "next_after": 1},
                 {"events": [], "next_after": 1}],
        summaries=[{"status": {"effective_status": "error"}, "metrics": {}}],
    )
    code, _, _ = run_cli(["tail", "run1"], stub, tty=False)
    assert code == 1
    # cursor advanced via next_after: first poll after=0, second after=1
    assert stub.event_cursors == [0, 1]


def test_tail_died_via_status_exit_3():
    stub = TailStub(
        batches=[{"events": [], "next_after": 0}],
        summaries=[{"status": {"effective_status": "died"}, "metrics": {}}],
    )
    code, _, _ = run_cli(["tail", "run1"], stub, tty=False)
    assert code == 3


# -- run passthrough smoke ---------------------------------------------------


def test_run_parser_smoke():
    args = runner.build_parser().parse_args(
        ["@task.md", "--cwd", "/tmp/proj", "--permission-mode", "acceptEdits",
         "--max-turns", "80", "--bare"]
    )
    assert args.prompt == "@task.md"
    assert args.cwd == "/tmp/proj"
    assert args.permission_mode == "acceptEdits"
    assert args.max_turns == 80
    assert args.bare is True


def test_run_parser_requires_prompt():
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args([])


def _dispatch_argv(tmp_path, monkeypatch, run_args):
    """Run the wrapper with Popen short-circuited; return the meta argv."""
    import json

    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)

    def _boom(*a, **k):
        raise FileNotFoundError  # short-circuit before launching claude

    monkeypatch.setattr(runner.subprocess, "Popen", _boom)
    with pytest.raises(SystemExit):
        runner.main(run_args + ["--cwd", str(tmp_path)])
    return json.loads(next(tmp_path.glob("*.meta.json")).read_text())["argv"]


def test_run_experimental_thinking_default_off():
    args = runner.build_parser().parse_args(["hi"])
    assert args.agmon_experimental_display_thinking is False


def test_run_experimental_thinking_sets_both_claude_flags(tmp_path, monkeypatch):
    argv = _dispatch_argv(
        tmp_path, monkeypatch, ["hello", "--agmon-experimental-display-thinking"]
    )
    # One agmon flag fans out to the undocumented claude pair.
    assert argv[argv.index("--thinking") + 1] == "adaptive"
    assert argv[argv.index("--thinking-display") + 1] == "summarized"


def test_run_without_experimental_thinking_passes_nothing(tmp_path, monkeypatch):
    argv = _dispatch_argv(tmp_path, monkeypatch, ["hello"])
    assert "--thinking" not in argv
    assert "--thinking-display" not in argv
