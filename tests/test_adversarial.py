"""Adversarial tests for byte-offset handling, transaction boundaries, and
crash durability in the ingester.

Scans are driven directly; no polling thread or HTTP client is involved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agmon import db
from agmon.config import Config
from agmon.ingest import Ingester

EMOJI = "\U0001f600"  # 😀 — 4 UTF-8 bytes: f0 9f 98 80


@pytest.fixture()
def setup(tmp_path: Path):
    """Yield (runs_dir, config, ingester, new_ingester).

    ``new_ingester()`` builds a second Ingester on the same DB + runs dir to
    simulate a process restart after a crash. All connections are closed on
    teardown.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    config = Config(
        runs_dir=runs_dir,
        db_path=tmp_path / "agmon.db",
        host="127.0.0.1",
        port=8400,
    )
    db.init_db(config.db_path)
    created: list[Ingester] = []

    def new_ingester() -> Ingester:
        ing = Ingester(config)
        created.append(ing)
        return ing

    ingester = new_ingester()
    try:
        yield runs_dir, config, ingester, new_ingester
    finally:
        for ing in created:
            try:
                ing.conn.close()
            except Exception:
                pass


def events_of(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT seq, type, subtype, payload FROM events WHERE run_id=? ORDER BY seq",
        (run_id,),
    ).fetchall()


def offset_of(conn: sqlite3.Connection, path: Path) -> int | None:
    row = conn.execute(
        "SELECT byte_off FROM ingest_state WHERE path=?", (str(path),)
    ).fetchone()
    return None if row is None else row["byte_off"]


# --- F1: invalid UTF-8 in a complete line -----------------------------------


def test_invalid_utf8_line_ingested_as_unparseable(setup):
    runs_dir, _config, ingester, _new = setup
    run_id = "run-badbytes"
    path = runs_dir / f"{run_id}.jsonl"
    # A complete line (has a trailing newline) whose bytes are not valid UTF-8,
    # followed by a valid line.
    path.write_bytes(b'{"x":"\xff\xfe"}\n' + b'{"type":"result"}\n')

    ingester.scan()  # must not raise

    rows = events_of(ingester.conn, run_id)
    assert [r["seq"] for r in rows] == [1, 2]  # contiguous, both ingested
    assert rows[0]["type"] == "_unparseable"
    assert rows[1]["type"] == "result"


def test_invalid_utf8_does_not_stall_later_runs(setup):
    runs_dir, _config, ingester, _new = setup
    # 'aaa' sorts before 'zzz', so the corrupt file is processed first.
    (runs_dir / "aaa-bad.jsonl").write_bytes(b'{"x":"\xff"}\n')
    (runs_dir / "zzz-good.jsonl").write_bytes(b'{"type":"result"}\n')

    ingester.scan()  # must not raise and must not abort mid-loop

    good = events_of(ingester.conn, "zzz-good")
    assert len(good) == 1
    assert good[0]["type"] == "result"


def test_per_file_failure_does_not_abort_scan(setup):
    """An unexpected error ingesting one file is contained: other files in the
    same scan still ingest fully, and the failing file's offset never advances.
    """
    runs_dir, _config, ingester, _new = setup
    bad = runs_dir / "aaa-bad.jsonl"  # sorts first, so it fails before the good one
    good = runs_dir / "zzz-good.jsonl"
    bad.write_bytes(b'{"type":"system"}\n')
    good.write_bytes(b'{"type":"result"}\n')

    # Inject an unexpected failure for one specific file via a Python-level seam
    # (sqlite3.Connection forbids attribute assignment, so wrap the method).
    real_ingest_events = ingester._ingest_events

    def flaky_ingest_events(jsonl_path, states):
        if jsonl_path == bad:
            raise RuntimeError("injected failure ingesting the bad file")
        return real_ingest_events(jsonl_path, states)

    ingester._ingest_events = flaky_ingest_events  # type: ignore[method-assign]
    try:
        ingester.scan()  # must not raise despite the per-file failure
    finally:
        ingester._ingest_events = real_ingest_events  # type: ignore[method-assign]

    # The good file ingested fully.
    good_rows = events_of(ingester.conn, "zzz-good")
    assert [r["type"] for r in good_rows] == ["result"]

    # The failing file never progressed: no events, and its offset never advanced.
    assert events_of(ingester.conn, "aaa-bad") == []
    assert offset_of(ingester.conn, bad) is None


# --- F2: multi-byte UTF-8 split across a scan boundary (should pass) ---------


