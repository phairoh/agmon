# Adversarial review — stage 4b artifact surfacing (specs/006-artifacts.md)

Baseline suite green before review: **164 passed** (transcript at end).
Reviewed the diff `stage-3..HEAD` for the artifact layer, focusing on the
FOCUS priorities: section parser, reconstruction (against **real**
`~/agent-runs` payloads), catalog contract, name resolution, and model harvest.

Verified correct against real data:
- **Reconstruction is byte-exact.** Reconstructing every still-on-disk
  reconstructable file from the real spool matched disk bytes exactly,
  including multi-op sequences (`test_artifacts.py` in a prior run: 6 ops,
  26 931 bytes; `artifacts.py`: 2 ops, 9 161 bytes). `replace_all` and
  multi-byte content survive.
- **Model harvest / replay backfill.** A drop-and-replay over the real
  historical run `20260708T174951-67a5e8` backfilled
  `model = claude-opus-4-8[1m]` from its init event. Requested value is never
  used as fallback; `SCHEMA_VERSION` bumped exactly once for §6 (3→4; the 2→3
  bump belongs to the separate labels task, commit `63ca44b`).
- **Section parser** last-occurrence-wins, heading/colon variants, mid-prose
  non-triggering; **catalog** unavailable-with-reasons; **name resolution**
  precedence and 400-with-candidates — all confirmed via the existing suite
  and direct probes.

---

## F1 — BUG (med): rejected (errored) Write/Edit ops surfaced as reconstructed content

**Where:** `src/agmon/artifacts.py:106` (`_iter_file_ops`), consumed by
`reconstruct_file` (`:190`), `derive_file_artifacts` (`:168`), and
`build_catalog` (`:229`).

**What:** `_iter_file_ops` collects *every* Write/Edit `tool_use` block and is
blind to whether the matching `tool_result` reported `is_error: True`. A write
or edit the harness **rejected** (permission denied, "File has not been read
yet", "String … not unique") never touched disk, yet its content is folded into
the reconstruction and its path is listed as an available, reconstructable file
artifact. This contradicts the spec's premise (§1/§3: an artifact is a file the
run *wrote*, "recoverable forever from the spool") and the README's
"Reconstruction limitations" section, which caveats disk-vs-spool state but not
rejected ops.

**Evidence (real spool, `~/agent-runs/20260708T220925-daadb1`):** a Write to
`/tmp/check_json.py` was permission-denied. Reconstructing with all ops vs. only
non-errored ops diverges:

```
$ uv run python  # compare reconstruction with vs. without errored ops, all real runs
DIVERGES: 20260708T220925-daadb1.jsonl /tmp/check_json.py full= 417 clean= 34

$ # trace that file's ops:
OP Write id= toolu_01CbrpMLFtsy35ZKSi7oEhhG content= 'import json ...'
   RESULT is_error= True "Claude requested permissions to write to /tmp/check_json.py, but you haven't gra…"
```

Reconstruction returns 417 bytes of content that never existed on disk (the
rejected write), rather than the 34 bytes that reflect the successful ops.

**Impact:** silently-wrong artifact content. A run whose only write to a path
was rejected still lists that path as `available: true`, `reconstructable:
true`, and serves phantom content through both `--get` and the content
endpoint. Low frequency (rejected ops are relatively rare, and a later
successful write to the same path masks the divergence), but when it bites it
is a correctness error the catalog presents as truth. The "not unique" edit
rejection is the worst sub-case: the real tool makes *no* change, but
`reconstruct_file`'s `str.replace(old, new, 1)` changes the first occurrence.

**Suggested fix (not applied):** correlate `tool_use` ids with their
`tool_result` `is_error` flag (as `derive.derive_issues` already does) and skip
ops whose result errored when building ops-by-path. A path whose only ops were
rejected should then not appear as a reconstructable artifact (→ `ArtifactUnknown`
on content fetch). Note the layer is pure and currently takes only `events`;
the errored ids are already present in those events (`tool_result` blocks), so
no new inputs are needed. `_content_to_text`/id-correlation logic can be lifted
from `derive`.

