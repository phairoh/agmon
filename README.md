# agmon

Remote agent-run monitor. A single FastAPI process that:

1. **Ingests** — a background thread scans `$AGENT_RUNS_DIR` every 2 seconds and
   folds the spool files written by `agmon run` (`<run_id>.meta.json` and
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

### The spool contract: labels

`agmon run` stamps arbitrary **labels** into `<run_id>.meta.json` under a
top-level `"labels"` object (an empty object when none) — flat string→string
facts, decided at dispatch:

```json
{ "run_id": "...", "labels": { "pipeline": "nightly", "phase": "build" } }
```

Labels are the only relation the spool contract knows about; all *meaning*
(pipelines, phases, parent/child edges) lives in the derivation layer, never in
the spool. Constraints, so a foreign writer can participate:

- keys match `[a-z0-9_.-]{1,64}`;
- values are non-empty printable strings (spaces allowed, no control
  characters), ≤256 chars;
- at most 16 labels per run; keys are unique.

The wrapper enforces these strictly (a bad `--label` never launches). The
ingester is lenient: a foreign or buggy meta.json with a malformed entry has
that entry skipped with a log line — it never fails the file.

Three keys are **reserved by convention** (still stored as ordinary labels):
`pipeline` (a grouping id), `phase` (conventionally `spec`/`build`/`review`/
`consolidate`, but any value renders — no vocabulary or ordering is enforced),
and `parent` (a run_id, the causal edge). Derivation reads these to build the
`lineage` block in a run summary: the run's pipeline/phase/parent plus its
`children` (runs whose `parent` names it) and `siblings` (runs sharing its
`pipeline`). This pipeline lineage is distinct from resume-chain lineage
(shared `session_id`); the two are never conflated.

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

`agmon` is one installable tool with two faces: a **server** (`agmon serve`)
and a **CLI client** (everything else). Bare `agmon` / `python -m agmon` is now
the CLI.

```sh
uv sync            # install deps
uv run agmon serve # start the collector + API (box-side)
uv run agmon ls    # query it from the CLI
```

> **⚠ Upgrading from an earlier version — systemd unit change required.**
> The server used to start with `python -m agmon`; that command is now the CLI.
> Change your unit's `ExecStart` to `agmon serve` and reload:
> ```sh
> systemctl --user daemon-reload && systemctl --user restart agmon
> ```
> A ready-to-copy user unit lives at [`deploy/agmon.service`](https://github.com/phairoh/agmon/blob/main/deploy/agmon.service).

### Security
agmon ships without authentication by design — it assumes deployment inside a private tailnet, where network membership is the access control. Bind to a tailnet address or firewall the port; never expose 8400 to the public internet. If you need HTTPS or want to share access beyond your own devices, put tailscale serve in front of it


## CLI

The `agmon` client curates the API into default views with escape hatches.
`serve` and `run` execute **box-side** (they touch the local spool/process);
the read commands (`ls`/`show`/`tail`/`events`/`costs`) work **from anywhere**
with `$AGMON_URL` set.

Global behaviour: the server is `--url`, else `$AGMON_URL`, else
`http://localhost:8400`. Run-id arguments are optional (omitted = the most
recent run) and match by unique **substring** (`agmon show a3f9`). Output is a
rich table on a TTY, decoration-free **TSV** when piped (`--plain` forces TSV);
`--json` dumps the underlying object(s); `--fields a,b,c` projects one-level
dotted fields (bare `--fields` lists the available names). `agmon --version`
prints the version.

```sh
agmon ls                        # fleet glance, newest first (-n N, --all)
agmon ls --status running       # filter by raw status (also --session <sid>)
agmon ls --pipeline nightly     # label filter (also --phase Y, --label k=v ×N)
agmon show a3f9                 # one run, digested (--full-prompt, --raw)
agmon artifacts a3f9            # list a run's named artifacts (name/kind/available/size)
agmon artifacts --get REVIEW.md         # fetch one artifact's content raw to stdout
agmon tail                      # live-follow the latest run (--last N)
agmon events a3f9 --errors-only     # forensics table (--type, --after, -n)
agmon costs --days 7            # cost/turn rollup (--since/--until)
agmon serve --port 8400         # start the server (--host/--port override env)
agmon run @task.md --cwd ~/src/proj    # launch + spool a run, prints run_id
agmon run @build.md --pipeline nightly --phase build --parent $spec_id  # a labeled phase run
```

`agmon ls` takes repeatable `--label k=v` filters (AND), with `--pipeline X` and
`--phase Y` as sugar. When a pipeline filter is active every listed run shares
it, so the table swaps in a `phase` column; otherwise a compact `labels` cell
appears only for the runs that carry labels. `agmon show` prints a **Pipeline**
section (id, phase, parent/children, a sibling table) whenever a run has
pipeline lineage — kept visually separate from the resume-chain lines so the two
relations don't read as one. When the result carries a `DECISIONS` section
`agmon show` renders it as its own **Decisions** block, just before the full
result. `agmon run` accepts `--label KEY=VALUE`
(repeatable) plus the `--pipeline`/`--phase`/`--parent` sugar (see the spool
contract above for constraints).

`agmon tail` is scriptable — it exits `0` on a finished run, `1` on error, and
`3` if the run died — so `agmon tail $id && next-thing` works. Fields are
discoverable from the tool itself:

```sh
agmon show --fields             # list the projectable field names, then exit
agmon show a3f9 --fields status.effective_status,metrics.total_cost_usd
```

### Client-only install

The read commands need only the client, not the server box. Install the package
anywhere on your tailnet and point it at the server:

```sh
uv tool install agmon           # or: pipx install agmon
                                # or: uv tool install git+https://github.com/phairoh/agmon
export AGMON_URL=http://server-box:8400
agmon ls
```

## Artifacts

A run produces durable things git forgets — a review file that consolidation
deletes, a DECISIONS block that lives only in the final message, the composed
prompt with its appended sections. The spool holds all of them, so every run
exposes a queryable **artifact catalog**: named things you list and fetch by
name, without knowing where any of them physically lived.

Two families. **Dispatch/section** artifacts come from the run record itself and
always list (available or not — the catalog shows what *could* exist):

| name                | source                             | available when            |
|---------------------|------------------------------------|---------------------------|
| `prompt`            | the stored composed prompt         | always                    |
| `prompt.focus`      | `FOCUS` section of the prompt      | marker present            |
| `prompt.overrides`  | `OVERRIDES` section of the prompt  | marker present            |
| `result`            | the run's result text              | run produced a result     |
| `result.decisions`  | `DECISIONS` section of the result  | marker present            |

**File** artifacts are files the run wrote, reconstructed from its `Write`/`Edit`
tool events and named by their path. `REVIEW.md` is the motivating case:
recoverable from the spool forever, even after the run itself deleted it.

```sh
agmon artifacts a3f9                    # the catalog: name, kind, available, size
agmon artifacts a3f9 --get REVIEW.md    # raw content to stdout (pipe to a file or diff)
agmon artifacts --get prompt.overrides  # a dispatch section, same interface
```

**Section-marker convention** (how foreign runs participate). A section marker
is the bare ALL-CAPS word (`DECISIONS`, `FOCUS`, `OVERRIDES`) on its own line —
optionally prefixed with markdown heading syntax (`#`–`###`) and optionally
suffixed with `:`, anchored at line start, so the word mid-prose never triggers.
A section runs from its **last** marker occurrence to the next marker line (any
bare ALL-CAPS heading) or end of text, the heading line excluded. Emit a
`## DECISIONS` heading in your final message and it becomes `result.decisions`.

**Reconstruction limitations.** A file is *reconstructable* only when its op
sequence starts from a `Write` (known-full content); an edit-only file is listed
but honestly marked unavailable — the spool knows the patches, not the base.
Reconstruction returns the last written content and makes **no claim about
current disk state**: if the run later deleted or a later run changed the file,
that is not reflected. Binary content, cross-run file state, and "file at time
T" time-travel are out of scope.

## Emacs

An Emacs client lives in [`emacs/`](emacs/) — the same read-only API rendered as
native buffers: a live-refreshing run list, per-run detail views, a follow-along
event tail, and the cost rollup. Like the CLI's read commands, it needs only the
server URL and works from anywhere on the tailnet.

```elisp
(add-to-list 'load-path "/path/to/agmon/emacs")
(require 'agmon)
(setq agmon-url "http://server-box:8400")
;; M-x agmon
```

Full install, configuration, per-buffer keys, the recommended evil/Doom setup,
and the optional tree-sitter JSON grammar are documented in
[`emacs/README.org`](emacs/README.org).

## Tests

```sh
uv run pytest
```

Tests drive the scan step directly (`app.state.ingester.scan()`) rather than
sleeping on the polling thread.

The Emacs client has its own ERT suite (`emacs/agmon-tests.el`) over its pure
layer; see [`emacs/README.org`](emacs/README.org#tests) for how to run it.

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

### `GET /healthz`

Unversioned on purpose: health is an operational endpoint with a different
stability contract than the `/v1` data API.

```sh
curl -s localhost:8400/healthz
# {"ok":true,"runs_dir":"/home/you/agent-runs","db":"...","last_scan_at":"2026-07-08T...","runs_tracked":3}
```

### `GET /v1/runs?status=<s>&limit=<n>&label=<k=v>`

Newest first by `started_at` (default `limit=50`). Each item carries the run
columns plus `prompt_preview` (first 120 chars), `event_count`,
`last_event_at`, `last_event_type`, `pid_alive`, `effective_status`,
`stalled_seconds`, `issue_count` (a count of error-flagged events), and a
`labels` object (empty when the run has none). `status` here filters on the raw
meta status, not `effective_status`. `pid_alive` is computed at request time
(`os.kill(pid, 0)`) only for `running` runs with a pid, else null.

`label=key=value` is a repeatable filter with **AND** semantics — a run must
carry every requested label to match. Malformed filter syntax (no `=`, empty
key) returns 400.

```sh
curl -s "localhost:8400/v1/runs?status=running&limit=10"
curl -s "localhost:8400/v1/runs?label=pipeline=nightly&label=phase=build"
```

### `GET /v1/runs/{run_id}`

Full run row including `prompt`, the `labels` object, and the parsed `meta_json`
(everything from the spool `.meta.json`, including fields not columnized), plus
the same computed fields. 404 with `{"error": "..."}` for an unknown id.

`model` is **observed**, not requested: it is harvested at ingest time from the
run's init system event (the resolved model that actually served, e.g.
`claude-opus-4-8[1m]`), and stays **null** for a run killed before init — "never
observed", not "unknown". The `--model` *argument* (dispatch intent, rarely
passed) is recorded separately in meta as `model_requested`, retrievable through
the `meta_json` passthrough; it is never used as a fallback for `model`.

```sh
curl -s localhost:8400/v1/runs/20260708T174951-67a5e8
```

### `GET /v1/runs/{run_id}/summary`

The full picture for one run: `{run, status, activity, issues, metrics,
result_text, decisions, lineage}`. `status` is the derived status block above;
`activity` is `{last_tool, last_text, progress}`; `issues` is the most recent 50
error-flagged tool_results and non-success results (`{seq, category, tool,
snippet}`, where `category` is `permission` / `tool_error` / `run_error`);
`metrics` is `{num_events, tool_counts, duration_seconds, num_turns,
total_cost_usd, usage}`; `result_text` is the full `result` string from the
run's result event (null if absent); `decisions` is the `DECISIONS` section
parsed from that result (null if absent — see [Artifacts](#artifacts)).
`run.labels` carries the run's labels. All events for the run are loaded per
request (no caching).

`lineage` is the pipeline-lineage block derived from the reserved labels, or
`null` when the run carries none of them:

```json
"lineage": {
  "pipeline": "nightly", "phase": "build", "parent": "<run_id>",
  "children": ["<run_id>", ...],
  "siblings": [{"run_id": "...", "phase": "spec", "effective_status": "finished", "started_at": "..."}]
}
```

`children` are runs whose `parent` label names this run; `siblings` are the
other runs sharing this run's `pipeline` (oldest first). `parent` is surfaced as
labeled — a `parent` naming a run that does not exist is rendered as-is, not
validated. This is **distinct** from the resume-chain lineage (shared
`session_id`); a run may have both.

```sh
curl -s localhost:8400/v1/runs/20260708T174951-67a5e8/summary
```

### `GET /v1/runs/{run_id}/artifacts`

The run's artifact catalog (see [Artifacts](#artifacts)): `{"artifacts": [...]}`,
both families in one list. Each item carries `name`, `kind` (`dispatch` /
`section` / `file`), `available` (bool), `bytes` when available, and a `reason`
when not (an absent section marker, or a non-reconstructable file). File items
additionally carry `path`, `ops`, `first_op` (`write`/`edit`), `last_seq`, and
`reconstructable`. Dispatch/section artifacts always list — absence is visible,
not silent. 404 for an unknown run id.

### `GET /v1/runs/{run_id}/artifacts/content?name=<name>`

One artifact's content as `text/plain; charset=utf-8`. Name resolution, in
order: exact dispatch/section name; exact file path; unique file **basename or
substring** (so `name=REVIEW.md` fetches the review without its worktree path).
`404` for an unknown name, `409` with a JSON `reason` for a listed-but-
unavailable artifact, `400` with `candidates` for an ambiguous fragment.

```sh
curl -s "localhost:8400/v1/runs/20260708T174951-67a5e8/artifacts"
curl -s "localhost:8400/v1/runs/20260708T174951-67a5e8/artifacts/content?name=REVIEW.md"
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
  labels.py    # label constraints — the spool primitive (strict wrapper, lenient ingest)
  ingest.py    # background scanner (the only writer)
  derive.py    # pure derivation functions (status/activity/issues/metrics/result/lineage)
  api.py       # FastAPI app + endpoints
  client.py    # pure HTTP client + id resolution + lineage (no printing)
  render.py    # all CLI formatting (tables/TSV/event compaction; no I/O)
  cli.py       # argument parsing + wiring (agmon ls/show/tail/events/costs/serve/run)
  runner.py    # `agmon run` — the ported launch/spool wrapper
  __main__.py  # `python -m agmon` (the CLI)
emacs/
  agmon.el        # the Emacs client (list/detail/tail/costs/json)
  agmon-tests.el  # ERT tests for the pure layer
  README.org      # install, keys, evil/Doom setup, tree-sitter, tests
tests/
  test_agmon.py        # ingester + core API
  test_adversarial.py  # byte-offset / crash-durability
  test_derive.py       # pure derivation functions
  test_stage2.py       # stage-2 endpoints + replay-as-migration
  test_stage3_server.py # result_text + healthz move
  test_client.py       # id resolution + lineage
  test_render.py       # event compaction + field flattening
  test_cli.py          # output layering + tail loop + run smoke
```
