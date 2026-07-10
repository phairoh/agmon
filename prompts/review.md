# Task: adversarial review

You are reviewing, not fixing. You may add tests and write REVIEW.md; you
may not change application code. Repo conventions in CLAUDE.md apply —
observed-vs-reasoned evidence, commit discipline, and the rule that
REVIEW.md is transient (a later consolidation run consumes and deletes
it; do not do any consolidation yourself, and do not edit CLAUDE.md or
BACKLOG.md).

## Scope

If a FOCUS section is appended below this prompt, it directs your
priorities. Absent one, review the diff since the most recent git tag —
tags mark previously reviewed baselines, so your subject is everything
after the newest tag, using the full repo as context.

Before anything else: run the full suite and record the result. That
output is your baseline and belongs in the Verification transcript. If
the baseline is not green (BACKLOG xfails reporting XFAIL are green),
that is itself a finding — report it and review anyway.

## Rules

1. **Observed vs. reasoned.** Every "passes", "fails", or "crashes"
   claim must come from something you executed in this session, with the
   command and actual output quoted. Anything else is labeled UNVERIFIED.
   A reviewer in this repo once reported a test as passing that could not
   even execute; that error class is the one unforgivable.
2. **Adversarial tests for real bugs only.** A genuine bug gets a test
   marked `@pytest.mark.xfail(strict=True, reason="F<n>")`. Run it;
   confirm pytest reports XFAIL — not ERROR, not PASS — before claiming
   it encodes the bug. Hardening ideas and nits get no tests.
3. **A short review of correct code is a successful review.** Do not
   manufacture findings; severity inflation is a review defect.
4. **Spec conflicts are findings, not fix targets** — tag SPEC-CONFLICT,
   recommend a resolution, change nothing.
5. **One commit at the end** (your tests + REVIEW.md). The suite must be
   green after it, your xfails XFAILing.

## Resources

- The primary spec under review is named in FOCUS; absent that, it is
  the newest file in specs/ (it ships inside the very diff you are
  reviewing). Judge the code against that spec and the README; report
  divergence rather than assuming either side is right. Older specs are
  historical (per CLAUDE.md) — consult them only to adjudicate whether a
  divergence from previously documented behavior is deliberate evolution
  or a regression.
- `~/agent-runs` (read-only) is real production data — interrupted runs,
  resume chains, genuine tool errors. Prefer real payloads over
  synthetic when reviewing parsing or derivation.
- The live service on localhost:8400 reads that spool; you may query it
  (including via the `agmon` CLI itself, if present) to compare actual
  behavior against your reading of the code. Do not restart, write
  through, or reconfigure it.

## REVIEW.md format

Findings ordered by severity, numbered F1..Fn, tagged
`BUG (high|med|low)`, `SPEC-CONFLICT`, `HARDENING`, or `NIT`. Each:
where (file:line), evidence (command + actual output), impact, suggested
fix (described, not applied), and its xfail test name if one exists. End
with a **Verification transcript**: baseline suite output and the tail of
your final `uv run pytest -q`, showing counts and each XFAIL.

## Definition of done

- REVIEW.md per the format; adversarial tests committed; one commit.
- Full suite green after your commit: every pre-existing test passing,
  your xfails XFAILing, BACKLOG xfails undisturbed.
- No application code changed; CLAUDE.md and BACKLOG.md untouched.
- Final message: findings count by severity, plus DECISIONS per repo
  convention.
