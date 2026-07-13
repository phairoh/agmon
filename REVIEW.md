# Review — stage 4b, artifact surfacing (specs/006-artifacts.md)

Subject: the diff `stage-3..HEAD`, scoped by FOCUS to spec 006 (artifacts +
model harvest). The diff also carries stage-4a (labels/lineage); that is out of
FOCUS and reviewed only where it touches artifacts.

**Baseline: green — `172 passed`** (see Verification transcript). BACKLOG xfails
undisturbed. No application code was changed by this review; the two temporary
reverts in the honesty spot-check were restored and the suite re-run green.

Overall the stage is in good shape. Section parsing, catalog assembly, name
resolution, reconstruction byte-exactness, and the model harvest all behave as
specified against **real** `~/agent-runs` payloads (all reconstructable files
across 15 real spool runs reconstruct with byte counts matching the declared
`bytes`; every real `system/init` event carries `model` at top level; the §6
`SCHEMA_VERSION` bump is exactly once, 3→4, distinct from the 2→3 labels bump).
Two findings below; neither is an unambiguous contract violation, so neither
carries an `xfail` (rule 2). Both are reproduced with real or minimal payloads.

---

## F1 — SPEC-CONFLICT (med): section boundary matches *any* ALL-CAPS line, not just the three named markers

**Where:** `src/agmon/derive.py:242` (`_ANY_MARKER_LINE_RE`) used as the
"next marker" terminator in `derive_section` (`derive.py:250-269`).

**The conflict.** The spec (§2) and README both define the marker set by
enumeration:

- spec §2: "A marker line is the bare ALL-CAPS word (`DECISIONS`, `FOCUS`,
  `OVERRIDES`), optionally prefixed by markdown heading syntax…"
- README:1 "a line consisting of just the bare ALL-CAPS marker word
  (`DECISIONS`, `FOCUS`, `OVERRIDES`)".

But the terminator regex is `^(?:#{1,3}\s*)?[A-Z][A-Z0-9_]*:?[ \t]*$` — it treats
**any** bare ALL-CAPS token as a section boundary. So a section body that
contains an unrelated bare ALL-CAPS line (`IMPORTANT`, `NOTE`, `TODO`,
`UNVERIFIED`, `WARNING`, …) is silently truncated at that line. The
implementation's own docstring documents the broad reading ("to the next such
marker line (any marker, not just this one)"), so this is a deliberate-but-
undocumented widening of the enumerated set, not an accident — hence
SPEC-CONFLICT rather than BUG.

**Evidence (observed):**

```
$ uv run python -c '
import sys; sys.path.insert(0,"src")
from agmon import derive
t="OVERRIDES\nkeep the old config\nIMPORTANT\ndo not skip this line\n"
print("derive_section(OVERRIDES) =>", repr(derive.derive_section(t,"OVERRIDES")))'
derive_section(OVERRIDES) => 'keep the old config'
```

The `OVERRIDES` body silently loses `do not skip this line` because the bare
`IMPORTANT` line is treated as a marker. The same truncation would hit
`prompt.focus`, `prompt.overrides`, and `result.decisions` — the primary
artifacts this stage exists to surface.

**Blast radius in practice is currently low.** In the real spool, FOCUS/OVERRIDES
sections sit at the end of their source text and run to EOF, and DECISIONS bodies
are markdown bullet lists — none contain a bare ALL-CAPS line, so no real
extraction is truncated today (verified by extracting every FOCUS/OVERRIDES from
all real prompts and every DECISIONS from all real results; all complete). The
risk is latent: a foreign run or a future DECISIONS writeup with a bare
`IMPORTANT`/`UNVERIFIED` line loses content silently.

**Impact:** silent content loss in the exact artifacts the catalog promises to
surface faithfully; worst for "foreign runs participate" (README), which is the
stated reason the convention exists.

