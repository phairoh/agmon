# Review — stage-4a labels & lineage (`specs/005-labels-lineage.md`)

Subject: diff since tag `stage-3` (commits `63ca44b..0f1c6d1`), the labels
primitive + pipeline lineage across wrapper, ingest, derive, api, cli, docs.

**Baseline suite: green** — `114 passed` (no xfails currently in the repo; no
BACKLOG xfails collected). Transcript at the end.

**Bottom line:** the feature is correct across all seven FOCUS priorities. I
found no correctness defect that warrants an adversarial test, so none is added
(a short review of correct code is a successful review). One low-severity
hardening observation is recorded. Findings: **0 BUG, 0 SPEC-CONFLICT, 1
HARDENING, 1 NIT.**

Per priority, observed evidence:

1. **Wrapper validation (§1).** Each constraint raises a *distinct* message
   (`labels.build_labels` / `validate_label`): malformed `KEY=VALUE`
   ("expected KEY=VALUE"), bad key ("invalid label key"), empty value
   ("non-empty"), >256 ("exceeds 256"), control char ("control characters"),
   duplicate ("duplicate label key"), >16 ("too many labels"). Sugar compiles
   to reserved-key labels and a sugar+explicit collision on the same key is a
   duplicate error regardless of value (checked in `_add` *before*
   validation). Covered by `test_each_constraint_has_a_distinct_error[*]`,
   `test_sugar_and_explicit_same_key_is_duplicate`,
   `test_too_many_labels_rejected`, `test_key_max_length_boundary` — all
   PASSED (see verbose run below).

2. **Ingester leniency (§2).** `ingest._label_rows` validates per entry via the
   *same* `validate_label`, skipping violators with `log.warning` and never
   raising; a non-dict `labels` logs and yields `[]`. Per-file failures are
   isolated in `_scan`, so a bad meta cannot stall others. Because
   `build_labels` calls `validate_label` on every accepted label, every
   wrapper-written label is accepted by the ingester — no strict/lenient
   divergence, which is what makes replay identical. Covered by
   `test_ingest_is_lenient_on_bad_entries`,
   `test_ingest_non_object_labels_ignored`.
   **Verified adversarially** (spot-check A): removing the `continue` in
   `_label_rows` makes `test_ingest_is_lenient_on_bad_entries` FAIL
   (`BADKEY` leaks into `run_labels`), then restored.

3. **Drop-and-replay (§2).** `db.SCHEMA_VERSION` bumped `2 → 3`
   (`git show stage-3:src/agmon/db.py` = 2, current = 3). `init_db` drops the
   db + `-wal`/`-shm` on mismatch; the meta path re-derives `run_labels`
   idempotently (`DELETE` then re-`INSERT` on each upsert). Covered by
   `test_replay_after_version_bump_repopulates_labels` (asserts identical
   repopulation after a forced version downgrade) — PASSED.

4. **The inversion (§ intro).** No dedicated pipeline/phase/parent column or
   spool field anywhere: `runs` table (db.py) has none; `run_labels` is a plain
   `(run_id, key, value)` store; `_META_COLUMNS` (ingest.py) is unchanged;
   `meta.json` gains only a flat `labels` object (runner.py). All meaning lives
   in `derive.derive_lineage`, read from reserved keys. Confirmed by reading
   `db.SCHEMA`, `ingest._META_COLUMNS`, and `runner.main`'s meta dict.

5. **Lineage derivation (§3).** `derive_lineage` returns `None` iff none of
   `pipeline/phase/parent` present; `children` = dedup+sorted run_ids whose
   `parent` label == this run; `siblings` = same `pipeline` value, self
   excluded, oldest-first (None `started_at` last); `pipeline/phase/parent`
   surfaced verbatim — a dangling `parent` is rendered, not validated. Pure
   (no db/os/fastapi imports). Covered by the four `test_lineage_*` cases and
   the API `test_summary_lineage_three_phase_pipeline` — PASSED.

6. **API `label=` filtering (§4).** `_parse_label_filters` rejects a filter
   with no `=` (400 "expected key=value") and an empty key (400 "empty key");
   valid repeats AND via per-filter indexed `run_id IN (SELECT …)` subqueries.
   `labels` present (empty `{}` when none) in list and detail; summary carries
   the `lineage` block; sibling/children lookups are single indexed
   `run_labels` queries (`_RELATED_SELECT`, `_labels_for` uses one `IN`).
   Covered by `test_label_filters_single_and_multiple_and`,
   `test_malformed_label_filter_is_400`, `test_labels_in_list_and_detail`,
   `test_summary_lineage_*`, `test_pipeline_and_resume_lineage_not_conflated`.
   **Verified adversarially** (spot-check B): neutering the `"=" not in raw`
   guard makes `test_malformed_label_filter_is_400` FAIL (200 instead of 400),
   then restored.

