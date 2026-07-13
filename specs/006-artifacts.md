# Task: agmon stage 4b — artifact surfacing

Runs produce durable artifacts that git deliberately forgets: REVIEW.md
is consumed and deleted, DECISIONS lives only in a final message, the
fully-composed prompt (including appended OVERRIDES/FOCUS sections)
exists only in meta. The spool holds all of it. This stage gives every
run a queryable **artifact catalog** — named things you can list and
fetch without knowing file paths or parsing prose yourself. Pure
derivation over already-ingested data; §6 is the one exception (a small
ingest change, no new columns).

Repo conventions in CLAUDE.md apply, including test-first feature work.

## 1. The artifact model

Every run exposes a set of named artifacts in two families:

**Dispatch artifacts** — derived from the run record itself:

| name                | source                            | present when            |
|---------------------|-----------------------------------|-------------------------|
| `prompt`            | the stored composed prompt        | always                  |
| `prompt.focus`      | FOCUS section parsed from prompt  | marker present          |
| `prompt.overrides`  | OVERRIDES section from prompt     | marker present          |
| `result`            | result_text                       | run produced a result   |
| `result.decisions`  | DECISIONS section from result     | marker present          |

**File artifacts** — files the run wrote, reconstructed from its
Write/Edit tool events (§3). Named by their path. REVIEW.md is the
motivating case: written by a review run, deleted by consolidation,
recoverable forever from the spool.

The catalog is the contract: a user should find prompt, overrides,
result, decisions, and REVIEW.md by name from `agmon artifacts`,
without knowing where any of them physically lived.

## 2. Section extraction (one parser, three conventions)

New pure function `derive_section(text, marker)` → the text from the
**last** line-anchored occurrence of MARKER to the next marker line or
end of text, heading line excluded; null when absent or text is null.
A marker line is the bare ALL-CAPS word (`DECISIONS`, `FOCUS`,
`OVERRIDES`), optionally prefixed by markdown heading syntax
(`#`–`###`) and optionally suffixed with `:` — anchored at line start,
so the word mid-prose never triggers. This single parser feeds
`result.decisions`, `prompt.focus`, and `prompt.overrides`. Document
the convention in the README (it is how foreign runs participate).

## 3. File reconstruction from events

A run's file-writing tool calls (Write, Edit, and any multi-edit
variants — discover the real payload shapes from `~/agent-runs`, do
not guess) contain everything needed to reconstruct final content.

Pure functions (in `derive.py` or a sibling `artifacts.py`, same purity
rule — no sqlite3/fastapi/os imports):

- `derive_file_artifacts(events)` → per written path:
  `{"path", "ops", "first_op" ("write"|"edit"), "last_seq",
  "reconstructable" (bool), "bytes" (int|null)}`. Reconstructable means
  the op sequence starts from known-full content (a Write); edit-only
  files are listed but honestly marked — the spool knows the patches,
  not the base.
- `reconstruct_file(events, path)` → final text content, ops applied in
  seq order against the evolving reconstruction (Write replaces; edits
  apply their old→new replacement honoring any replace-all semantics
  the real payloads carry). Specific errors for unknown path and
  non-reconstructable.

If the run later deleted the file (rm in Bash), reconstruction still
returns the last written content — that is the point — with no claim
about current disk state.

## 4. API

