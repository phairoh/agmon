# Task: consolidate a review

You are consuming a completed review of this repo. REVIEW.md is a message
addressed to you; when you finish, it must be gone and every finding in it
must have been routed to exactly one of four bins: **FIXED**, **BACKLOGGED**,
**REMEMBERED**, or **DISCARDED**. No residue, nothing lost.

If REVIEW.md does not exist or contains no findings, say so and stop.

## Step 0 — verify before trusting

Reviewers have been wrong in this repo before. For each finding, reproduce
the claimed behavior (run the evidence command, run the xfail test) before
acting on it. A finding that does not reproduce is DISCARDED, with your
reproduction attempt quoted in DECISIONS. You are the review's reviewer.

## Default routing policy

Applies unless an OVERRIDES section appended below this prompt says
otherwise — per-finding instructions there take precedence.

- **BUG (high or med)** → FIXED now.
- **BUG (low)** → FIXED if the fix is small and local; else BACKLOGGED.
- **HARDENING** → FIXED if trivial and riskless; else BACKLOGGED.
- **NIT** → FIXED if a one-liner; else DISCARDED with reason.
- **SPEC-CONFLICT** → resolve per repo convention: if it blocks this
  task's definition of done, stop and ask; otherwise choose the most
  defensible resolution, apply it, and record it in DECISIONS.

## Bin mechanics

**FIXED.** Regression test first, always. If the reviewer left a strict
xfail for the finding: remove the marker, run the test, observe it fail,
then fix, then observe it pass. If no test exists, write one, observe it
fail, fix, observe it pass. Never weaken a test to make it pass; if a test
seems wrong, that is a SPEC-CONFLICT, handle it as one.

**BACKLOGGED.** Add an entry to BACKLOG.md. Each entry: a B-number (next
in sequence), origin ("from <review> F<n>"), severity tag, where
(file:line), impact, suggested fix, and the name of its strict-xfail test.
Backlogged BUGs must have a strict-xfail test — if the reviewer didn't
leave one, write it now and confirm it reports XFAIL. Hardenings may be
entry-only. If BACKLOG.md does not exist, create it with exactly this
header:

> Known, deliberately deferred items. Each BUG's authoritative
> reproduction is its named strict-xfail test — the suite re-verifies
> this backlog on every run. Do NOT fix these opportunistically while on
> another task: an unexpected XPASS fails the suite. To take an item:
> remove the xfail marker, fix, delete the entry.

**REMEMBERED.** Durable invariants a future run could violate go to
CLAUDE.md, terse. Not findings now enforced by tests — the tests are
their home. Not narrative — memory, not documentation.

**DISCARDED.** Allowed only with a stated reason in DECISIONS
(did not reproduce, out of scope by policy, superseded, trivial-but-wrong).
Silent discard is a defect of this run.

## Finish

1. Delete REVIEW.md. Its durable content now lives in tests, BACKLOG.md,
   and CLAUDE.md; git history keeps the rest.
2. Commit in logical units: (1) fixes + tests, (2) consolidation
   (BACKLOG.md, CLAUDE.md, REVIEW.md deletion). Plain imperative messages.
3. Final message: a routing table — every finding, its bin, and the
   artifact that now carries it (test name, B-number, CLAUDE.md line, or
   discard reason) — followed by DECISIONS for any judgment calls.

## Definition of done

- `uv run pytest` fully green; every FIXED finding's test passes live
  (no xfail marker); every BACKLOGGED bug's test reports XFAIL.
- REVIEW.md deleted. `git status` clean.
- Every finding from REVIEW.md appears exactly once in the routing table.
- No changes beyond the findings' scope and the consolidation artifacts.
