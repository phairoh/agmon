# friction
- INVESTIGATE: thinking blocks in spool are present but empty.
  `jq '.message.content[]? | select(.type=="thinking")'` on recent runs
  yields blocks with empty `.thinking` fields (40/40 sampled, 2026-07-11).
  Questions: does `claude -p` stream-json thin thinking bodies on stdout?
  Is content under a different key (delta events / signature fields)?
  Does MAX_THINKING_TOKENS or --include-partial-messages change what's
  spooled? Check docs + a probe run before designing any rendering.
  If capturable: tail snippet + --thinking flag, events --type thinking,
  summary.activity.last_thinking (the stalled-vs-thinking sensor).
  WHY THIS RANKS HIGH (not just a nice-to-have): thinking is where an
  agent's confusion is legible. Without it the operator sees the tool
  calls and the result but not *what tripped the agent up* — the dead
  keybinding it couldn't explain, the checkdoc warning it burned three
  round-trips on, the wrong assumption it never stated. Those are exactly
  the moments you want to catch, and they are invisible in the current
  stream. Surfacing thinking turns agmon from "what did it do" into "why
  did it get stuck", which is the whole point of monitoring a headless run
  you can't watch live.
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
