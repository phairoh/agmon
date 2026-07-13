"""FastAPI app: owns the ingester and serves the read-only query API."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from . import artifacts, db, derive
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


def _labels_for(conn: sqlite3.Connection, run_ids: list[str]) -> dict[str, dict]:
    """``{run_id: {key: value, ...}}`` for the given ids (empty dict per id with
    no labels). One IN query, not a per-row scan."""
    out: dict[str, dict] = {rid: {} for rid in run_ids}
    if not run_ids:
        return out
    placeholders = ",".join("?" * len(run_ids))
    for row in conn.execute(
        f"SELECT run_id, key, value FROM run_labels WHERE run_id IN ({placeholders})",
        run_ids,
    ):
        out.setdefault(row["run_id"], {})[row["key"]] = row["value"]
    return out


def _parse_label_filters(label: list[str]) -> tuple[list[tuple[str, str]], str | None]:
    """Parse repeatable ``label=key=value`` filters. Returns ``(pairs, error)``;
    ``error`` is a message string on malformed syntax, else None."""
    pairs: list[tuple[str, str]] = []
    for raw in label:
        if "=" not in raw:
            return [], f"invalid label filter {raw!r}: expected key=value"
        key, value = raw.split("=", 1)
        if not key:
            return [], f"invalid label filter {raw!r}: empty key"
        pairs.append((key, value))
    return pairs, None


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

    _RELATED_SELECT = (
        "SELECT r.*, "
        "(SELECT MAX(ingested_at) FROM events e WHERE e.run_id = r.run_id) "
        "  AS last_event_at "
        "FROM runs r JOIN run_labels rl ON rl.run_id = r.run_id "
        "WHERE rl.key = ? AND rl.value = ?"
    )

    def _lineage_for(
        conn: sqlite3.Connection, run_id: str, labels: dict, now: str
    ) -> dict | None:
        """Assemble the pipeline-lineage pool with two indexed run_labels
        queries (pipeline members + parent-pointers), compute each candidate's
        effective_status, and hand it to the pure ``derive.derive_lineage``."""
        rows_by_id: dict[str, sqlite3.Row] = {}
        pipeline = labels.get("pipeline")
        if pipeline is not None:
            for r in conn.execute(_RELATED_SELECT, ("pipeline", pipeline)):
                rows_by_id[r["run_id"]] = r
        for r in conn.execute(_RELATED_SELECT, ("parent", run_id)):
            rows_by_id[r["run_id"]] = r
        rows_by_id.pop(run_id, None)  # the pool is *other* runs
        labels_map = _labels_for(conn, list(rows_by_id))
        related = []
        for rid, r in rows_by_id.items():
            run = {c: r[c] for c in _RUN_COLS}
            st = derive.derive_status(
                run, r["last_event_at"], _pid_alive(r["status"], r["pid"]),
                now, config.stall_seconds,
            )
            related.append(
                {
                    "run_id": rid,
                    "labels": labels_map.get(rid, {}),
                    "effective_status": st["effective_status"],
                    "started_at": r["started_at"],
                }
            )
        return derive.derive_lineage(run_id, labels, related)

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
    def list_runs(
        status: str | None = None,
        limit: int = 50,
        label: list[str] = Query(default=[]),
    ):
        filters, err = _parse_label_filters(label)
        if err is not None:
            return JSONResponse({"error": err}, status_code=400)
        sql = _RUN_SELECT
        params: list = []
        wheres: list[str] = []
        if status is not None:
            wheres.append("r.status = ?")
            params.append(status)
        # AND across repeated label= filters: each is an indexed membership test.
        for key, value in filters:
            wheres.append(
                "r.run_id IN (SELECT run_id FROM run_labels WHERE key = ? AND value = ?)"
            )
            params += [key, value]
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY (r.started_at IS NULL), r.started_at DESC LIMIT ?"
        params.append(limit)
        with read() as conn:
            rows = conn.execute(sql, params).fetchall()
            labels_map = _labels_for(conn, [row["run_id"] for row in rows])
        items = []
        for row in rows:
            item = _base_run(row)
            item["labels"] = labels_map.get(row["run_id"], {})
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
        with read() as conn:
            labels = _labels_for(conn, [run_id]).get(run_id, {})
        item = _base_run(row)
        item["labels"] = labels
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
            labels = _labels_for(conn, [run_id]).get(run_id, {})
            now = now_iso()
            lineage = _lineage_for(conn, run_id, labels, now)
        run = _base_run(row)
        run["labels"] = labels
        run["prompt"] = row["prompt"]
        run["meta_json"] = json.loads(row["meta_json"]) if row["meta_json"] else None
        result_text = derive.derive_result_text(events)
        return {
            "run": run,
            "status": derive.derive_status(
                run, row["last_event_at"], run["pid_alive"], now, config.stall_seconds
            ),
            "activity": derive.derive_activity(events),
            "issues": derive.derive_issues(events),
            "metrics": derive.derive_metrics(run, events, now),
            "result_text": result_text,
            "decisions": derive.derive_section(result_text, "DECISIONS"),
            "lineage": lineage,
        }

    @app.get("/v1/runs/{run_id}/artifacts")
    def get_artifacts(run_id: str):
        with read() as conn:
            row = conn.execute(
                _RUN_SELECT + " WHERE r.run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return JSONResponse({"error": "unknown run_id"}, status_code=404)
            events = _load_events(conn, run_id)
        catalog = artifacts.build_catalog({"prompt": row["prompt"]}, events)
        return {"artifacts": catalog}

    @app.get("/v1/runs/{run_id}/artifacts/content")
    def get_artifact_content(run_id: str, name: str):
        with read() as conn:
            row = conn.execute(
                _RUN_SELECT + " WHERE r.run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return JSONResponse({"error": "unknown run_id"}, status_code=404)
            events = _load_events(conn, run_id)
        run = {"prompt": row["prompt"]}
        try:
            content = artifacts.resolve_artifact_content(run, events, name)
        except artifacts.ArtifactNotFound as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except artifacts.ArtifactUnavailable as exc:
            return JSONResponse({"error": str(exc), "reason": exc.reason}, status_code=409)
        except artifacts.AmbiguousArtifactName as exc:
            return JSONResponse(
                {"error": str(exc), "candidates": exc.candidates}, status_code=400
            )
        return PlainTextResponse(content)

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
