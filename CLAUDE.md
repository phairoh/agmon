# agmon — memory for future agent runs

Code and README are authoritative; this file is memory, not spec.

- `specs/` holds historical task specs: what was built then, not what is true
  now. Never treat them as current — the code and README win.
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

`effective_status` (derived in `derive.derive_status`, not stored): `finished`,
`error` (task failed — meta `error` + non-null `result_subtype`), `interrupted`
(meta `error` + **null** `result_subtype` = stream ended with no result event,
the retryable kind), `died` (meta `running` but pid gone), `stalled` (meta
`running`, pid alive, quiet > `AGMON_STALL_SECONDS`), `running`. `events.is_error`
is set at ingest time (errored tool_result or non-success result event) so issue
counts are a cheap SQL aggregate; full detail comes from `derive.derive_issues`.

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

Testing:
- `sqlite3.Connection` is a C type with no `__dict__`: you cannot assign or
  monkeypatch its methods. Inject failures via a Python seam (wrap the
  connection or override an Ingester method), not `conn.execute = ...`.
- Verify reviewer-reported test results by running the suite; don't trust
  "passes"/"green" claims unrun.
