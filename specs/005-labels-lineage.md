# Task: agmon stage 4a — labels and lineage foundations

agmon runs are currently related only by accident (resume chains sharing a
session_id). This stage adds intentional relations: arbitrary key=value
**labels** stamped at dispatch, with pipeline lineage (spec → build →
review → consolidate phases, parent/child edges) as blessed conventions
derived on top. Labels are the spool-level primitive; *meaning lives in
the derivation layer*. Do not add dedicated pipeline/phase columns or
fields anywhere in the spool contract — that inversion is deliberate and
load-bearing.

Repo conventions in CLAUDE.md apply.

## 1. Wrapper — `agmon run`

- New repeatable flag `--label KEY=VALUE`. Constraints, enforced strictly
  at the wrapper with clear errors: keys match `[a-z0-9_.-]{1,64}`;
  values are non-empty printable strings without control characters,
  ≤256 chars; at most 16 labels per run; duplicate keys rejected.
  Flat string→string only — no nesting, ever.
- Convention sugar that compiles to labels (no separate storage):
  `--pipeline X` → `pipeline=X`, `--phase Y` → `phase=Y`,
  `--parent RUN_ID` → `parent=RUN_ID`. Sugar and explicit `--label` for
  the same key is a duplicate-key error.
- meta.json gains `"labels": {…}` (empty object when none). This is a
  spool-contract addition; document it in the README's spool section
  including the constraints, so foreign writers can participate.

## 2. Ingest + schema

- New table `run_labels (run_id TEXT NOT NULL, key TEXT NOT NULL,
  value TEXT NOT NULL, PRIMARY KEY (run_id, key))`, populated from
  meta.json on upsert.
- The ingester is lenient where the wrapper is strict: label entries in
  a meta.json that violate the constraints (foreign or buggy writers)
  are skipped with a log line, never fatal, and never stall the file —
  per the containment invariant.
- This is an ingest-derivation change: bump `db.SCHEMA_VERSION`
  (drop-and-replay per CLAUDE.md). Historical runs correctly ingest with
  no labels.

## 3. Derivation — lineage (blessed conventions)

Reserved keys interpreted by derivation: `pipeline` (a grouping id),
`phase` (conventionally one of spec|build|review|consolidate, but any
value renders — no enforcement of vocabulary or ordering), `parent`
(a run_id, the causal edge).

`derive` stays pure; give it the data it needs. Summary gains:

```
"lineage": {
  "pipeline": "…" | null,
  "phase": "…" | null,
  "parent": "<run_id>" | null,          # as labeled; not validated to exist
  "children": ["<run_id>", …],          # runs whose parent label = this run
  "siblings": [                          # same pipeline value, excluding self
    {"run_id": …, "phase": …, "effective_status": …, "started_at": …}
  ]
}
```

`lineage` is null when the run has none of the reserved keys. Pipeline
lineage is distinct from resume-chain lineage (session_id); both may
exist on one run and must not be conflated in any output.

## 4. API

- `GET /v1/runs` accepts repeatable `label=key=value` filters, AND
  semantics across repeats. Invalid filter syntax → 400 with a clear
  message. List items and run detail gain a `labels` object.
- Summary includes the `lineage` block. Keep queries bounded: sibling
  and children lookups are single indexed queries against `run_labels`,
  not per-row scans.

## 5. CLI

- `agmon ls --label k=v` (repeatable), plus sugar `--pipeline X` and
  `--phase Y`. When every listed run shares a pipeline filter, show a
  `phase` column; otherwise show a compact labels cell only for runs
  that have labels (blank cell, no noise, for the common unlabeled
  case).
- `agmon show`: when lineage exists, a **Pipeline** section — pipeline
  id, this run's phase, parent and children (short ids), and a sibling
  table (short id, phase, status, started) — rendered separately from
  the existing resume-chain lines, and clearly named so the two lineages
  read as different relations.
- `agmon run` passes the new flags through per §1.

## 6. Docs

- README: labels in the spool contract (constraints, foreign-writer
  note), the reserved-key conventions and what derivation does with
  them, `label=` filtering in the API section, new CLI flags with one
  example dispatching a labeled phase run.
- CLAUDE.md, one invariant: labels are flat string→string facts in the
  spool; all meaning (pipeline, phase, parent) lives in derivation —
  never add semantics to the spool contract.

## Tests (required, alongside the full existing suite)

1. Wrapper: valid labels round-trip to meta.json; each constraint
   violation rejected with a distinct error; sugar flags compile to
   labels; sugar+explicit duplicate rejected.
2. Ingest: labels land in run_labels; a meta.json containing one invalid
   and two valid entries ingests the valid two and logs; replay after
   version bump repopulates labels identically.
3. Derivation: lineage null with no reserved keys; children computed
   from parent labels; siblings share pipeline and exclude self; a
   `parent` pointing at a nonexistent run renders as-is without error.
4. API: single and multiple `label=` filters (AND), malformed filter →
   400; labels present in list and detail; summary lineage matches a
   constructed three-phase pipeline fixture.
5. CLI: ls filter flags plumb through to the query; show renders the
   Pipeline section for a labeled fixture and omits it for an unlabeled
   one (drive via the injected client per the existing pattern).

## Definition of done

- `uv run pytest` fully green; BACKLOG xfails undisturbed.
- Schema version bumped; a pre-existing spool replays cleanly with
  labels populated where meta files carry them.
- README and CLAUDE.md updated per §6. All work committed, tree clean.

Out of scope: any orchestrator or phase gating, POST /v1/runs,
artifact reconstruction from events, agmon.el changes, BLOCKED
conventions, enforcement of phase vocabulary or ordering, and label
editing after dispatch (labels are dispatch-time facts).
