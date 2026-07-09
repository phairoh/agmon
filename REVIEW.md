# Stage-2 adversarial review — agmon

Scope: `derive.py`, `ingest.py`, `db.py`, `api.py` against `specs/002-semantics.md`,
the README, and the real spool at `~/agent-runs`. Reviewed, not fixed.

Baseline before this review: `uv run pytest -q` → **34 passed**. After this
review: **35 passed, 1 xfailed** (one new living-record test passes, one xfail
encodes F1).

Bottom line: the implementation is largely correct. Status matrix, tool_use_id
resolution, errors_only, `/v1` prefix, replay-as-migration (including sidecar
removal), and cost totals all behave as specified on real and synthetic data
(see "Verified correct" below). The findings are one real gap where a genuinely
failed run is invisible in the issues surface, and three lower-severity
robustness/consistency notes.

---

## F1 — BUG (med): a result event that self-reports `is_error:true` but keeps `subtype:"success"` is never flagged; the failed run shows `issue_count:0` and `issues:[]`

**Where:** `ingest.py:65-75` (`_is_error_event`) and `derive.py:220-232`
(`derive_issues`). Both decide "is this a run error?" from `subtype != "success"`
only, ignoring the result event's own `is_error` field.

**Real specimen:** `~/agent-runs/20260709T030855-b6c59d.jsonl` — a run killed by an
API 529. Its result event is:

```
$ python3 -c "import json; [print(json.dumps({k:d.get(k) for k in ('type','subtype','is_error','result')})) for d in map(json.loads, open('/home/aaron/agent-runs/20260709T030855-b6c59d.jsonl')) if isinstance(d,dict) and d.get('type')=='result']"
{"type": "result", "subtype": "success", "is_error": true, "result": "API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again in a moment. ..."}
```

The wrapper wrote `subtype:"success"` (misleading), but the event flags
`is_error:true` and the text is a 529 failure. The meta is `status:error,
result_subtype:success`, so `derive_status` correctly yields `effective_status:
error` — but the issues surface is empty.

**Evidence (live service on :8400, same spool):**

```
$ curl -s localhost:8400/v1/runs/20260709T030855-b6c59d/summary | python3 -c "import json,sys; d=json.load(sys.stdin); print('effective_status=',d['status']['effective_status']); print('issue_count=',d['run']['issue_count']); print('issues=',d['issues'])"
effective_status= error
issue_count= 0
issues= []
```

`_is_error_event` on the exact payload (living-record test
`test_ingest_flags_result_event_with_is_error_true`, PASSED) returns `False`, so
`events.is_error` is 0 and the SQL `issue_count` aggregate is 0.