**xfail test:** `tests/test_artifacts.py::test_rejected_write_not_surfaced_as_content`
(reason F1) — builds the real permission-denied shape and asserts the phantom
content is not served. Reports XFAIL against current code.

---

## F2 — HARDENING: exact basename match can be shadowed into ambiguity by a substring sibling

**Where:** `src/agmon/artifacts.py:294` (`resolve_content` step 3):
`candidates = [p for p in ops_by_path if _basename(p) == name or name in p]`.

**What:** basename-equality and substring-containment share one tier joined by
`or`, so an exact basename hit does not win over an incidental substring hit.

**Evidence (probe):**
```
$ uv run python -c "... resolve_content(None,None,ev,'REVIEW.md') ..."
basename over-match raised: ArtifactAmbiguous 'REVIEW.md' matches 2 files:
    /wt/OLD_REVIEW.md.bak, /wt/REVIEW.md
```
`REVIEW.md` is an exact basename of `/wt/REVIEW.md` but also a substring of
`/wt/OLD_REVIEW.md.bak`, so the canonical `--get REVIEW.md` errors 400 instead
of resolving the exact-basename file.

**Impact:** the canonical use "must work exactly as written" (§5) breaks in the
presence of a substring-sibling path. The spec lists "basename **or** substring"
as one tier, so this is defensible as-written, hence HARDENING not BUG.

**Suggested fix (not applied):** try exact-basename uniqueness first; fall back
to substring only if no basename matches. No test (per rules, hardening gets no
test).

---

## F3 — HARDENING: CRLF-terminated marker lines are not recognized

**Where:** `src/agmon/artifacts.py:34` (`_ANY_MARKER_LINE`) and `:72`
(`_marker_line_re`): both end `…[ \t]*$`, and `[ \t]*` does not consume a
trailing `\r`.

**Evidence (probe):**
```
$ uv run python -c "... derive_section('intro\r\n## DECISIONS\r\nbody line\r\n','DECISIONS') ..."
CRLF section: None
```
A `DECISIONS` heading on a CRLF line is treated as absent.

**Impact:** a foreign run whose composed prompt or result uses CRLF line endings
would have its sections silently missed. Real agmon result/prompt text is LF, so
this is not observed in the spool — hardening, not a live bug. No test.

**Suggested fix (not applied):** allow an optional `\r` before `$`, or compile
with a `\r?` tail.

---

## F4 — NIT: no drop-and-replay backfill test over a historical fixture

**Where:** `tests/test_artifacts_api.py` model-harvest section (`:234`).

**What:** spec Tests §7 asks for "replay over a historical fixture spool
backfills". The suite covers init-population, null-without-init,
requested-never-fallback, and rescan-does-not-clobber, but not a
drop-then-replay backfill. I verified the behavior works (real run
`20260708T174951-67a5e8` backfilled after `_drop_db` + re-scan), so this is a
coverage gap, not a defect. No test added (behavior is correct; a fixture-based
test would be an enhancement).

---

## Verification transcript

**Baseline (before any change):**
```
$ uv run pytest -q
........................................................................ [ 43%]
........................................................................ [ 87%]
....................                                                     [100%]
164 passed, 1 warning in 0.84s
```

**TDD honesty spot-check (two features reverted, confirmed red, restored):**
```
# 1. section last-occurrence-wins: matches[-1] -> matches[0]
$ uv run pytest tests/test_artifacts.py::test_section_last_occurrence_wins -q
E  AssertionError: assert 'first' == 'second'
1 failed

# 2. model harvest disabled: init_model = None
$ uv run pytest tests/test_artifacts_api.py::test_model_harvested_from_init_event -q
E  AssertionError: assert None == 'claude-opus-4-8[1m]'
1 failed
# both reverts undone; `git diff` of src/ empty afterward
```

**Final suite (after adding F1 xfail):**
```
$ uv run pytest -q -rxX
.............x.......................................................... [ 43%]
........................................................................ [ 87%]
.....................                                                    [100%]
XFAIL tests/test_artifacts.py::test_rejected_write_not_surfaced_as_content - F1
164 passed, 1 xfailed, 1 warning in 0.85s
```
