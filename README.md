# agmon

Remote agent-run monitor. A single FastAPI process that:

1. **Ingests** — a background thread scans `$AGENT_RUNS_DIR` every 2 seconds and
   folds the spool files written by `agmon-run` (`<run_id>.meta.json` and
   `<run_id>.jsonl`) into a SQLite database.
2. **Serves** — a read-only HTTP API for querying run state and streaming
   events, meant to be hit from a laptop on the same tailnet.

The ingester is the only writer; it owns one SQLite connection. HTTP handlers
open short-lived read-only connections. Event ingestion is crash-safe: each
`.jsonl` file's byte offset is persisted in the same transaction as the events
it covers, so a crash never skips or duplicates events. Partial trailing lines
(mid-write) are left for the next scan.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Configuration

All configuration is via environment variables, read once at startup:

| Variable          | Default                          | Meaning                          |
| ----------------- | -------------------------------- | -------------------------------- |
| `AGENT_RUNS_DIR`  | `~/agent-runs`                   | Directory of run spool files     |
| `AGMON_DB`        | `~/.local/share/agmon/agmon.db`  | SQLite database path (WAL mode)  |
| `AGMON_HOST`      | `0.0.0.0`                        | Bind host                        |
| `AGMON_PORT`      | `8400`                           | Bind port                        |

## Running

```sh
uv sync                 # install deps
uv run python -m agmon  # start the server (also: `uv run agmon`)
```

## Tests

```sh
uv run pytest
```

Tests drive the scan step directly (`app.state.ingester.scan()`) rather than
sleeping on the polling thread.

## API

All responses are JSON.

### `GET /healthz`

```sh
curl -s localhost:8400/healthz
# {"ok":true,"runs_dir":"/home/you/agent-runs","db":"...","last_scan_at":"2026-07-08T...","runs_tracked":3}
```

### `GET /runs?status=<s>&limit=<n>`

Newest first by `started_at` (default `limit=50`). Each item carries the run
columns plus `prompt_preview` (first 120 chars), `event_count`,
`last_event_at`, `last_event_type`, and `pid_alive`. `pid_alive` is computed at
request time (`os.kill(pid, 0)`) only for `running` runs with a pid, else null.
A `running` run with `pid_alive: false` means the wrapper died without
finalizing — surfaced as-is, status is not rewritten.

```sh
curl -s "localhost:8400/runs?status=running&limit=10"
```

### `GET /runs/{run_id}`

Full run row including `prompt` and the parsed `meta_json` (everything from the
spool `.meta.json`, including fields not columnized), plus the same computed
fields. 404 with `{"error": "..."}` for an unknown id.

```sh
curl -s localhost:8400/runs/20260708T174951-67a5e8
```

### `GET /runs/{run_id}/events?after=<seq>&limit=<n>`

Events with `seq > after` (default `after=0`, `limit=200`), ordered by `seq`.
Each event is `{seq, ingested_at, type, subtype, payload}` with `payload` parsed
back into JSON (or the raw string for lines that failed to parse, stored as
`type = "_unparseable"`). The response includes `next_after` — the max seq
returned, or the request's `after` if the page is empty — so a client can poll
in a loop.

```sh
curl -s "localhost:8400/runs/20260708T174951-67a5e8/events?after=0&limit=200"
```

## Layout

```
src/agmon/
  config.py    # env-var configuration
  db.py        # schema + connection helpers
  ingest.py    # background scanner (the only writer)
  api.py       # FastAPI app + endpoints
  __main__.py  # `python -m agmon`
tests/
  test_agmon.py
```