**Suggested resolution (not applied):** restrict the terminator to the three
reserved words — `^(?:#{1,3}\s*)?(?:DECISIONS|FOCUS|OVERRIDES):?[ \t]*$` — matching
the enumerated set in spec + README. This narrows correctly, keeps adjacent
reserved sections (FOCUS→OVERRIDES) as boundaries, and breaks no existing test
(all boundary tests use reserved markers). Alternatively, if the broad reading is
intended, spec §2 and the README should say so explicitly ("any bare ALL-CAPS
line terminates the section"). No `xfail` written: an `xfail(strict)` here would
encode one side of a genuine documentation ambiguity as "the contract."

---

## F2 — HARDENING (low-med): a rejected Write lists as a reconstructable, available artifact

**Where:** `src/agmon/artifacts.py:55-77` (`_write_edit_ops`) and
`derive_file_artifacts`/`reconstruct_file` — reconstruction reads only the
assistant `tool_use` blocks and never consults the paired `tool_result`, so a
tool call the platform **rejected** (permission denied, "file not read yet",
non-unique `old_string`) is reconstructed as if it had taken effect.

**Evidence (real spool, observed).** Run `20260708T220925-daadb1` issued a single
`Write` to `/tmp/check_json.py` that was rejected ("Claude requested permissions
to write to /tmp/check_json.py, but you haven't granted it yet"), with no later
successful write. The file never existed on disk, yet:

```
CATALOG ENTRY: {"name": "/tmp/check_json.py", "kind": "file", "ops": 1,
  "first_op": "write", "last_seq": 113, "reconstructable": true,
  "available": true, "bytes": 417}
RECONSTRUCTED 417 bytes; file exists on disk? False
```

`agmon artifacts --get /tmp/check_json.py` would serve 417 bytes of content that
was never written anywhere. The same class covers a rejected non-unique `Edit`:
reconstruction replaces the first occurrence while the real tool applied nothing.

**Why HARDENING, not BUG.** The spec is deliberately syntactic: §3 defines
`reconstructable` as "the op sequence starts from known-full content (a Write)"
and frames reconstruction as intent-stream replay "with no claim about current
disk state" (the `rm`-after-write case is explicitly embraced). A rejected Write
is *syntactically* a Write, so `available: true` is literally spec-compliant. It
is nonetheless a real gotcha for the motivating "REVIEW.md recoverable forever"
promise if the write that produced it was ever rejected.

**Impact:** the catalog can advertise, and `--get` can serve, content for a file
that was never created — presented indistinguishably from a genuine artifact.

**Suggested fix (not applied):** correlate `tool_use` ids with their
`tool_result` blocks (the `is_error` signal is already available at ingest) and
skip ops whose result errored — or, more conservatively, keep serving but flag
such a file so a caller can tell "attempted" from "written." No test added
(hardening, per rule 2).

---

## Notes (no finding)

- **Model `[1m]` suffix** is stored verbatim (`claude-opus-4-8[1m]`). Correct —
  that is the observed identity from the init event; `model` means observed.
- **`**DECISIONS:**` (bold) is not recognized** as a marker — real run
  `20260713T172231`… wrote `**DECISIONS:**` and `derive_section` returns `None`.
  This is spec-correct (§2 allows only heading/colon affixes, not bold), noted
  only as an observation about foreign-run participation.
- **Live service (`:8400`) vintage verified:** `/v1/runs/{id}/artifacts` → 404,
  `model` null. Artifacts + model harvest are not deployed there (matches FOCUS
  note); no live behavior was cited for those features.

---

## Test-honesty spot check (rule 6)

Two features stubbed, each confirmed to fail for the stated reason, then restored:

1. **Section last-occurrence-wins.** Reverted `matches[-1]` → `matches[0]` in
   `derive.derive_section`:
   ```
   test_section_last_occurrence_wins FAILED
   AssertionError: assert 'first attempt' == 'final answer'
   ```
   Restored.

2. **Model harvest from init event.** Forced `model_observed = None` in
   `ingest._ingest_events`:
   ```
   test_model_populated_from_init_event FAILED  (assert None == 'claude-opus-4-8[1m]')
   test_model_backfills_on_replay      FAILED  (assert None == 'claude-opus-4-8[1m]')
   ```
   Restored. Post-restore suite re-run green.

---

## Verification transcript

Baseline (before any change):

```
$ uv run pytest -q
........................................................................ [ 41%]
........................................................................ [ 83%]
............................                                             [100%]
172 passed, 1 warning in 0.79s
```

Final (after restoring both spot-check reverts; this review adds no `xfail`
tests, so counts are unchanged):

```
$ uv run pytest -q
........................................................................ [ 41%]
........................................................................ [ 83%]
............................                                             [100%]
172 passed, 1 warning in 0.79s
```

Real-spool cross-checks run during review (observed, not asserted in-suite):
reconstruction byte-exactness held for every reconstructable file across all 15
real runs (0 errors); every real `system/init` event carries top-level `model`;
FOCUS/OVERRIDES/DECISIONS extraction from all real prompts/results is complete
(F1's truncation is latent, not currently triggered).
