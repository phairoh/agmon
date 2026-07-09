# Task: agmon stage 2 — derived semantics

The agmon collector (stage 1) ingests raw run events into SQLite and serves
them over HTTP. Stage 2 adds the layer that turns raw events into answers:
is this run healthy, what is it doing right now, what went wrong, and what
is the fleet costing. Work in the current repo. Read the existing code and
README before changing anything. Keep the stage 1 architecture: the ingester
thread is the only writer; handlers read.

## 1. Route prefix

Move all routes under `/v1` (e.g. `/v1/runs`). No aliases for the old paths;
the only client is curl. Update the README examples.

## 2. Schema change + replay-as-migration

Add a `schema_meta` table with a single `version` row, set to `2`. Add a
column `events.is_error INTEGER NOT NULL DEFAULT 0`, classified at ingest
time (see §4). On startup, if the database's schema version is missing or
does not match the code's version: delete the database file (and its
`-wal`/`-shm` sidecars), recreate the schema, and reset all ingest offsets
so the entire spool re-ingests from
scratch. The spool is the source of truth; the database is a disposable
index. Document this policy in the README ("migrations are: drop and
replay").

## 3. Derivation module — `agmon/derive.py`

Pure functions only: they take plain data (run row dict, list of event
dicts, `now`, `pid_alive`, config values) and return dicts. No database or
filesystem access inside this module, so tests can drive it directly.

**`derive_status(run, last_ingested_at, pid_alive, now, stall_seconds)`** →
`{"effective_status": ..., "stalled_seconds": int|None, "pid_alive": bool|None}`

- meta status `finished` → passes through unchanged.
- meta status `error` with a **null** `result_subtype` → `"interrupted"`:
  the stream ended without a result event (server overload, kill signal,
  crash), which is the retryable kind of failure. With a non-null
  subtype → `"error"` (the task itself failed).
- meta status `running` and `pid_alive` is False → `"died"` (the wrapper
  stopped without finalizing meta — report it, do not rewrite the row).
- meta status `running`, pid alive, and `now - last_ingested_at >
  stall_seconds` → `"stalled"`, with `stalled_seconds` set.
- otherwise → `"running"`.

**`derive_activity(events)`** →
- `last_tool`: from the most recent assistant event containing a `tool_use`
  block: `{"seq", "tool", "target"}` where target is the block input's
  `file_path`, else `command`, else the first string value, truncated to
  120 chars. Null if no tool calls yet.
- `last_text`: first 200 chars of the most recent assistant text block.
- `progress`: agents may self-report by emitting a text line matching
  `^PROGRESS: (.+)$` (multiline). Return the most recent match, else null.

**`derive_issues(events)`** → list of `{"seq", "category", "tool",
"snippet"}`, most recent 50.
- Any `tool_result` block with `is_error` true → category `"permission"`
  if the content matches a permission/approval-denied pattern
  (case-insensitive heuristic), else `"tool_error"`. Resolve `tool` by
  matching the block's `tool_use_id` against prior `tool_use` blocks;
  null if unresolvable. Snippet: first 200 chars of the result content.
- A `result` event with subtype other than `success` → category
  `"run_error"`.

**`derive_metrics(run, events, now)`** → `{"num_events", "tool_counts"
(name → count), "duration_seconds" (started_at to ended_at, or to now if
still running), "num_turns", "total_cost_usd", "usage"}` — usage is the
raw usage object from the result event if present, passed through opaquely.

## 4. Ingest-time classification

While parsing each line, set `events.is_error = 1` when the event contains
a `tool_result` block with `is_error` true, or is a `result` event with a
non-success subtype. This makes issue counts a cheap SQL aggregate. The full
issue detail still comes from `derive_issues` at read time.

## 5. Endpoints

- `GET /v1/runs/{run_id}/summary` →
  `{"run": {...existing detail fields...}, "status": {...derive_status...},
  "activity": {...}, "issues": [...], "metrics": {...}}`. Loading all events
  for one run per request is acceptable at this scale; do not add caching.
- `GET /v1/runs` list items gain `effective_status`, `stalled_seconds`, and
  `issue_count` (SQL count of `is_error` events). Keep the list endpoint to
  a bounded number of queries — one aggregate query across the listed runs,
  not one per run, except the existing per-run `pid_alive` check.
- `GET /v1/runs/{run_id}/events` gains an optional `errors_only=true`
  filter.
- `GET /v1/stats/costs?since=<iso>&until=<iso>&bucket=day` →
  `{"buckets": [{"bucket": "2026-07-08", "runs": n, "total_cost_usd": x,
  "total_turns": n}], "totals": {...}}`, aggregated from the runs table by
  `started_at`, UTC. Default since: 30 days ago. Runs with null cost count
  toward `runs` but contribute 0 to cost.

## 6. Config

`AGMON_STALL_SECONDS` (default 300), read at startup like the others.

## Tests (required, in addition to keeping the stage 1 suite green)

1. Status matrix: running / stalled / died / finished / error /
   interrupted, driven by
   injected `now` and a stubbed `pid_alive` — no sleeping, no real pids.
2. Issue extraction: synthetic events with an `is_error` tool_result whose
   `tool_use_id` resolves to a named tool; a permission-style denial
   classified as `permission`; a non-success result event as `run_error`.
3. Activity: `last_tool` target selection (file_path vs command), and
   `progress` returning the latest of multiple PROGRESS lines.
4. Replay-as-migration: ingest a spool, then simulate a version mismatch,
   restart, and assert the rebuilt events table is identical (same seqs,
   same payloads) and `is_error` is populated.
5. Cost rollup: runs across three days, one with null cost, bucketed
   correctly with correct totals.
6. List endpoint returns `effective_status` and `issue_count` consistent
   with the above.

## Working conventions

- Commit as you go in logical units (at minimum: schema + ingest changes,
  derive module, endpoints, docs). Plain imperative messages. Work that
  is not committed does not exist.
- If any instruction here conflicts with the code, the tests, or itself:
  do not bulldoze through. If the conflict blocks the definition of done,
  stop and ask. Otherwise choose the most defensible resolution and record
  it in a DECISIONS section of your final message.
- If you discover a durable invariant a future run could violate, add it
  to CLAUDE.md (terse — it is memory, not documentation). At minimum,
  record the effective_status vocabulary and what `interrupted` means
  once implemented.

## Definition of done

- `uv run pytest` fully green — the existing 13-test suite (updated for
  `/v1`) plus the tests above.
- All work committed; `git status` clean.
- README updated: new endpoints with one curl example each, the
  drop-and-replay migration policy, the PROGRESS convention, and
  `AGMON_STALL_SECONDS`.
- `derive.py` has no imports of sqlite3, fastapi, or os.

Out of scope: SSE/streaming, launch endpoints, notifications, any UI, and
any caching layer. Those are later stages.
