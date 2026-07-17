# friction
- Multi-agent future: `agmon run` hardcodes `["claude", "-p", ...]`
  (runner.py) and resolves it via ambient PATH — which is exactly what
  broke on the box (fnm multishell dirs; see next entry). Enhancement:
  an agent registry in config (name -> absolute binary path + argv
  template), `agmon run --agent <name>`, agent stamped into meta. Kills
  the PATH-sensitivity class and is the natural seam for a second agent
  CLI. Caveat that makes this a spec, not a tweak: ingest/derive assume
  Claude stream-json event shape (result subtypes, tool_result error
  flags, session ids) — a second agent needs an output adapter or an
  agent-aware spool contract. The registry's argv templates are also the
  container on-ramp: a dockerized agent (official image exists:
  ghcr.io/anthropics/claude-code) is just an entry whose template is
  `docker run --rm -v {cwd}:/work ... image:tag -p {prompt}` — right
  boundary for unattended bypassPermissions pipeline runs, agmon stays
  docker-unaware. Costs to spec: per-project toolchain images, creds
  (API key, not mounted OAuth), uid mapping, pid-of-client semantics
  for `died` detection. (2026-07-16, operator musing after the PATH
  incident.)
- `agmon run` reports any Popen FileNotFoundError as "claude not found on
  PATH" (runner.py). Misleading in two real cases: (a) `claude` exists only
  as a shell alias/function (local-install alias in ~/.bashrc) — invisible
  to an argv-list Popen; (b) `claude` IS on PATH but is a dangling symlink
  or a script with a dead shebang interpreter — exec fails ENOENT and the
  message blames PATH. Enhancement: preflight with shutil.which and print
  the PATH searched; if which() finds it yet exec fails, say the target is
  broken instead. Hit 2026-07-16 diagnosing "works in my shell, fails via
  agmon run" on the box.
- ENHANCE: `summary.activity.last_thinking` — the stalled-vs-thinking
  sensor. Now that spools can carry thinking bodies (2026-07-17: the
  empty thinking blocks were claude's `display: omitted` default in
  headless mode; `agmon run --agmon-experimental-display-thinking` opts a
  dispatch into the undocumented claude flag pair, and tail/events render
  `thinking:` snippets — see README), the derive layer could surface the
  latest thinking text on
  the summary's `activity` block, distinguishing "quietly reasoning" from
  "stalled" during long silent stretches. Pure read-time derivation over
  events, no schema bump. Residual of the resolved empty-thinking
  INVESTIGATE entry (2026-07-11), which held the rationale: thinking is
  where an agent's confusion is legible — it turns agmon from "what did
  it do" into "why did it get stuck".
- `/v1/runs` items carry no progress field; the latest PROGRESS line lives
  only in `/v1/runs/{id}/summary` (`activity.progress`). A list client that
  wants a live progress column must fetch /summary per run — n+1, and it
  compounds once the client auto-refreshes (agmon.el will poll the list
  every ~10s from stage 3.el.4). agmon.el shows `prompt_preview` instead
  for now. Enhancement: surface the latest PROGRESS line on each
  `/v1/runs` row. If that means deriving it at ingest time, that is a
  derivation change → bump `db.SCHEMA_VERSION` (per CLAUDE.md).
- ENHANCE: `agmon ls` activity column shows last_event_type, not
  tool+target (stage-3 DECISIONS D1; review F2, discarded correctly).
  Right fix is server-side: the /v1/runs list gains a last_tool /
  last_target per row (one aggregate query, consistent with the
  event_count subquery pattern) — not N+1 client summary calls.
- BIG IDEA (institutionalize the dev loop as a first-class lineage):
  this repo is built by a fixed pipeline — spec -> build -> adversarial
  review -> consolidate review — but today each phase is a separately,
  manually dispatched run, related only loosely. `session_id` captures
  resume chains (same conversation continued), NOT cross-phase links: a
  build run and its review run are different sessions entirely, so agmon
  sees them as unrelated. Institutionalize the pipeline: an orchestrator
  that runs the phases fully agentically end to end, and agmon tracking
  the whole thing as one coherent *lineage* rather than N disconnected
  runs. Two halves, keep them separate:
  1. Represent/navigate (agmon's job): a pipeline/lineage id + a phase
     label (spec|build|review|consolidate) stamped into run metadata by
     the orchestrator — `session_id` won't carry it. That is a spool
     metadata + ingest-derivation change → bump `db.SCHEMA_VERSION` (per
     CLAUDE.md). The detail-buffer lineage section then badges phases and
     shows the sibling phases of the same pipeline; distinct from the
     resume-chain lineage now in agmon.el (this is an intentional phase
     DAG, not a resume).
  2. Orchestrate/dispatch (control-plane): actually launching the phase
     runs and gating between them (build only after spec, review after
     build, etc.). This is out of scope for the read-only agmon.el mode
     (spec fences control-plane to stage 4+ / a separate roadmap item) —
     but it is the half that makes the loop "run fully agentically."
  Follow-on (surface deleted phase artifacts): a phase's key output can
  be intentionally removed from the repo — REVIEW.md is written by the
  review phase and consumed+deleted by consolidate (per CLAUDE.md). But
  the content is NOT gone: it lives in the review run's event stream (the
  Write/Edit tool calls in the spool). agmon could reconstruct and show
  the historical REVIEW.md from events as part of the chain view, so you
  can follow the whole path — spec -> build -> the actual review text ->
  consolidation — even for artifacts git no longer has. Read-only, pure
  derivation from events; fits the represent half. Natural extension:
  human-reviewed chains, where each phase's surfaced artifact gets a
  person's sign-off recorded on the lineage.
