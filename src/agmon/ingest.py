"""Background ingester: scans the runs dir and writes rows into SQLite.

The ingester is the only writer. Each scan is idempotent: byte offsets are
persisted in the same transaction as the events they cover, so a crash mid-scan
never skips or duplicates events.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import Config
from .labels import validate_label

log = logging.getLogger("agmon.ingest")

# Meta fields that get their own column. `git` is nested and handled separately;
# everything (including unlisted fields) is also kept verbatim in meta_json.
# `model` is deliberately NOT here: it is *observed* (harvested from the init
# event in _ingest_events), never taken from meta — intent is not observation.
# Keeping it out of the meta upsert also means a later meta rewrite can't clobber
# the harvested value.
_META_COLUMNS = (
    "run_id",
    "session_id",
    "prompt",
    "cwd",
    "host",
    "pid",
    "started_at",
    "ended_at",
    "exit_code",
    "status",
    "result_subtype",
    "num_turns",
    "total_cost_usd",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_row(data: dict) -> dict:
    git = data.get("git") or {}
    row = {col: data.get(col) for col in _META_COLUMNS}
    row["git_branch"] = git.get("branch")
    row["git_commit"] = git.get("commit")
    row["meta_json"] = json.dumps(data)
    return row


def _as_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _label_rows(run_id: str, data: dict) -> list[tuple[str, str, str]]:
    """Well-formed (run_id, key, value) rows from a meta.json ``labels`` object.

    Lenient by design (the wrapper is the strict gate): a non-object ``labels``
    or any entry that violates the constraints is skipped with a log line, so a
    foreign or buggy writer can never stall the file — per the containment
    invariant."""
    labels = data.get("labels")
    if labels is None:
        return []
    if not isinstance(labels, dict):
        log.warning("run %s: ignoring non-object labels", run_id)
        return []
    rows = []
    for key, value in labels.items():
        err = validate_label(key, value)
        if err is not None:
            log.warning("run %s: skipping label — %s", run_id, err)
            continue
        rows.append((run_id, key, value))
    return rows


def _blocks(obj: dict) -> list:
    """Content blocks of a message event, or []."""
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _is_error_event(obj: object) -> bool:
    """True if this event should be flagged as an error at ingest time: a
    tool_result block with is_error true, or a result event that either has a
    non-success subtype or self-reports is_error true (a subtype may lie —
    e.g. a 529-overload result stamped subtype:"success", is_error:true)."""
    if not isinstance(obj, dict):
        return False
    if obj.get("type") == "result" and (
        obj.get("subtype") != "success" or obj.get("is_error") is True
    ):
        return True
    return any(
        b.get("type") == "tool_result" and b.get("is_error") is True
        for b in _blocks(obj)
    )


class Ingester:
    def __init__(self, config: Config):
        self.config = config
        self.conn = db.writer(config.db_path)
        self.last_scan_at: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="agmon-ingest", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.conn.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:
                log.exception("scan failed")
            self._stop.wait(2.0)

    # -- scanning --------------------------------------------------------

    def scan(self) -> None:
        """One pass over the runs dir. Safe to call directly (e.g. from tests)."""
        with self._lock:
            self._scan()

    def _scan(self) -> None:
        runs_dir = self.config.runs_dir
        if runs_dir.is_dir():
            states = {
                r["path"]: r
                for r in self.conn.execute(
                    "SELECT path, run_id, byte_off, meta_mtime FROM ingest_state"
                )
            }
            for meta_path in sorted(runs_dir.glob("*.meta.json")):
                try:
                    self._ingest_meta(meta_path, states)
                except Exception:
                    log.exception("failed to ingest meta %s", meta_path)
            for jsonl_path in sorted(runs_dir.glob("*.jsonl")):
                try:
                    self._ingest_events(jsonl_path, states)
                except Exception:
                    log.exception("failed to ingest events %s", jsonl_path)
        self.last_scan_at = now_iso()

    def _ingest_meta(self, meta_path: Path, states: dict) -> None:
        run_id = meta_path.name[: -len(".meta.json")]
        jsonl_key = str(self.config.runs_dir / f"{run_id}.jsonl")
        try:
            mtime = meta_path.stat().st_mtime
        except OSError:
            return
        prev = states.get(jsonl_key)
        if (
            prev is not None
            and prev["meta_mtime"] is not None
            and mtime <= prev["meta_mtime"]
        ):
            return
        try:
            data = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            # Atomic replace means a read failure is transient; retry next scan.
            return
        if not isinstance(data, dict):
            return
        data.setdefault("run_id", run_id)
        row = _run_row(data)
        cols = list(row.keys())
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "run_id")
        label_rows = _label_rows(run_id, data)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO runs ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(run_id) DO UPDATE SET {updates}",
                [row[c] for c in cols],
            )
            # Labels are dispatch-time facts, but meta.json is rewritten as the
            # run progresses; re-derive them idempotently from the current file.
            self.conn.execute("DELETE FROM run_labels WHERE run_id=?", (run_id,))
            if label_rows:
                self.conn.executemany(
                    "INSERT INTO run_labels (run_id, key, value) VALUES (?, ?, ?)",
                    label_rows,
                )
            self.conn.execute(
                "INSERT INTO ingest_state (path, run_id, byte_off, meta_mtime) "
                "VALUES (?, ?, 0, ?) "
                "ON CONFLICT(path) DO UPDATE SET "
                "meta_mtime=excluded.meta_mtime, run_id=excluded.run_id",
                (jsonl_key, run_id, mtime),
            )

    def _ingest_events(self, jsonl_path: Path, states: dict) -> None:
        key = str(jsonl_path)
        run_id = jsonl_path.name[: -len(".jsonl")]
        prev = states.get(key)
        off = prev["byte_off"] if prev is not None else 0
        try:
            with open(jsonl_path, "rb") as f:
                f.seek(off)
                data = f.read()
        except OSError:
            return
        if not data:
            return
        nl = data.rfind(b"\n")
        if nl == -1:
            # No complete line yet; leave the partial bytes for the next scan.
            return
        chunk = data[: nl + 1]
        new_off = off + len(chunk)

        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        rows = []
        init_model: str | None = None
        for raw in chunk.split(b"\n"):
            if not raw.strip():
                continue
            seq += 1
            text = raw.decode("utf-8", errors="replace")
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                rows.append((run_id, seq, "_unparseable", None, text, 0))
                continue
            if isinstance(obj, dict):
                typ = _as_str_or_none(obj.get("type"))
                sub = _as_str_or_none(obj.get("subtype"))
                # The init system event carries the *observed* resolved model.
                if typ == "system" and sub == "init":
                    m = _as_str_or_none(obj.get("model"))
                    if m is not None:
                        init_model = m
            else:
                typ, sub = None, None
            rows.append((run_id, seq, typ, sub, text, int(_is_error_event(obj))))

        ingested_at = now_iso()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO runs (run_id) VALUES (?)", (run_id,)
            )
            if rows:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO events "
                    "(run_id, seq, ingested_at, type, subtype, payload, is_error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(r[0], r[1], ingested_at, r[2], r[3], r[4], r[5]) for r in rows],
                )
            # Harvest the observed model from the init event (present only in the
            # first chunk); leave it untouched otherwise so it stays null until
            # observed and is never reset by later chunks.
            if init_model is not None:
                self.conn.execute(
                    "UPDATE runs SET model = ? WHERE run_id = ?", (init_model, run_id)
                )
            self.conn.execute(
                "INSERT INTO ingest_state (path, run_id, byte_off, meta_mtime) "
                "VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(path) DO UPDATE SET "
                "byte_off=excluded.byte_off, run_id=excluded.run_id",
                (key, run_id, new_off),
            )
