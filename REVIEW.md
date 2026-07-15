# Review — spec 007 (observed model harvest)

Subject: commit `020efd9` "Harvest observed model from the init event"
(the spec-007 diff; `fe47092` adds the spec). Reviewed against
`specs/007-model-harvest.md` and the README.

**Verdict: no bugs found.** The change is correct across all five spec
priorities. Findings below are two forward-looking observations (no
severity), plus two passing characterization tests I added to guard
invariants the feature tests leave unexercised. No xfail tests: there is
no bug to encode.

The live service on :8400 is pre-harvest vintage — its `model` column
still holds the old requested arguments (`{None, 'sonnet', 'opus'}`), not
observed init-event identities. Confirmed and *not* cited as harvest
evidence, per FOCUS.

## What I verified (observed)

- **Observed-only semantics (priority 1).** No `or model_requested`
  laundering anywhere: `model_requested` is written only by the runner
  (`runner.py:184-185`) and read back only in tests — `derive.py`,
  `api.py`, `cli.py`, `client.py`, `render.py` never consult it. The
  no-init fixture yields null (`test_no_init_event_leaves_model_null`),
  and the requested-but-no-init fixture yields null
  (`test_requested_value_is_never_a_fallback`). Real-spool leak check:
  0 runs with an init event left at null model (below).

- **Replay backfill (priority 2).** `SCHEMA_VERSION` bumped exactly once,
  3→4 (`db.py:15`); no other version change in the diff. Replayed the
  ingester over the **real** `~/agent-runs` spool into a throwaway DB:

  ```
  observed model distribution after replay over REAL spool:
      15  'claude-opus-4-8[1m]'
       7  'claude-opus-4-8'
       1  'claude-sonnet-5'
  total runs=23  runs_with_init_event=23  runs_with_model=23
  runs with init-event but NULL model (should be 0): 0
  ```

  Every historical run with an init event backfilled to its *observed*
  identity — not the requested argument the live service shows.
  `test_replay_backfills_model_from_init` also exercises the stamp-old-
  version → drop-and-replay path and asserts init wins over a legacy meta
  `model`.

- **Wrapper (priority 3).** `model_requested` written only when `--model`
  is passed, omitted otherwise; meta `model` no longer written
  (`runner.py:184`, and `model` dropped from `_META_COLUMNS`,
  `ingest.py:27-41`). README §"Observed vs. requested model" documents
  the spool-contract addition and the observed-vs-requested semantics.
  Covered by `test_wrapper_writes_model_requested_when_passed` /
  `..._omits_...`.

- **No new columns (priority 4).** `model` pre-existed (`db.py:29`); the
  db diff is the version bump alone. Confirmed no column added to any
  table.

- **Never-clobber invariant.** The meta path (`_ingest_meta`) never lists
  `model` in its INSERT/UPDATE column set, so it can neither set nor
  clear the column; only `_ingest_events` writes it, and only when
  `observed_model is not None` (`ingest.py:269-273`). Verified
  empirically on the incremental path (meta rewritten running→finished
  after model observed — model survives).

## test-honesty spot check (observed)

Two feature tests, each stubbed then restored (tree left clean):

1. Stubbed the harvest (`observed_model = None` in place of
   `_as_str_or_none(obj.get("model"))`, `ingest.py:257`):
   `test_init_event_populates_model_byte_exact` and
   `test_replay_backfills_model_from_init` both FAILED —
   `AssertionError: assert None == 'claude-sonnet-5'`. Restored.
2. Stubbed the wrapper (`if False and args.model:`, `runner.py:184`):
   `test_wrapper_writes_model_requested_when_passed` FAILED —
   `KeyError: 'model_requested'`. Restored.

Both tests bind to the real behavior they claim to.

## Observations (no severity — not bugs, no tests)

**O1 — HARDENING: incremental paths were untested.** Every feature test
is a single full scan or a full replay; none exercises the *incremental*
ingest the "never clobber" comment (`ingest.py:267-268`) exists to
protect, nor an init event arriving on a later scan than the first. Both
work — I verified them — but nothing in the suite would catch a
regression. I added two passing characterization tests
(`tests/test_review_model_harvest.py`); see below. Suggested fix: keep
them (or fold into the feature file).

**O2 — NIT: byte-exact capture fragments the `[1m]` variants.** Real init
events report `claude-opus-4-8[1m]` distinctly from `claude-opus-4-8`
(15 vs 7 above); byte-exact capture stores both verbatim. This is
*correct* per spec ("byte-exact") and cost-by-model is explicitly out of
scope, so it is not a finding — but the future rollup the spec defers to
will see one model as two rows. Flag for whoever builds that rollup; no
change now.

## Tests added by this review

`tests/test_review_model_harvest.py` (both **pass** against reviewed code):

- `test_meta_rewrite_after_init_does_not_clobber_model` — meta rewritten
  running→finished after the model was observed; model must survive.
- `test_init_arriving_in_later_scan_populates_model` — a partial init
  line on scan 1 (null), completed on scan 2 (observed).

These are regression guards for O1, not adversarial (no bug → no xfail).

## Verification transcript

Baseline (before any review edits):

```
$ uv run pytest -q
........................................................................ [ 60%]
................................................                         [100%]
120 passed, 1 warning in 0.62s
```

Spot-check 1 (harvest stubbed, then restored):

```
$ uv run pytest tests/test_model_harvest.py -q     # with observed_model = None
FAILED tests/test_model_harvest.py::test_init_event_populates_model_byte_exact
FAILED tests/test_model_harvest.py::test_replay_backfills_model_from_init
2 failed, 4 passed in 0.24s
```

Spot-check 2 (wrapper stubbed, then restored):

```
$ uv run pytest tests/test_model_harvest.py -q -k wrapper   # if False and args.model
FAILED tests/test_model_harvest.py::test_wrapper_writes_model_requested_when_passed
1 failed, 1 passed, 4 deselected in 0.23s
```

Final suite (review tests added, app code untouched):

```
$ uv run pytest -q
122 passed, 1 warning in 0.62s
```

No xfail tests in this review (no bug found). BACKLOG xfails undisturbed
(the 120→122 delta is exactly my two passing characterization tests).