- `GET /v1/runs/{id}/artifacts` → `{"artifacts": [...]}`, both families
  in one list. Each item: `name` (`prompt`, `result.decisions`, or the
  file's path), `kind` (`"dispatch"` | `"section"` | `"file"`),
  `available` (bool — a section whose marker is absent, or a
  non-reconstructable file, lists as unavailable with a `reason`),
  `bytes` when available, and the §3 fields for files. Dispatch
  artifacts always list, available or not — the catalog shows what
  *could* exist, so absence is visible rather than silent.
- `GET /v1/runs/{id}/artifacts/content?name=<name>` → the content,
  `text/plain; charset=utf-8`. Name resolution, in order: exact
  dispatch-artifact name; exact file path; unique file **basename or
  substring** (the run-id resolution ergonomic, applied to paths —
  `name=REVIEW.md` must fetch the review without the worktree path).
  404 unknown, 409 + JSON reason for listed-but-unavailable, 400 with
  candidates for an ambiguous fragment.
- Summary gains `"decisions": <string|null>` alongside `result_text`.

One run's events per request, as established; no caching.

## 5. CLI

- `agmon artifacts [id]` — table: name, kind, available, size.
  Substring id resolution and latest-run default as everywhere.
- `agmon artifacts [id] --get NAME` — content to stdout raw (pipeable
  to files and diff); resolution per §4; errors to stderr, exit 1.
  `agmon artifacts --get REVIEW.md` and `--get prompt.overrides` are
  the canonical uses and must work exactly as written.
- `agmon show` gains a Decisions section (rendered like Result,
  placed before it) when present.

The full composed prompt is already stored and served; the `prompt`
artifact surfaces it through this same interface. If you find the
prompt truncated anywhere in storage or transport, that is a BUG to
report in DECISIONS, not silently fix.

## 6. Model harvest (the one ingest change)

`runs.model` has been null for the project's entire history: the
wrapper records the `--model` *argument* (rarely passed), never the
model that actually served. The stream knows: the init system event
carries the resolved model identity. Fix at the ingest layer so history
backfills on replay:

- Ingest: derive `runs.model` from the run's init event when present;
  null otherwise. `model` now means **observed** — a run killed before
  init honestly stays null ("never observed"). Do not fall back to the
  requested value; intent is not observation.
- Wrapper: write the `--model` argument to meta as `model_requested`
  (additive spool-contract field; stop writing meta `model`). Requested
  intent stays retrievable via the run detail's meta passthrough — no
  new column.
- Bump `SCHEMA_VERSION` per the CLAUDE.md invariant (ingest-derivation
  change): drop-and-replay teaches every historical run which model
  served it.
- Surfacing needs nothing new — `model` is an existing column; verify
  it renders in run detail and `agmon show`, and document the
  observed-vs-requested semantics in the README.

## Tests (test-first, per conventions; lift tool-call fixtures from
real spool payloads)

1. Section parser: each marker with and without heading prefix and
   trailing colon; last occurrence wins; runs to next marker vs EOF;
   the marker word mid-prose does NOT trigger; null text → null.
2. Catalog: a run with prompt+overrides+result+decisions+one written
   file lists all of them with correct kinds; a bare run lists
   dispatch artifacts with `available: false` and reasons.
3. Reconstruction: write-then-edits yields final content; interleaved
   files reconstruct independently; an edit applies against the
   *current* reconstructed state, not the original; replace-all
   semantics if real payloads support it; multi-byte content survives
   byte-exact; a canned REVIEW.md write-then-deleted sequence
   reconstructs the review text.
4. Not-reconstructable: edit-without-write listed false; content → 409.
5. Name resolution: dispatch name, exact path, unique basename,
   ambiguous fragment → 400 with candidates, unknown → 404.
6. API + summary decisions; CLI table and --get through the injected
   client (both canonical --get forms); show renders Decisions.
7. Model harvest: init-event model populates the column; no-init
   fixture stays null; requested value never used as fallback; replay
   over a historical fixture spool backfills; wrapper writes
   model_requested.

## Definition of done

- Full suite green; BACKLOG xfails undisturbed. Schema version bumped
  exactly once (for §6); **no new columns** — if you believe you need
  one, stop and ask. A pre-existing spool replays cleanly with model
  backfilled wherever an init event exists.
- README: the artifact model and catalog table, section-marker
  convention, endpoints and CLI with `--get REVIEW.md` as the shown
  example, reconstruction limitations, model semantics.
- All work committed, tree clean.

Out of scope: cross-run file state or "file at time T" time-travel,
binary content, artifacts from non-spooled interactive sessions,
Notebook-specific tools unless present in the real spool, editing or
writing anything through this API (read-only, forever), and agmon.el
consumption (the operator's attended track).
