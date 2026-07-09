# Task: build `agmon` stage 1 — collector + query API

You are building the first real stage of a remote agent-monitoring system called
**agmon**. It runs on this box and lets a laptop on the same tailnet query the
state of headless Claude runs over HTTP.

Work in the current directory. Build it as a uv-managed Python project.

## What already exists (do not build or modify this)

A wrapper script (`agmon-run`, installed at `/home/aaron/.local/bin/agmon-run`)
launches headless Claude runs and spools their output to a directory,
`$AGENT_RUNS_DIR` (default `~/agent-runs`). For each run with id `<run_id>`
it writes:

1. `<run_id>.jsonl` — append-only newline-delimited JSON. Each line is one
   raw stream-json event from the Claude Code CLI. Top-level event types you
   will see include `system` (subtype `init` etc.), `assistant`, `user`, and a
   final `result`. Treat the payload as opaque JSON; do not assume a full
   schema beyond `type` and optional `subtype` string fields.
2. `<run_id>.meta.json` — a JSON object, atomically replaced whenever it
   changes during the run's lifetime. Fields: `run_id`, `prompt`, `cwd`,
   `git` (object with `branch`, `commit`), `model`, `permission_mode`,
   `host`, `session_id`, `pid`, `started_at`, `ended_at`, `exit_code`,
   `status` (one of `running`, `finished`, `error`), `result_subtype`,
   `num_turns`, `total_cost_usd`. Any field except `run_id` may be null.
3. `<run_id>.stderr.log` — ignore for now.

Files may appear at any time, grow while you're reading them, and the last
line of a `.jsonl` may be incomplete (mid-write). Ignore `*.tmp` files.

## What to build

A single FastAPI application with two responsibilities:

### 1. Ingester (background thread)

- Runs inside the app (start on startup, stop cleanly on shutdown).
- Every 2 seconds, scans `$AGENT_RUNS_DIR`:
  - For each `*.meta.json`: if its mtime is newer than last seen, upsert the
    corresponding row in `runs`.
  - For each `*.jsonl`: read from the stored byte offset to EOF, but only
    consume up to the **last complete newline** — never parse a partial
    trailing line; leave its bytes for the next scan. Insert each parsed line
    into `events` with a per-run monotonically increasing `seq` starting at 1.
    Lines that fail to parse as JSON are stored with `type = "_unparseable"`
    and the raw line as the payload.
  - Persist the new byte offset only after the corresponding inserts commit
    (same transaction), so a crash never skips or duplicates events.
- The ingester thread is the **only writer**. It owns its own SQLite
  connection. HTTP handlers open short-lived read-only connections.
- An event row for a run whose meta.json hasn't been seen yet must still
  ingest: create a stub `runs` row from the run_id and fill it in when the
  meta arrives.

### 2. HTTP API

SQLite database (WAL mode) at `$AGMON_DB` (default
`~/.local/share/agmon/agmon.db`). Schema:

```sql
CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  session_id     TEXT,
  prompt         TEXT,
  cwd            TEXT,
  git_branch     TEXT,
  git_commit     TEXT,
  model          TEXT,
  host           TEXT,
  pid            INTEGER,
  started_at     TEXT,
  ended_at       TEXT,
  exit_code      INTEGER,
  status         TEXT,           -- running | finished | error | (null for stub)
  result_subtype TEXT,
  num_turns      INTEGER,
  total_cost_usd REAL,
  meta_json      TEXT            -- full raw meta.json for anything not columnized
);

CREATE TABLE IF NOT EXISTS events (
  run_id      TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  ingested_at TEXT NOT NULL,
  type        TEXT,
  subtype     TEXT,
  payload     TEXT NOT NULL,     -- raw JSON line
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS ingest_state (
  path       TEXT PRIMARY KEY,   -- absolute path of the .jsonl
  run_id     TEXT NOT NULL,
  byte_off   INTEGER NOT NULL,
  meta_mtime REAL
);
```

Endpoints (all JSON):

- `GET /healthz` → `{"ok": true, "runs_dir": "...", "db": "...",
  "last_scan_at": "<iso>", "runs_tracked": <n>}`
- `GET /runs?status=<s>&limit=<n>` → newest first by `started_at`, default
  limit 50. Each item: all `runs` columns except `meta_json` and `prompt`
  (include `prompt_preview`, first 120 chars) plus `event_count`,
  `last_event_at`, `last_event_type`, and `pid_alive` (computed at request
  time via `os.kill(pid, 0)` when status is `running` and pid is set;
  null otherwise). `pid_alive: false` on a `running` run means the wrapper
  died without finalizing — surface it honestly, don't rewrite status.
- `GET /runs/{run_id}` → full row including `prompt` and parsed `meta_json`,
  plus the same computed fields. 404 with `{"error": "..."}` if unknown.
- `GET /runs/{run_id}/events?after=<seq>&limit=<n>` → events with
  `seq > after` (default `after=0`, limit 200, ordered by seq). Each item:
  `{seq, ingested_at, type, subtype, payload}` with payload as parsed JSON
  (or the raw string for `_unparseable`). Response includes `next_after`
  (the max seq returned, or the request's `after` if empty) so clients can
  poll in a loop.

Serve with uvicorn on `$AGMON_HOST`/`$AGMON_PORT` (defaults `0.0.0.0`, 8400).

## Constraints

- Python 3.12+, uv project (`uv init`, deps via `uv add`). Runtime deps:
  fastapi, uvicorn only. Use stdlib `sqlite3` and stdlib threading — no ORM,
  no aiosqlite, no watchdog; polling is fine.
- All configuration via the environment variables named above, read once at
  startup, with the defaults given.
- Keep it small: this should land around 300–400 lines of application code
  in an `agmon/` package (suggest `agmon/db.py`, `agmon/ingest.py`,
  `agmon/api.py`, `agmon/__main__.py` so `uv run python -m agmon` starts it).

## Tests (required)

pytest + fastapi TestClient, using a tmp_path runs dir and tmp db. Cover at
minimum:

1. A complete run (meta + jsonl with init/assistant/result lines) ingests
   into correct `runs` and `events` rows; endpoints return it.
2. Partial trailing line: write a jsonl whose last line has no newline, scan,
   assert it is NOT ingested; append the rest of the line plus newline, scan
   again, assert exactly one event was added and seq is contiguous.
3. Append-resume: ingest, append two more lines, re-scan, assert offsets
   advanced and no duplicates.
4. Stub run: jsonl with no meta.json ingests events; meta arriving later
   fills in the run row.
5. `after`/`next_after` pagination on the events endpoint.

Expose the scan step as a callable function so tests can drive scans
directly instead of sleeping on the polling thread.

## Definition of done

- `uv run pytest` passes.
- `uv run python -m agmon` starts the server; with real spool files present,
  `curl localhost:8400/runs` shows them.
- A `README.md` covering: env vars, how to run, how to run tests, one curl
  example per endpoint.
- Code formatted, no dead code, no TODOs left behind.

Do not build anything beyond this spec (no auth, no SSE, no launch endpoint,
no UI). Those are later stages.
