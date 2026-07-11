# Task: agmon stage 3.el — the Emacs mode

agmon is a monitoring system for headless Claude Code runs: a wrapper
spools stream-json events to disk, a collector ingests them into SQLite,
and a FastAPI server exposes them over the tailnet. A CLI (stage 3)
already provides curated views. This stage adds the client the operator
will actually live in: an Emacs mode, `agmon.el`, hitting the same HTTP
API.

## How this session is different — read first

Unlike previous agmon stages, this is an INTERACTIVE pair session inside
the operator's own Emacs (via agent-shell). The operator is watching, and
will evaluate and use each piece as it lands. Two standing instructions
follow from that:

1. **You are a tutor as well as a builder.** The operator's Emacs Lisp
   knowledge is limited and learning it is an explicit goal of this
   stage. When you introduce a new concept (a derived mode, a face, a
   timer, a keymap), say briefly what it is, why it's the idiomatic
   tool here, and how to inspect it (`C-h f`, `C-h v`, `C-h k`). Prefer
   idiomatic-and-explained over maximally clever. Keep explanations
   tight — a few sentences at the moment of first use, not lectures.

2. **Stages end at checkpoints, and checkpoints belong to the human.**
   At each stage's checkpoint, stop. Tell the operator what to evaluate
   and what to try. Let them drive, gather their reactions, fix what
   irritates them, and do not begin the next stage without an explicit
   go-ahead. "It works" is not the acceptance criterion; "it feels
   right in my frame" is, and only the operator can judge that.

## Environment facts

- Emacs 30.2. Use modern facilities freely: `keymap-set`, `setopt`,
  `defvar-keymap`, `json-parse-buffer`, `seq`, `map`, `cl-lib`.
- HTTP via **plz.el** (install from GNU ELPA). All requests are GETs to
  a tailnet-internal server; no auth.
- The file lives in the agmon repo at `emacs/agmon.el` — a single file;
  no multi-file package ceremony at this size. The operator has a local
  clone on this laptop and loads it from there.
- The server is live and reachable. Base URL comes from a defcustom
  `agmon-url`, defaulting from `$AGMON_URL` if set, else
  `http://localhost:8400`.
- **Discover payload shapes by querying, not guessing.** Before
  rendering any endpoint, curl it (or fetch via plz) and read the real
  JSON. The API: `GET /v1/runs`, `GET /v1/runs/{id}`,
  `GET /v1/runs/{id}/summary` (includes `result_text`),
  `GET /v1/runs/{id}/events?after=<seq>&limit=<n>` (returns
  `next_after` for cursor polling), `GET /v1/stats/costs`,
  `GET /healthz`. The repo README documents all of them.

## Architecture invariants (hard, every stage)

- `;;; agmon.el --- ... -*- lexical-binding: t; -*-` and full package
  header/footer conventions from the start.
- Namespace discipline: `agmon-` public, `agmon--` private. Docstrings
  on every def. `M-x checkdoc` and byte-compilation clean at every
  stage boundary (run both in batch mode as a check:
  `emacs -Q --batch -f batch-byte-compile emacs/agmon.el`).
- **One request indirection.** Every HTTP call goes through a single
  function (e.g. `agmon--request`), so the transport can be stubbed in
  tests and swapped later. Nothing else in the file may call plz
  directly.
- **Pure render layer.** Functions that turn parsed JSON into
  `tabulated-list-entries`, propertized strings, or detail-buffer text
  take data in and return text/structures out — no network, no buffer
  mutation. This is what makes them ert-testable and keeps the future
  TUI-ish ambitions cheap.
- Pick one JSON representation at the parse boundary (recommend
  `json-parse-buffer` with `:object-type 'alist`), note it in a comment
  near `agmon--request`, and use it consistently.
- User-facing choices are defcustoms under a `defgroup agmon`; colors
  are named `defface`s, never literal color strings in code.
- Commit at each stage boundary at minimum. Work that is not committed
  does not exist.

## Dev workflow (establish in stage 1, use throughout)

The reload loop will dominate this session; set it up before it bites:

- Re-evaluate the whole file with `M-x eval-buffer`; re-evaluate a
  single form with `C-M-x`. Teach the operator the crucial asymmetry:
  `C-M-x` on a `defvar`/`defface`/`defcustom` **forces**
  re-initialization; `eval-buffer` does not. Changed a default? `C-M-x`
  it.