def test_multibyte_utf8_split_across_scans(setup):
    runs_dir, _config, ingester, _new = setup
    run_id = "run-emoji"
    path = runs_dir / f"{run_id}.jsonl"
    full = ('{"type":"assistant","x":"' + EMOJI + '"}\n').encode("utf-8")
    # Cut inside the 4-byte emoji so the first flush ends mid-character and has
    # no newline yet.
    cut = full.index(b"\xf0") + 2
    first, rest = full[:cut], full[cut:]

    path.write_bytes(first)
    ingester.scan()
    assert events_of(ingester.conn, run_id) == []  # nothing complete yet
    # No complete line was consumed, so no ingest_state row exists yet (the
    # ingester writes an offset only once it commits events).
    assert offset_of(ingester.conn, path) is None

    with path.open("ab") as f:
        f.write(rest)
    ingester.scan()

    rows = events_of(ingester.conn, run_id)
    assert [r["seq"] for r in rows] == [1]
    import json

    assert json.loads(rows[0]["payload"])["x"] == EMOJI  # not corrupted
    assert offset_of(ingester.conn, path) == len(full)


# --- F3: offset and events commit atomically (should pass) ------------------


class _FailOffsetConn:
    """Delegating proxy around the writer connection that fails the ingest_state
    offset write, forcing the surrounding transaction (runs + events + offset)
    to roll back as a unit.

    A Python-level seam: ``sqlite3.Connection`` is a C type that forbids
    attribute assignment, so the failure is injected by wrapping the connection
    the Ingester holds rather than patching a method on the connection itself.
    """

    def __init__(self, real: sqlite3.Connection):
        self._real = real

    def execute(self, sql, *args, **kwargs):
        # Only the offset write, not the `SELECT ... FROM ingest_state` at the
        # top of a scan.
        if "INSERT INTO ingest_state" in sql:
            raise sqlite3.OperationalError("injected failure on offset write")
        return self._real.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        return self._real.executemany(sql, *args, **kwargs)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


def test_offset_and_events_commit_atomically(setup):
    runs_dir, _config, ingester, _new = setup
    run_id = "run-atomic"
    path = runs_dir / f"{run_id}.jsonl"
    path.write_bytes(b'{"type":"system"}\n{"type":"result"}\n')

    # The offset write fails mid-transaction. Per-file failures are contained by
    # the scan loop (logged, scan continues), so scan() does not itself raise —
    # what matters is that the transaction rolled back as a unit.
    real = ingester.conn
    ingester.conn = _FailOffsetConn(real)  # type: ignore[assignment]
    try:
        ingester.scan()
    finally:
        ingester.conn = real

    # Nothing partially committed: no events, no run row, no offset row.
    assert events_of(ingester.conn, run_id) == []
    assert offset_of(ingester.conn, path) is None
    assert (
        ingester.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )

    # A clean re-scan ingests everything exactly once, seqs starting at 1.
    ingester.scan()
    rows = events_of(ingester.conn, run_id)
    assert [r["seq"] for r in rows] == [1, 2]


# --- F4: no duplicate ingestion after a simulated crash (should pass) -------


def test_no_duplicates_after_simulated_crash(setup):
    runs_dir, _config, ingester, new_ingester = setup
    run_id = "run-crash"
    path = runs_dir / f"{run_id}.jsonl"
    path.write_bytes(b'{"type":"system"}\n{"type":"assistant"}\n')

    ingester.scan()
    assert [r["seq"] for r in events_of(ingester.conn, run_id)] == [1, 2]
    off_before = offset_of(ingester.conn, path)

    # Simulate a crash: drop the connection entirely and restart on the same DB.
    ingester.conn.close()
    restarted = new_ingester()

    # A crash before any new bytes arrive: re-scanning must not re-ingest.
    restarted.scan()
    assert [r["seq"] for r in events_of(restarted.conn, run_id)] == [1, 2]
    assert offset_of(restarted.conn, path) == off_before

    # New bytes after the restart resume from the persisted offset — contiguous,
    # no duplicates of the pre-crash lines.
    with path.open("ab") as f:
        f.write(b'{"type":"result"}\n')
    restarted.scan()
    rows = events_of(restarted.conn, run_id)
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert rows[2]["type"] == "result"

    # Idempotent: another crash + rescan with no new bytes changes nothing.
    restarted.conn.close()
    again = new_ingester()
    again.scan()
    assert [r["seq"] for r in events_of(again.conn, run_id)] == [1, 2, 3]
