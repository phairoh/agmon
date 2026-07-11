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
- `/v1/runs` items carry no progress field; the latest PROGRESS line lives
  only in `/v1/runs/{id}/summary` (`activity.progress`). A list client that
  wants a live progress column must fetch /summary per run — n+1, and it
  compounds once the client auto-refreshes (agmon.el will poll the list
  every ~10s from stage 3.el.4). agmon.el shows `prompt_preview` instead
  for now. Enhancement: surface the latest PROGRESS line on each
  `/v1/runs` row. If that means deriving it at ingest time, that is a
  derivation change → bump `db.SCHEMA_VERSION` (per CLAUDE.md).
