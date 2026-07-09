Known, deliberately deferred items. Each BUG's authoritative
reproduction is its named strict-xfail test — the suite re-verifies
this backlog on every run. Do NOT fix these opportunistically while on
another task: an unexpected XPASS fails the suite. To take an item:
remove the xfail marker, fix, delete the entry.

## B1 — HARDENING: `stats_costs` range filter compares `started_at` as raw strings

- **Origin:** from stage-2 review F2.
- **Severity:** hardening (no real trigger today — every spool meta uses a
  `+00:00` offset, verified across all runs).
- **Where:** `api.py` `stats_costs`, the `started_at >= ?` / `started_at < ?`
  WHERE clauses.
- **Impact:** the `since`/`until` window is a TEXT comparison, so a foreign
  writer emitting a non-`+00:00` offset (the spool format is documented as
  agent-agnostic) would be included/excluded from the window by its literal
  characters, not its UTC instant — e.g. `2026-07-08T01:30:00+02:00`
  (UTC 2026-07-07T23:30Z) sorts `>=` a `2026-07-08T00:00:00+00:00` bound
  though its instant is before it.
- **Not the bucketing:** the review's headline claim that `date(started_at)`
  ignores the offset did NOT reproduce — SQLite `date()` normalizes an
  explicit offset to UTC (`date('2026-07-08T23:30:00-05:00')` → `2026-07-09`),
  which is exactly the UTC bucketing spec §5 requires. Only the range filter
  is the residual.
- **Suggested fix:** normalize `started_at` to a canonical UTC instant before
  comparing (persist a `started_at_utc` at ingest, or compare on
  `strftime`-of-a-UTC-normalized value), then filter on that.
- **Test:** none — hardening entry-only, no real trigger in the spool.
