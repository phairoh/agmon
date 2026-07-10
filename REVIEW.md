# Review — stage-3 CLI (diff since tag `stage-2`)

Scope: everything after `stage-2` (the `stage-3` tag is the pre-review baseline
and is ignored as a baseline per FOCUS). Primary spec: `specs/003-cli.md`.
Baseline suite: **79 passed** (green) — see Verification transcript.

Findings below are ordered by severity. Two genuine bugs carry `xfail(strict)`
tests in `tests/test_review_findings.py`; the spec-conflict changes no code.

The priority areas that I probed and found **correct** (no finding):
substring id resolution incl. exact-match precedence and shared-date-prefix
ambiguity (`client.resolve`); the tail cursor advancing via `next_after` with
no skip/double across batch boundaries (server always returns `next_after`, and
`--last N` maths `num_events - N` is sound because `seq` is per-run and gapless
from 1 — `ingest.py:201`); tail exit codes for finished/error/interrupted/died
(`_TAIL_EXIT`, including `interrupted→1`); one-level dot flattening and bare
`--fields`; the `agmon run` port (byte-for-byte faithful to
`~/.local/bin/agmon-run` apart from the `build_parser` extraction); and the
server changes (`result_text` presence/nullability, `/healthz` unversioned with
`/v1/healthz` gone).

---

## F1 — BUG (low): `agmon tail --plain` is a dead flag; colour still emitted

**Where:** `src/agmon/cli.py:272` (flag defined, help "no color") vs `cmd_tail`
(`cli.py:157-198`, never reads `args.plain`). The console's colour is fixed once
in `main` from `tty_resolved` only (`cli.py:346`).

**Evidence (executed):**
```
$ uv run python -c "... cli.main(['tail','run1','--plain'], ..., tty=True ...) ..."
exit 0
contains ANSI color? True
'\x1b[1;36mPROGRESS: working\x1b[0m\n\x1b[32mresult: success · $0.04 · 1 turns\x1b[0m\n...'
```
On a TTY, `--plain` leaves the ANSI SGR codes (`\x1b[1;36m`, `\x1b[32m`) intact.

**Impact:** low. A documented flag (`--help` shows "no color") has zero effect;
a user who runs `agmon tail <id> --plain` on a terminal still gets colour. The
spec does not require a tail `--plain`, so this is an implementer-added flag that
is non-functional, not a spec violation. No data/exit-code consequence.

**Suggested fix (not applied):** have `cmd_tail` honour `args.plain` — e.g. build
its `Console` (or pass `no_color`) from `tty and not args.plain`, mirroring how
`_emit_rows` gates the table path. Alternatively drop the flag.

**Test:** `tests/test_review_findings.py::test_tail_plain_suppresses_color_on_tty`
(XFAIL, reason F1).

---

## F2 — SPEC-CONFLICT (low): `ls` "activity" column shows event *type*, not "tool + truncated target"

**Where:** `src/agmon/render.py:303` — `ls_rows` fills the activity cell with
`it.get("last_event_type")`.

Spec `specs/003-cli.md:54-59` (ls columns): *"last activity (tool + truncated
target)"*. The rendered column instead shows the raw event type.

**Evidence (executed):**
```
$ AGMON_URL=http://localhost:8400 uv run agmon ls -n 3
id      status    started  dur     project  activity    issues  cost
26274c  running   2m ago   2m08s   agmon    assistant   0       -
6cdc57  finished  4h ago   20m38s  agmon    result      3       $8.10
ae7c1d  finished  11h ago  4m54s   agmon    result      1       $1.44
```
The activity column reads `assistant` / `result` (the event type), never a tool
name + target.

**Impact:** low, and **this is a deliberate, recorded deviation** — the stage-3
run's DECISIONS (surfaced verbatim by `agmon tail 6cdc57`, the run's own final
message) states: *"the latter would need one summary call per row, and server
enrichment was out of scope. `agmon show` gives the full last-tool detail."* The
list endpoint (`/v1/runs`) exposes `last_event_type` but no per-run tool/target,
so rendering the spec's column would cost one `/summary` call per row. Reporting
it per FOCUS/rule 4; not a hidden regression.

**Suggested resolution (choose one, change nothing now):** (a) accept the
deviation and align the spec/README to say "last event type"; or (b) enrich the
list endpoint with a `last_tool`/`last_target` pair (single-writer ingest can
carry it cheaply) and render that. No `xfail` test: this is a spec conflict, and
the behaviour is an accepted judgment call.

---

## F3 — BUG (low): TSV output is not row-safe — a cell with `\n`/`\t` splits one row into many lines

**Where:** `src/agmon/render.py:215-220` (`to_tsv`) — cells are joined with tabs
and rows with newlines, with no escaping of tab/newline inside a value.

`to_tsv`'s own docstring promises *"a header line then one tab-joined row each"*.
`--fields` projects **raw** values (`project_rows` → `_raw_scalar`, `render.py:259`),
and multiline fields (prompt, `result_text`) are ordinary targets.

**Evidence (executed):**
```
$ AGMON_URL=http://localhost:8400 uv run agmon show 6cdc57 --fields run.prompt | cat -A | head
run.prompt$
# Task: agmon stage 3 M-bM-^@M-^T the CLI$
$
agmon's API is complete but uncurated; querying it means hand-writing curl$
...
```
The single projected row spans dozens of physical lines. A line-oriented
consumer (`agmon ... --fields x | cut -f1`, `while read`) sees each wrapped line
as a separate record — silent corruption of the piped/scriptable contract the
`--plain`/TSV path exists to serve.

**Impact:** low. Only affects `--fields`/piped output whose projected values
contain a tab or newline; default columns are pre-formatted scalars and are
unaffected. But when it bites, it is a silent malformed-output bug, not an error.

**Suggested fix (not applied):** in `to_tsv` (or `_cell_text`) replace `\t`→space
and `\n`/`\r`→space (or `\\n`) per cell before joining, so every row is exactly
one physical line. Applies equally to the default-column path for safety.

**Test:** `tests/test_review_findings.py::test_tsv_row_stays_one_line_with_newline_cell`
(XFAIL, reason F3).

---

## Verification transcript

Baseline (before any review changes):
```
$ uv run pytest -q
........................................................................ [ 91%]
.......                                                                  [100%]
79 passed, 1 warning in 0.49s
```

New adversarial tests alone:
```
$ uv run pytest tests/test_review_findings.py -v
tests/test_review_findings.py::test_tail_plain_suppresses_color_on_tty XFAIL [ 50%]
tests/test_review_findings.py::test_tsv_row_stays_one_line_with_newline_cell XFAIL [100%]
2 xfailed in 0.29s
```

Full suite after adding the tests (tail):
```
$ uv run pytest -q
xx.......                                                                [100%]
79 passed, 2 xfailed, 1 warning in 0.52s
```
79 pre-existing tests still pass; the 2 new findings XFAIL (strict). No BACKLOG
xfails were touched. No application code changed.
