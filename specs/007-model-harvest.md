# Task: agmon — observed model harvest

> Context: extracted from unmerged spec 006 §6. The rest of 006 (the
> artifact catalog) is deliberately unimplemented — do not build any of
> it. This spec stands alone against main.

`runs.model` has been null for the project's entire history: the wrapper
records the `--model` *argument* (rarely passed), never the model that
actually served the run. The stream knows — the init system event
carries the resolved model identity. Recent A/B experiments (two builds
of one spec, sonnet vs opus) made the gap concrete: runs are
indistinguishable in every view unless someone remembered a flag.

Fix it at the ingest layer so history backfills on replay. Repo
conventions in CLAUDE.md apply, including test-first feature work.

## 1. Ingest

Derive `runs.model` from the run's init event when present; null
otherwise. `model` now means **observed** — a run killed before init
honestly stays null ("never observed"), which is signal, not a gap.
Do not fall back to the requested value; intent is not observation.

## 2. Wrapper

Write the `--model` argument to meta as `model_requested` (additive
spool-contract field); stop writing meta `model`. Requested intent
stays retrievable via the run detail's meta passthrough — no new
column for it.

## 3. Schema

Bump `SCHEMA_VERSION` per the CLAUDE.md invariant (this is an
ingest-derivation change): the drop-and-replay must teach every
historical run which model served it.

## 4. Surfacing

`model` is an existing column; nothing structural. Verify it renders in
the run detail and `agmon show`, is reachable via `--fields`, and
update the README: the spool-contract addition (`model_requested`) and
the observed-vs-requested semantics of `model`.

## Tests (test-first, per conventions)

1. Init-event model populates the column; the value is the init
   event's, byte-exact.
2. A run with no init event (interrupted-pre-init fixture) stays null.
3. The requested value is never used as a fallback — a fixture with
   `model_requested` set and no init event must yield null.
4. Replay over a historical fixture spool (metas without
   model_requested, events with init) backfills model on every run
   with an init event.
5. Wrapper writes model_requested to meta when `--model` is passed and
   omits it otherwise; meta `model` is no longer written.

## Definition of done

- Full suite green; BACKLOG xfails undisturbed. Schema version bumped
  exactly once; **no new columns** — if you believe you need one, stop
  and ask.
- A pre-existing spool replays cleanly with model backfilled wherever
  an init event exists.
- README updated per §4. All work committed, tree clean.

Out of scope: everything else in spec 006 (artifact catalog, section
extraction, reconstruction — none of it exists and none of it should
after this run), any cost-by-model rollup (future; this stage only
makes the data true), and any client rendering changes beyond
verifying the column now carries data.