- Stale state is the classic trap: timers from a previous evaluation
  keep firing old callbacks, and renamed functions leave their old
  definitions loaded until restart. Build `agmon-dev-reset` in stage 1:
  cancel every agmon timer, kill every agmon buffer, and say what it
  did. It is a dev tool; mark it clearly as such in its docstring.
- Track live timers in a variable (e.g. `agmon--timers`) so reset and
  cleanup hooks have something to enumerate.

## The stages

Each stage is one sitting: a goal, the new concepts it introduces, and
a visible checkpoint. Concepts compound; nothing is introduced twice.

### 3.el.1 — the fleet appears

Package skeleton and a minimal run list.

- Ensure plz is installed; add the clone's `emacs/` dir to `load-path`
  in the operator's init (show them the two lines; explain them).
- `defgroup agmon`, `defcustom agmon-url` (env-var default as above).
- `agmon--request`: synchronous for now (plz sync mode), one endpoint
  helper per route as needed.
- `agmon-list-mode`, derived from `tabulated-list-mode`; command
  `agmon` (autoloaded) that creates/refreshes the `*agmon*` buffer.
  Columns for now: short run id, effective status (plain text), cwd
  (abbreviated), started-at. `g` refreshes (this comes free from
  `tabulated-list-mode` + a revert function — explain how).
- `agmon-dev-reset` per the dev-workflow section.

Concepts to teach: package anatomy and lexical binding; derived modes
and how `define-derived-mode` builds keymap/hooks; the
tabulated-list format/entries protocol; interactive commands and
autoload cookies; the reload loop.

Checkpoint: the operator runs `M-x agmon` and sees their real fleet —
every run in the spool — in a buffer, sortable by clicking column
headers, refreshable with `g`.

### 3.el.2 — information design

Make the list glanceable; this is where the operator's taste enters.

- `defface`s per effective status (running, finished, error, stalled,
  interrupted) applied to the status cell; sensible choices that
  respect the user's theme (inherit from standard faces like `success`,
  `error`, `warning` where apt).
- New columns: latest PROGRESS line (truncated), cost (right-aligned,
  `$%.2f`), and age/duration rendered human-style (`4m`, `2h13m`,
  `3d`). Age formatting is a pure function; write it as one.
- Default sort: newest first. Column widths tuned against real data in
  the operator's frame — iterate live until it scans well.

Concepts: faces and `defface`, `propertize` and text properties,
inheritance from theme faces, the customize system.

Checkpoint: the operator can answer "is anything running, stalled, or
broken?" from a one-second glance, in their own theme, and likes how
it looks. Expect iteration here; that is the point of this stage.

### 3.el.3 — navigation

From the list into a run.

- `RET` on a run opens a detail buffer (derived from `special-mode`)
  rendering `/summary`: status block, activity, issues, metrics, and
  `result_text` in full — the final message is where the answers live,
  so it gets the space. `q` buries, `g` re-fetches.
- `j` (or similar) on any agmon buffer: pretty-printed raw JSON of the
  thing at point in a temp buffer — the `--json` escape hatch reborn
  as a keybinding.
- `agmon-jump`: `completing-read` over runs (candidates showing id,
  status, and cwd) that opens the chosen run's detail buffer from
  anywhere. Note for the operator how this supersedes CLI-style prefix
  matching.
- Session lineage, minimal form: in the detail buffer, if other runs
  share this run's `session_id`, list them as "resumed from / resumed
  by" lines (client-side grouping of the runs list; no server changes).

Concepts: `tabulated-list-get-id` and carrying data on list entries,
buffer-local variables, `special-mode` and its conventions,
`defvar-keymap`/`keymap-set`, minibuffer completion.

Checkpoint: list → RET → detail → q feels like dired; `agmon-jump`
finds a run in three keystrokes; the JSON escape hatch works on both
list and detail buffers.

### 3.el.4 — liveness

The mode stops being a snapshot.

- Convert `agmon--request` to support async with a callback (keep a
  sync path for commands where blocking is fine); handle errors by
  messaging, never by leaving a half-drawn buffer.
