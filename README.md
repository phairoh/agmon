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

On top of the raw ingest, a small derivation layer (`agmon/derive.py`, pure
functions) turns events into answers: a run's effective status, what it is
doing right now, what went wrong, and what the fleet is costing.

### Migrations are: drop and replay

The spool is the source of truth; the SQLite database is a disposable index.
The schema carries a version (`schema_meta.version`). On startup, if the db's
version is missing or does not match the code's `SCHEMA_VERSION`, agmon deletes
the db file (and its `-wal`/`-shm` sidecars) and recreates it empty — which
resets all ingest offsets, so the next scan re-ingests the entire spool from
scratch. There is no in-place migration path; never hand-edit the db.

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
| `AGMON_STALL_SECONDS` | `300`                        | Quiet time before a live `running` run is reported `stalled` |

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

All responses are JSON. All routes are under `/v1`.

### Effective status

Several endpoints report an `effective_status` derived from the run's meta
status, process liveness, and event recency. The vocabulary:

| value         | meaning                                                          |
| ------------- | --------------------------------------------------------------- |
| `finished`    | meta status `finished` (passed through)                         |
| `error`       | meta status `error` with a non-null `result_subtype` — the task itself failed |
| `interrupted` | meta status `error` with a **null** `result_subtype` — the stream ended with no result event (overload, kill signal, crash); the retryable kind |
| `died`        | meta status `running` but the pid is gone — the wrapper stopped without finalizing (surfaced as-is, the row is not rewritten) |
| `stalled`     | meta status `running`, pid alive, but no new events for more than `AGMON_STALL_SECONDS`; `stalled_seconds` is set |
| `running`     | live and recent                                                 |

### `GET /v1/healthz`

```sh
curl -s localhost:8400/v1/healthz
# {"ok":true,"runs_dir":"/home/you/agent-runs","db":"...","last_scan_at":"2026-07-08T...","runs_tracked":3}
```

### `GET /v1/runs?status=<s>&limit=<n>`

Newest first by `started_at` (default `limit=50`). Each item carries the run
columns plus `prompt_preview` (first 120 chars), `event_count`,
`last_event_at`, `last_event_type`, `pid_alive`, `effective_status`,
`stalled_seconds`, and `issue_count` (a count of error-flagged events). `status`
here filters on the raw meta status, not `effective_status`. `pid_alive` is
computed at request time (`os.kill(pid, 0)`) only for `running` runs with a
pid, else null.

```sh
curl -s "localhost:8400/v1/runs?status=running&limit=10"
```

### `GET /v1/runs/{run_id}`

Full run row including `prompt` and the parsed `meta_json` (everything from the
spool `.meta.json`, including fields not columnized), plus the same computed
fields. 404 with `{"error": "..."}` for an unknown id.

```sh
curl -s localhost:8400/v1/runs/20260708T174951-67a5e8
```

### `GET /v1/runs/{run_id}/summary`

The full picture for one run: `{run, status, activity, issues, metrics}`.
`status` is the derived status block above; `activity` is `{last_tool,
last_text, progress}`; `issues` is the most recent 50 error-flagged
tool_results and non-success results (`{seq, category, tool, snippet}`, where
`category` is `permission` / `tool_error` / `run_error`); `metrics` is
`{num_events, tool_counts, duration_seconds, num_turns, total_cost_usd,
usage}`. All events for the run are loaded per request (no caching).

```sh
curl -s localhost:8400/v1/runs/20260708T174951-67a5e8/summary
```

### `GET /v1/runs/{run_id}/events?after=<seq>&limit=<n>&errors_only=<bool>`

Events with `seq > after` (default `after=0`, `limit=200`), ordered by `seq`.
Each event is `{seq, ingested_at, type, subtype, payload}` with `payload` parsed
back into JSON (or the raw string for lines that failed to parse, stored as
`type = "_unparseable"`). With `errors_only=true`, only error-flagged events are
returned. The response includes `next_after` — the max seq returned, or the
request's `after` if the page is empty — so a client can poll in a loop.

```sh
curl -s "localhost:8400/v1/runs/20260708T174951-67a5e8/events?after=0&errors_only=true"
```

### `GET /v1/stats/costs?since=<iso>&until=<iso>&bucket=day`

Cost/turn rollup over the runs table, bucketed by `started_at` date in UTC
(default `since` = 30 days ago; `until` optional). Runs with a null cost still
count toward `runs` but contribute 0 to cost.

```sh
curl -s "localhost:8400/v1/stats/costs?since=2026-06-01T00:00:00%2B00:00&bucket=day"
# {"buckets":[{"bucket":"2026-07-08","runs":3,"total_cost_usd":0.31,"total_turns":12}],"totals":{...}}
```

### Self-reported progress

An agent can surface a one-line progress note by emitting an assistant text
line matching `^PROGRESS: (.+)$`. The most recent such line is returned as
`activity.progress` in the run summary.

## Layout

```
src/agmon/
  config.py    # env-var configuration
  db.py        # schema + connection helpers, drop-and-replay migration
  ingest.py    # background scanner (the only writer)
  derive.py    # pure derivation functions (status/activity/issues/metrics)
  api.py       # FastAPI app + endpoints
  __main__.py  # `python -m agmon`
tests/
  test_agmon.py        # ingester + core API
  test_adversarial.py  # byte-offset / crash-durability
  test_derive.py       # pure derivation functions
  test_stage2.py       # stage-2 endpoints + replay-as-migration
```
