"""FastAPI app: owns the ingester and serves the read-only query API."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import db, derive
from .config import Config
from .ingest import Ingester, now_iso

# runs columns exposed by the API, in a stable order (meta_json/prompt handled
# specially per-endpoint).
_RUN_COLS = (
    "run_id",
    "session_id",
    "cwd",
    "git_branch",
    "git_commit",
    "model",
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

_RUN_SELECT = """
SELECT r.*,
  (SELECT COUNT(*) FROM events e WHERE e.run_id = r.run_id) AS event_count,
  (SELECT MAX(ingested_at) FROM events e WHERE e.run_id = r.run_id) AS last_event_at,
  (SELECT type FROM events e WHERE e.run_id = r.run_id
     ORDER BY seq DESC LIMIT 1) AS last_event_type,
  (SELECT COUNT(*) FROM events e WHERE e.run_id = r.run_id AND e.is_error = 1)
     AS issue_count
FROM runs r
"""


def _pid_alive(status: str | None, pid: object) -> bool | None:
    if status != "running" or not pid:
        return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return None


def _base_run(row: sqlite3.Row) -> dict:
    out = {c: row[c] for c in _RUN_COLS}
    out["event_count"] = row["event_count"]
    out["last_event_at"] = row["last_event_at"]
    out["last_event_type"] = row["last_event_type"]
    out["issue_count"] = row["issue_count"]
    out["pid_alive"] = _pid_alive(row["status"], row["pid"])
    return out


def _parse_payload(type_: str | None, payload: str):
    if type_ == "_unparseable":
        return payload
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return payload


def _load_events(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """All events for a run as parsed event dicts, in seq order."""
    rows = conn.execute(
        "SELECT seq, ingested_at, type, subtype, payload, is_error FROM events "
        "WHERE run_id = ? ORDER BY seq",
        (run_id,),
    ).fetchall()
    return [
        {
            "seq": row["seq"],
            "ingested_at": row["ingested_at"],
            "type": row["type"],
            "subtype": row["subtype"],
            "payload": _parse_payload(row["type"], row["payload"]),
            "is_error": row["is_error"],
        }
        for row in rows
    ]


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    db.init_db(config.db_path)
    ingester = Ingester(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ingester.start()
        try:
            yield
        finally:
            ingester.stop()

    app = FastAPI(title="agmon", lifespan=lifespan)
    app.state.config = config
    app.state.ingester = ingester

    @contextmanager
    def read():
        conn = db.reader(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _status_for(row: sqlite3.Row) -> dict:
        run = {c: row[c] for c in _RUN_COLS}
        return derive.derive_status(
            run,
            row["last_event_at"],
            _pid_alive(row["status"], row["pid"]),
            now_iso(),
            config.stall_seconds,
        )

    @app.get("/healthz")
    def healthz():
        with read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return {
            "ok": True,
            "runs_dir": str(config.runs_dir),
            "db": str(config.db_path),
            "last_scan_at": ingester.last_scan_at,
            "runs_tracked": n,
        }

    @app.get("/v1/runs")
    def list_runs(status: str | None = None, limit: int = 50):
        sql = _RUN_SELECT
        params: list = []
        if status is not None:
            sql += " WHERE r.status = ?"
            params.append(status)
        sql += " ORDER BY (r.started_at IS NULL), r.started_at DESC LIMIT ?"
        params.append(limit)
        with read() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            item = _base_run(row)
            prompt = row["prompt"]
            item["prompt_preview"] = prompt[:120] if prompt else prompt
            status_ = _status_for(row)
            item["effective_status"] = status_["effective_status"]
            item["stalled_seconds"] = status_["stalled_seconds"]
            items.append(item)
        return {"runs": items}

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str):
        with read() as conn:
            row = conn.execute(
                _RUN_SELECT + " WHERE r.run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return JSONResponse({"error": "unknown run_id"}, status_code=404)
        item = _base_run(row)
        item["prompt"] = row["prompt"]
        item["meta_json"] = json.loads(row["meta_json"]) if row["meta_json"] else None
        return item

    @app.get("/v1/runs/{run_id}/summary")
    def get_summary(run_id: str):
        with read() as conn:
            row = conn.execute(
                _RUN_SELECT + " WHERE r.run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return JSONResponse({"error": "unknown run_id"}, status_code=404)
            events = _load_events(conn, run_id)
        run = _base_run(row)
        run["prompt"] = row["prompt"]
        run["meta_json"] = json.loads(row["meta_json"]) if row["meta_json"] else None
        now = now_iso()
        return {
            "run": run,
            "status": derive.derive_status(
                run, row["last_event_at"], run["pid_alive"], now, config.stall_seconds
            ),
            "activity": derive.derive_activity(events),
            "issues": derive.derive_issues(events),
            "metrics": derive.derive_metrics(run, events, now),
            "result_text": derive.derive_result_text(events),
        }

    @app.get("/v1/runs/{run_id}/events")
    def get_events(
        run_id: str, after: int = 0, limit: int = 200, errors_only: bool = False
    ):
        sql = (
            "SELECT seq, ingested_at, type, subtype, payload FROM events "
            "WHERE run_id = ? AND seq > ?"
        )
        params: list = [run_id, after]
        if errors_only:
            sql += " AND is_error = 1"
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit)
        with read() as conn:
            rows = conn.execute(sql, params).fetchall()
        events = [
            {
                "seq": row["seq"],
                "ingested_at": row["ingested_at"],
                "type": row["type"],
                "subtype": row["subtype"],
                "payload": _parse_payload(row["type"], row["payload"]),
            }
            for row in rows
        ]
        next_after = events[-1]["seq"] if events else after
        return {"events": events, "next_after": next_after}

    @app.get("/v1/stats/costs")
    def stats_costs(
        since: str | None = None, until: str | None = None, bucket: str = "day"
    ):
        if since is None:
            since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        sql = (
            "SELECT date(started_at) AS bucket, COUNT(*) AS runs, "
            "COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd, "
            "COALESCE(SUM(num_turns), 0) AS total_turns "
            "FROM runs WHERE started_at IS NOT NULL AND started_at >= ?"
        )
        params: list = [since]
        if until is not None:
            sql += " AND started_at < ?"
            params.append(until)
        sql += " GROUP BY bucket ORDER BY bucket"
        with read() as conn:
            rows = conn.execute(sql, params).fetchall()
        buckets = [
            {
                "bucket": row["bucket"],
                "runs": row["runs"],
                "total_cost_usd": row["total_cost_usd"],
                "total_turns": row["total_turns"],
            }
            for row in rows
        ]
        totals = {
            "runs": sum(b["runs"] for b in buckets),
            "total_cost_usd": sum(b["total_cost_usd"] for b in buckets),
            "total_turns": sum(b["total_turns"] for b in buckets),
        }
        return {"buckets": buckets, "totals": totals}

    return app


def main() -> None:
    import uvicorn

    config = Config.from_env()
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)
