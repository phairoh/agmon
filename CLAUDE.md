# agmon — memory for future agent runs

Code and README are authoritative; this file is memory, not spec.

- `specs/` holds historical task specs: what was built then, not what is true
  now. Never treat them as current — the code and README win.
- `notes/friction.md` is the shared friction log — rough edges in the API,
  tooling, or workflow that a client bumps into (operator or agent; the
  friction is the same either way). Append to it liberally as you hit things;
  each entry is a candidate future enhancement, not a committed one. It is
  raw and unprioritized by design — distinct from `BACKLOG.md` (policy-deferred
  work with strict xfails) and from `specs/` (per-task briefs). Triage and
  prioritize over time; graduating an item means writing it up properly
  elsewhere, not leaving it here.
- The spool (`~/agent-runs`, `$AGENT_RUNS_DIR`) is the source of truth; the
  SQLite DB is a disposable index. Migration = bump `db.SCHEMA_VERSION`, drop DB,
  replay the spool. Don't hand-edit the DB. `init_db` enforces this: on a
  `schema_meta.version` mismatch it deletes the db file + `-wal`/`-shm` sidecars
  and recreates empty, which resets ingest offsets so the whole spool re-ingests.
- The ingester thread is the sole writer (scans serialized by a lock); HTTP
  handlers use short-lived read-only connections. `MAX(seq)` is read before the
  write transaction — safe only under that single-writer invariant.
- All HTTP routes live under `/v1`. Derived answers live in `derive.py` (pure:
  no sqlite3/fastapi/os imports, so tests drive them with plain dicts).
- Bump the schema version whenever ingest-time derivation logic changes, not
  only the schema shape — stale classification is a silent index corruption.
- Labels are flat string→string facts in the spool (`meta.json` `labels`,
  table `run_labels`); all meaning — pipeline, phase, parent lineage — lives in
  derivation (`derive.derive_lineage`), read from reserved keys. Never add
  pipeline/phase/parent columns or fields to the spool contract; that inversion
  is deliberate. The wrapper (`labels.build_labels`) enforces the constraints
  strictly; the ingester (`_label_rows`) applies them leniently (skip + log a
  bad entry, never stall the file). Pipeline lineage is distinct from
  resume-chain lineage (`session_id`); never conflate them in output.

`effective_status` (derived in `derive.derive_status`, not stored): `finished`,
`error` (task failed — meta `error` + non-null `result_subtype`), `interrupted`
(meta `error` + **null** `result_subtype` = stream ended with no result event,
the retryable kind), `died` (meta `running` but pid gone), `stalled` (meta
`running`, pid alive, quiet > `AGMON_STALL_SECONDS`), `running`. `events.is_error`
is set at ingest time (errored tool_result, or a result event with a non-success
subtype **or** `is_error:true` — a subtype can lie, e.g. a 529 stamped
`subtype:"success"`) so issue counts are a cheap SQL aggregate; full detail comes
from `derive.derive_issues`. `issue_count` (per-*event* SQL count of `is_error`)
and the `issues` list (per-*block*, capped at 50) measure different things and may
legitimately diverge — a line with N errored tool_result blocks adds 1 to
`issue_count` but N to `issues`; don't assert they're equal.

Ingestion (`ingest.py`):
- Reads spool files in binary mode; `byte_off` is a true byte offset. Never
  consume past the last newline; a partial trailing line waits for the next scan
  (a multi-byte UTF-8 char can't be split by `\n`).
- Lines parse as raw bytes with `except (json.JSONDecodeError,
  UnicodeDecodeError) -> _unparseable`. Structurally-valid JSON carrying invalid
  UTF-8 is deliberately quarantined as `_unparseable`, not decoded lossily.
- The new byte offset commits in the same transaction as the events it covers,
  so a crash never skips or duplicates (living test: F3 in
  `tests/test_adversarial.py`).
- One bad spool file must never stall ingestion of others: the scan loop
  isolates per-file failures (log with the path, continue).
- `seq` advances per appended non-blank line. Single-writer keeps seqs gapless;
  a gap would make `after=`/`next_after` clients silently skip events.
- Spool files are append-only. Truncating/replacing one shorter than the stored
  offset silently stops that run (offset stuck past EOF); this is undetected.

CLI (`cli.py` wires `client.py` + `render.py`; all three are terminal- and
network-independent so tests inject a stub client, a StringIO writer, and a
TTY flag):
- `agmon tail` exit-code contract (scriptable: `agmon tail $id && next`):
  `finished`→0, `error`/`interrupted`→1, `died`→3. On a result event the code
  is from its subtype (success→0 else 1); otherwise from the effective_status.
  `stalled`/`running` are non-terminal and keep polling.
- Run-id args resolve by unique **substring** (exact full id wins, ambiguous
  errors listing candidates, omitted → latest); the resolver is pure
  (`client.resolve`), tested without a server.
- `--fields` flattens the JSON one level with dots; default columns render
  times/durations human-relative while `--fields`/`--json` keep raw values.
- `/healthz` is unversioned (operational, distinct stability contract); the
  data API stays under `/v1`.
- `serve` and `run` execute box-side (local spool/process); read commands work
  from anywhere with `$AGMON_URL` set.

Testing:
- `sqlite3.Connection` is a C type with no `__dict__`: you cannot assign or
  monkeypatch its methods. Inject failures via a Python seam (wrap the
  connection or override an Ingester method), not `conn.execute = ...`.
- Verify reviewer-reported test results by running the suite; don't trust
  "passes"/"green" claims unrun.

## Run conventions (every dispatched task)

- Commit as you go, in logical units, plain imperative messages. Work
  that is not committed does not exist. Finish with `git status` clean.
- On conflict (spec vs code vs tests vs itself): if it blocks the
  definition of done, stop and ask. Otherwise take the most defensible
  resolution and record it in DECISIONS.
- DECISIONS is a section of your final message — never a file in the
  repo. One entry per judgment call or deviation, with rationale.
- Regression test before fix: observe it fail, fix, observe it pass.
  Never weaken a test to make it pass — a wrong-seeming test is a
  conflict; see above.
- Any "passes"/"fails" claim you report must come from a command you ran
  in this session. Label unexecuted beliefs UNVERIFIED.
- Durable invariants a future run could violate go in this file, tersely.
  Things enforced by tests do not; the tests are their home.
- BACKLOG.md items are deferred by policy. Never fix them while on
  another task — their strict xfails will XPASS and fail the suite.
- Create no root files beyond README.md, CLAUDE.md, BACKLOG.md.
  (REVIEW.md exists transiently: written only by review tasks, consumed
  and deleted by consolidation.)
