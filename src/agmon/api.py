"""FastAPI app: owns the ingester and serves the read-only query API."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import db
from .config import Config
from .ingest import Ingester

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
     ORDER BY seq DESC LIMIT 1) AS last_event_type
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
    out["pid_alive"] = _pid_alive(row["status"], row["pid"])
    return out


def _parse_payload(type_: str | None, payload: str):
    if type_ == "_unparseable":
        return payload
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return payload


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

    @app.get("/runs")
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
            items.append(item)
        return {"runs": items}

    @app.get("/runs/{run_id}")
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

    @app.get("/runs/{run_id}/events")
    def get_events(run_id: str, after: int = 0, limit: int = 200):
        with read() as conn:
            rows = conn.execute(
                "SELECT seq, ingested_at, type, subtype, payload FROM events "
                "WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, after, limit),
            ).fetchall()
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

    return app


def main() -> None:
    import uvicorn

    config = Config.from_env()
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)