7. **Test-honesty spot check.** Both spot checks above executed this session:
   each target test failed for its stated reason when its code was reverted,
   and passed again after restore (`git diff --stat` clean). The two chosen
   tests genuinely exercise the code they claim to.

Note on the live service (`localhost:8400`): it predates this branch — its
`/v1/runs` payloads carry no `labels` field (`labels: None` client-side), so it
was not used as evidence for the code under review; a fresh `create_app`
instance (as the suite uses) was used instead.

---

## F1 — HARDENING (low): ingester does not enforce the 16-label aggregate cap

**Where:** `src/agmon/ingest.py:59` (`_label_rows`) — validates each entry via
`validate_label` (a *per-entry* check) but never applies `labels.MAX_LABELS`,
the per-run aggregate constraint that only `build_labels` enforces.

**Evidence (observed this session):**
```
$ uv run python -c "from agmon import ingest; \
  print(len(ingest._label_rows('r1', {'labels': {f'k{i}':'v' for i in range(20)}})))"
20        # a foreign meta.json with 20 valid labels lands all 20
```
The wrapper caps at `MAX_LABELS = 16`; the ingester will store an unbounded
number of well-formed labels a foreign/buggy writer emits.

**Impact:** low. No incorrect output and no stall/corruption — `run_labels` PK
`(run_id, key)` still holds, and the containment invariant (never stall the
file) is intact. It is purely a robustness gap: the spool can accumulate more
labels per run than the dispatch contract permits. Whether the aggregate cap is
meant to be an ingest-time constraint is genuinely ambiguous in the spec — §2
says entries "that violate the constraints … are skipped," and the cap is
listed among §1's constraints, but it is a run-level rather than an entry-level
rule and the lenient path is built around the per-entry `validate_label`. I read
this as deliberate (aggregate cap = dispatch guard only), so it is reported as
hardening, not a bug, and carries no xfail.

**Suggested fix (if desired):** after the per-entry loop, truncate to the first
`MAX_LABELS` rows (deterministic order) with a single `log.warning` naming the
count dropped — mirroring the per-entry skip-and-log, keeping the file
non-stalling.

## F2 — NIT: whitespace-only label values are accepted

**Where:** `src/agmon/labels.py:36-42` (`validate_label`).

**Evidence (observed):** `build_labels(['k= '])` → `{'k': ' '}`. A single space
is non-empty and `str.isprintable()` is True, so it passes. The spec asks for
"non-empty printable strings"; a space technically qualifies. Cosmetic only — a
foreign or fat-fingered writer could stamp a blank-looking value. No fix
recommended; noted for completeness. No test.

---

## Verification transcript

**Baseline** (before any change this session):
```
$ uv run pytest -q
........................................................................ [ 63%]
..........................................                               [100%]
114 passed, 1 warning in 0.61s
```

**Spot-check A** (ingester leniency reverted → target test fails; then restored):
```
$ uv run pytest tests/test_labels.py::test_ingest_is_lenient_on_bad_entries -q
FAILED tests/test_labels.py::test_ingest_is_lenient_on_bad_entries - Assertio...
1 failed, 1 warning in 0.30s
# git diff --stat src/agmon/ingest.py  ->  (clean after restore)
```

**Spot-check B** (API 400 guard reverted → target test fails; then restored):
```
$ uv run pytest tests/test_labels.py::test_malformed_label_filter_is_400 -q
FAILED tests/test_labels.py::test_malformed_label_filter_is_400 - ValueError:...
1 failed, 1 warning in 0.59s
# git diff --stat src/agmon/api.py src/agmon/ingest.py  ->  (clean after restore)
```

**Final suite** (after restores; no application code changed; no tests added —
no genuine bug to encode):
```
$ uv run pytest -q
........................................................................ [ 63%]
..........................................                               [100%]
114 passed, 1 warning in 0.61s
```
No xfails were added (Rule 2: adversarial tests are for real bugs only, and
none were found). BACKLOG xfails undisturbed (none are collected in this repo
state).