- Auto-refresh: a timer refreshes the list buffer at
  `agmon-list-refresh-seconds` (defcustom, suggest 10) — but ONLY
  while some window displays the buffer. Check visibility in the
  callback; cancel the timer on `kill-buffer-hook`. No background
  network chatter from an invisible buffer, ever.
- Refresh must preserve point (stay on the same run id if it still
  exists) and the current sort. Losing point on refresh is the kind of
  irritation this stage exists to eliminate — test it live.

Concepts: async callbacks and their pitfalls (the buffer may have died
before the response arrives — check liveness), `run-with-timer`,
buffer/window liveness predicates, cleanup hooks. `agmon-dev-reset`
earns its keep here.

Checkpoint: the operator leaves the list visible in a side window,
dispatches a real run on the box, and watches it appear and change
status without touching the keyboard. Killing the buffer provably
stops all polling (`M-x list-timers`).

### 3.el.5 — the tail

The hardest and best stage; everything prior was training for it.

- `t` on a run opens `*agmon-tail:<id>*`: a `special-mode` buffer that
  polls `/events?after=<cursor>` using the returned `next_after`,
  appends newly rendered lines, and repeats on a short timer (1–2s,
  defcustom) subject to the same visibility discipline as stage 4.
- Rendering ports the CLI's editorial line, as pure functions: tool
  calls compact (`Edit → agmon/derive.py`), PROGRESS lines in a bright
  face, errors loud, assistant prose dimmed and truncated to a line
  with `TAB` on it expanding/collapsing the full text, low-value
  event types (or an operator-chosen set) filtered by default with a
  toggle to show everything.
- Tail-follow point behavior, compilation-buffer style: if point was
  at the bottom before insert, keep it pinned to bottom; if the
  operator has scrolled up, never yank them down.
- When the run reaches terminal status: stop polling, insert a final
  verdict line, and `message` the outcome — the Emacs-native
  equivalent of `agmon tail && notify-send done`.

Concepts: cursor-based polling as a protocol, inserting into
read-only buffers (`inhibit-read-only`), markers vs point, window-point
subtleties when a buffer shows in multiple windows, invisibility or
overlay-based expand/collapse (pick the simpler that works; explain
the choice).

Checkpoint: the operator watches a live run scroll by in a side
window while working in another, scrolls up mid-run without being
dragged down, expands one prose line, and gets an echo-area verdict
when the run finishes.

### 3.el.6 — residue

- `agmon-costs`: the `/v1/stats/costs` rollup in a small
  tabulated-list or rendered table — the money view, one command away.
- Lineage badging in the list (e.g. a marker on runs that are resumes),
  building on stage 3's grouping.
- Keymap consolidation: one coherent map, documented; a `?` or
  `C-h m`-friendly summary. Leave keys unbound where stage 4 control
  verbs will land (kill/resume/dispatch) — note them in a comment.
- ert tests for the pure layer: age formatting, entry construction
  from a canned runs payload, event-line rendering from canned events,
  cursor arithmetic. Run via `M-x ert` and in batch. Keep it light —
  the operator's eyes are the primary test suite for this stage; the
  ert suite is a regression net for the pure functions and one more
  concept for the collection.
- README: an Emacs section — installation (load-path + require, or
  `package-vc-install` from the repo), the command entry points, and
  the defcustoms.

Checkpoint: a stranger with Emacs 30 could install and use the mode
from the README alone; `M-x ert` is green; checkdoc and byte-compile
are clean; everything is committed.

## Out of scope (fenced, do not build)

- TRAMP integration (clickable file paths opening on the box,
  dired/magit into a run's cwd). It graduates to the top of the
  enhancement list the day 3.el.5 lands; not before.
- Any control-plane action: launching, killing, resuming. Stage 4 owns
  those; this mode is read-only.
- Hooks-based capture of interactive sessions (separate roadmap item).
- Multi-file package structure, MELPA packaging, Emacs <30 compat.

## Definition of done for the whole stage

All six checkpoints accepted by the operator; invariants held at every
boundary; the file byte-compiles clean with no warnings under Emacs
30.2; the ert suite passes in batch; README updated; all work
committed. The acceptance criterion throughout is the operator's, not
yours: when in doubt at a checkpoint, ask, show, and iterate.
