# Decisions — stage 3 (the CLI)

Non-obvious resolutions made while building the CLI, per the spec's
"most defensible resolution, recorded in DECISIONS" convention.

## D1 — `ls` "last activity" is `last_event_type`, not tool + target

The spec's `ls` column list asks for "last activity (tool + truncated
target)". That detail (the last tool call and its target) is only available
from the per-run **summary** endpoint's `activity.last_tool`; the `/v1/runs`
list rows carry only `last_event_type`. Surfacing tool+target would mean one
summary request per displayed row (20 by default) — turning a single-request
fleet glance into N+1 requests. Server changes were restricted to the two
listed items, so enriching the list endpoint was out of scope.

Resolution: the `activity` column shows `last_event_type` (e.g. `assistant`,
`result`, `user`) from the single list call. `agmon show <id>` gives the full
last-tool detail. If a future server change adds last-tool to the list row,
swap the column source with no CLI restructuring.

## D2 — `agmon tail` exit codes: interrupted maps to 1

The spec defines exit 0 (finished), 1 (error), 3 (died) and is silent on
`interrupted` (meta error with a null result_subtype — the stream ended with
no result event). Since interrupted is a non-success terminal state, it maps
to exit **1** alongside `error`. `stalled` and `running` are non-terminal and
never end the loop. The full contract is recorded in CLAUDE.md.

## D3 — TSV carries a header row

"Plain TSV with no decoration" is read as: no rich borders, no color, no
box-drawing — i.e. a real `\t`-separated grid a pipe can consume. A single
leading header line of column names is kept (it is plain text and makes the
columns self-describing for `awk`/`cut`); the decoration being stripped is the
rich table chrome, not the header.

## D4 — `tail` polls the summary endpoint only when caught up

To avoid doubling request volume on a live stream, the follow loop consults
`/summary` for terminal/stall status only when an events page comes back empty
(the run has gone quiet) or when a result event is seen. While events are
still flowing the run is by definition live, so no status poll is needed.

## D5 — `run` is intercepted before argparse

`argparse`'s `REMAINDER` mishandles a leading option-like token (e.g. a bare
`--help`), passing it up to the parent parser. So `main` special-cases a
leading `run` argument and hands everything after it straight to the ported
wrapper's own parser — which owns the real flag set — before top-level parsing.
This is why `agmon run --help` shows the wrapper's flags, not a passthrough stub.