- ENHANCE (agmon.el): copy a tabulated-list screen as TSV. The column
  header is a `header-line`, not buffer text, so it can't be marked or
  killed — you can copy the data rows but never the titles, which makes
  pasting the table into a doc/spreadsheet lossy. Add a buffer command
  (mnemonic `w`, "copy") that yanks the header plus the visible rows,
  tab-separated, to the kill-ring. Pure formatting over
  `tabulated-list-format` (the column names) and the printed rows, so it
  is ERT-testable; applies to every such screen (run list, cost rollup).
  Surfaced 2026-07-13 while adding the new run-list columns.
- ENHANCE (cli): no headerless output for scripting a single field.
  `agmon show --fields run.run_id --plain` still prints the header line
  ("run.run_id" then the value), so you can't do
  `ID=$(agmon show <run> --fields run.run_id --plain)` without stripping
  it. Cause: `render.to_tsv` unconditionally prepends the header row
  (render.py:225) and every `--plain`/piped path goes through it
  (`_emit_rows`, cli.py:66; `cmd_show`, cli.py:134). Options: a
  `--no-header` flag, or suppress the header when `--fields` projects a
  single column, or drop it whenever `--plain` is explicit (TSV-with-
  header still the default for spreadsheet paste). Today's workaround is
  `--json` piped to jq, but a bare value is the natural ask. Surfaced
  2026-07-13.
- ENHANCE (cli): `agmon show` needs a pager, and there is no way to keep
  colour through a pipe. Piped stdout → `_resolve_tty` false → the rich
  Console is built `force_terminal=False, no_color=True` (cli.py:370), so
  `| less -R` gets zero ANSI to preserve; rich's FORCE_COLOR env is also
  dead because `force_terminal` is passed explicitly. Two standard fixes,
  complementary: (1) a `--color=always|auto|never` flag (grep/git
  convention) feeding the Console construction, so `--color=always |
  less -R` works; (2) git-style auto-pager on a TTY — when output is
  taller than the screen, spawn `$PAGER` with `LESS=-RFX` and force
  colour into it (rich `console.pager(styles=True)` or a subprocess).
  Interim workaround: `script -qec "agmon show <id>" /dev/null | less -R`
  (fake pty). Surfaced 2026-07-15.
- An auth-failed run is opaque in every list view: when claude can't see
  credentials (here: `--bare` severed OAuth — next entry) it replies "Not
  logged in · Please run /login", stamps the result `subtype:"success"`,
  and exits 1. The wrapper's exit-code guard correctly lands status=error,
  but issue_count stays 0 (`is_error` is false: subtype success and no
  `is_error:true` — the mirror image of the documented lying-529 case,
  minus even the flag), cost is $0.00, and the only clue is `result_text`
  behind the summary endpoint. So `ls` shows an error run, zero issues,
  zero cost, and you open the run to learn why. Enhancement candidate: a
  derive-level issue (or status badge) for result-subtype-success +
  nonzero exit — "stream says fine, process says no" is exactly the
  divergence worth one glanceable line. Hit 2026-07-17 live-testing spec
  007 (the run also proved a nice 007 property: even an unauthenticated
  run emits an init event, so `model` still observes).
- `agmon run --bare` silently severs subscription auth: claude 2.1.212's
  `--bare` skips credential loading along with the hooks/skills/CLAUDE.md
  discovery it's meant to skip — the identical `claude -p` over the same
  ssh env answers fine without `--bare` and says "Not logged in" with it,
  credentials file present and fresh. So every `--bare` dispatch on a
  /login-authenticated box fails, and it fails in the opaque shape of the
  previous entry (two runs burned before the flag was suspected; isolated
  2026-07-17 by A/B-ing `claude -p` ±`--bare` directly). Candidates:
  caveat on the `--bare` help text, a wrapper preflight (bare + no
  ANTHROPIC_API_KEY → warn), or report it upstream — bare skipping
  *identity* looks like a claude CLI bug, not a feature.