**Impact:** For the clearest failure class agmon exists to report ("what went
wrong"), a hard-errored run reports zero issues and an empty `issues` list; the
529 message is present only in raw events. Note the code follows spec §4
literally ("a result event with a **non-success subtype**") — the spec's model
doesn't anticipate a result event whose subtype lies while `is_error` is true, so
this is partly SPEC-CONFLICT. Inconsistent internally, too: the code trusts
`is_error` on *tool_result blocks* but ignores it on *result events*.

**Suggested fix (described, not applied):** In both `_is_error_event` and
`derive_issues`, treat a `result` event as an error when `subtype != "success"`
**or** its top-level `is_error is True`; use the `result` text as the snippet.
Optionally reconsider whether such a run should be `interrupted` rather than
`error`, since a 529 is the retryable kind — but that is a status-vocabulary
question for the consolidation run, not this review.

**xfail test:** `tests/test_review.py::test_result_is_error_true_surfaces_as_issue`
(XFAIL, strict, reason F1).

---

## F2 — HARDENING: cost rollup buckets and range-filters `started_at` with SQLite `date()` + string comparison, which ignore the timezone offset

**Where:** `api.py:242-252` (`stats_costs`): `date(started_at) AS bucket`,
`started_at >= ?`, `started_at < ?`.

**Evidence — SQLite `date()` uses wall-clock, not UTC:**

```
$ python3 -c "import sqlite3; c=sqlite3.connect(':memory:'); [print(repr(s),'->',c.execute('SELECT date(?)',(s,)).fetchone()[0]) for s in ['2026-07-08T23:30:00-05:00','2026-07-08T01:30:00+02:00']]"
'2026-07-08T23:30:00-05:00' -> 2026-07-08      # UTC instant is 2026-07-09T04:30Z -> should bucket 07-09
'2026-07-08T01:30:00+02:00' -> 2026-07-08      # UTC instant is 2026-07-07T23:30Z -> should bucket 07-07
```

The `WHERE started_at >= ?` filter is a raw TEXT string comparison, so a
`+02:00`-offset timestamp sorts by its literal characters, not its instant.

**Impact:** None on current data — every spool meta uses `+00:00` (verified
across all 10 runs), so buckets and the range window are exactly right today.
But the spool format is documented as agent-agnostic; a foreign writer emitting a
non-UTC offset would land runs in the wrong day and be included/excluded from the
`since`/`until` window incorrectly, silently. Spec §5 explicitly acknowledges the
implementation "compares `started_at` as strings, valid only for uniform UTC
offsets," so this is a known limitation, recorded here for completeness.

**Suggested fix:** Normalize to UTC before bucketing/comparing, e.g.
`date(started_at, 'utc')` won't help (it assumes localtime); instead store/compare
on a canonical UTC instant (`strftime('%Y-%m-%d', started_at)` also ignores the
offset — the offset must be applied first). Simplest robust route: compute the
UTC date in Python per row, or persist a normalized `started_at_utc` at ingest.

**No test:** spec-acknowledged limitation, no real trigger in the spool.

---

## F3 — LOW: `issue_count` (per-event SQL aggregate) and `derive_issues` (per-block) can disagree

**Where:** `api.py:44-45` (`issue_count` = `COUNT(*) ... WHERE is_error=1`, one per
event line) vs `derive.py:200-219` (`derive_issues` emits one entry per errored
`tool_result` block, and is capped at 50).

**Evidence:**

```
$ uv run python -c "
from agmon import derive; from agmon.ingest import _is_error_event
obj={'type':'user','message':{'content':[
  {'type':'tool_result','tool_use_id':'a','is_error':True,'content':'fail A'},
  {'type':'tool_result','tool_use_id':'b','is_error':True,'content':'fail B'}]}}
print('is_error_event (issue_count contribution):', int(_is_error_event(obj)))
print('derive_issues count:', len(derive.derive_issues([{'seq':1,'type':'user','subtype':None,'payload':obj}])))"
is_error_event (issue_count contribution): 1
derive_issues count: 2
```

**Impact:** A single user line carrying multiple errored `tool_result` blocks
(parallel tool calls) contributes 1 to `issue_count` but N to `issues`; and with
>50 issues, `issue_count` keeps climbing while `issues` is capped at 50. So the
two numbers a client sees for one run can differ. No real spool line currently
carries >1 errored tool_result (verified: scanned all 10 `.jsonl`, max 1 per
line), so this does not fire today. It is a documentation/consistency trap rather
than an active bug — each number is correct under its own definition.

**Suggested fix:** Either document explicitly that `issue_count` counts *events*
while `issues` counts *blocks* (capped), or align them (count blocks in both, or
cap consistently).

**No test:** no real trigger; both values are internally consistent per their
definitions.

---

## F4 — NIT: `derive_activity` scans `PROGRESS:` on all events, not just assistant

**Where:** `derive.py:165-169`. `last_tool`/`last_text` are correctly gated on
`_event_type(event) == "assistant"`, but the progress loop scans `_text_blocks`
of every event.

**Evidence:**

```
$ uv run python -c "
from agmon import derive
ev={'seq':1,'type':'user','subtype':None,'payload':{'type':'user','message':{'content':'PROGRESS: from a tool echo'}}}
print(derive.derive_activity([ev])['progress'])"
from a tool echo
```

**Impact:** A `PROGRESS:` line echoed back inside a `tool_result` / user message
(e.g. a command that prints the agent's own progress marker, or a subagent
transcript) would be surfaced as the run's self-reported progress. Spec §3 frames
progress as an *assistant* text line. Low likelihood, cosmetic.

**Suggested fix:** Gate the progress scan on `_event_type(event) == "assistant"`,
mirroring `last_text`.

**No test:** cosmetic.

---

## Verified correct (checked this session, no finding)

- **Status matrix boundaries.** `finished` passthrough, `error` vs `interrupted`
  by null `result_subtype`, `died` on dead pid, and stall use strict `elapsed >
  stall_seconds` (at exactly the threshold → `running`), matching spec §3. All
  covered green by `tests/test_derive.py`.
- **tool_use_id resolution** scans `tool_use` blocks across earlier events, keys a
  name map, and degrades to `null` on an unresolvable id
  (`test_issue_*`, green).
- **Replay-as-migration.** A stamped version mismatch drops the db and its
  `-wal`/`-shm` sidecars and re-ingests from the spool. Verified directly:
  ```
  after write:        ['x.db', 'x.db-shm', 'x.db-wal']
  after drop-replay:  ['x.db']
  stored_version: 2 | schema_meta rows: 1 | events remaining: 0
  ```
  `is_error` is repopulated on replay (`test_stage2.py`, green). Version row is
  created idempotently (`INSERT ... WHERE NOT EXISTS`).
- **`errors_only` filter** returns only `is_error=1` events; **`/healthz` is now
  `/v1/healthz`** (bare `/healthz` → 404, confirmed by curl); no old-path aliases.
- **Cost totals** for uniform-UTC data (all real runs) bucket and total correctly,
  null cost counts as a run contributing 0 (`test_cost_rollup_*`, green).
- **`derive.py` purity:** no `import sqlite3 / fastapi / os` (grep clean).

---

## Verification transcript

```
$ uv run pytest -q
...............................x....                                     [100%]
35 passed, 1 xfailed, 1 warning in 0.39s
```

```
$ uv run pytest tests/test_review.py -v
tests/test_review.py::test_ingest_flags_result_event_with_is_error_true PASSED [ 50%]
tests/test_review.py::test_result_is_error_true_surfaces_as_issue        XFAIL  [100%]
```

The 34 pre-existing tests still pass; the sole new passing test is a living
record of current (buggy) `_is_error_event` behavior; the F1 xfail reports XFAIL
(strict).
