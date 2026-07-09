# agmon stage-1 review — ingest byte-offset & durability

Scope requested: byte-offset handling in `ingest.py` (binary vs text mode,
partial trailing lines, multi-byte UTF-8 split across a read boundary),
transaction boundaries between event inserts and offset commits, and duplicate
ingestion after a simulated crash.

Reviewed against `SPEC.md`. **No application code was changed.** Adversarial
tests were added in `tests/test_adversarial.py`; the ones that document open
bugs are marked `xfail(strict=True)` so the suite stays green while the gap is
tracked (they flip to a hard failure the moment the bug is fixed and the
`xfail` should be removed).

---

## F1 — BUG (high): a line containing invalid UTF-8 crashes the scan and stalls the ingester

**Where:** `src/agmon/ingest.py:179-190`

```python
text = raw.decode("utf-8", errors="replace")
try:
    obj = json.loads(raw)
except json.JSONDecodeError:
    rows.append((run_id, seq, "_unparseable", None, text))
    continue
```

`json.loads()` is called on the raw **bytes** of the line. When those bytes are
not valid UTF-8 (e.g. a corrupted line, or a tool emitting latin-1 / raw binary
in a payload), `json.loads(bytes)` decodes internally and raises
**`UnicodeDecodeError`**, which is a subclass of `ValueError` but **not** of
`json.JSONDecodeError`. The `except json.JSONDecodeError` clause does not catch
it, so the exception escapes `_ingest_events`.

Consequences:

1. The exception is raised **before** the `with self.conn:` block, so the byte
   offset for that file is never advanced. The polling loop
   (`_loop` → `except Exception: log.exception`) simply retries the identical
   bytes every 2 seconds — a permanent livelock on that run's `.jsonl`.
2. Inside a single scan, `_scan` iterates `sorted(runs_dir.glob("*.jsonl"))`.
   The exception propagates out of the loop, so **every alphabetically-later
   run stops ingesting too** until the offending file is removed. One corrupt
   byte in one run stalls an unbounded number of other runs.

**Spec conflict:** SPEC.md §Ingester says *"Lines that fail to parse as JSON are
stored with `type = "_unparseable"` and the raw line as the payload."* An
invalid-UTF-8 line plainly fails to parse as JSON and should land as
`_unparseable`, not crash. The author clearly anticipated non-UTF-8 bytes — the
line is decoded with `errors="replace"` precisely to build a lossy `text` for
storage — but the parse path still hands strict bytes to `json.loads`.

**Suggested fix (not applied):** widen the catch to
`except (json.JSONDecodeError, UnicodeDecodeError):`, or parse the
already-decoded `text` (`json.loads(text)`) instead of `raw`.

**Tests:** `test_invalid_utf8_line_ingested_as_unparseable` and
`test_invalid_utf8_does_not_stall_later_runs` (both `xfail(strict=True)`).

---

## F2 — OK (verified): binary mode + newline-keyed consumption handles split multi-byte UTF-8 correctly

**Where:** `src/agmon/ingest.py:157-169`

- The file is opened in **binary** mode (`open(path, "rb")`) and the offset
  stored in `ingest_state.byte_off` is a true byte count. This is correct: in
  text mode `seek`/`tell` values are opaque cookies, not byte offsets, and
  seeking to a stored integer offset would be undefined. Good.
- Consumption stops at the **last** newline: `nl = data.rfind(b"\n")`,
  `chunk = data[: nl + 1]`, `new_off = off + len(chunk)`. A partial trailing
  line is left in the file for the next scan.
- A multi-byte UTF-8 character can never be split by this boundary: the newline
  byte `0x0A` never occurs inside a multi-byte UTF-8 sequence (lead bytes are
  ≥`0xC0`, continuation bytes are `0x80`–`0xBF`). So if the writer flushes only
  the first bytes of a multi-byte character, that character sits in the
  yet-to-arrive trailing line and is not consumed until its line completes with
  a newline. No boundary corruption, no premature/partial decode.

This is correct behavior and worth locking in.

**Test:** `test_multibyte_utf8_split_across_scans` (passes against current code).

---

## F3 — OK (verified): event inserts and the offset write commit in one transaction

**Where:** `src/agmon/ingest.py:193-210`

The `INSERT OR IGNORE INTO runs`, the `executemany` of events, and the
`ingest_state` offset `UPSERT` all execute inside a single `with self.conn:`
block. `sqlite3.Connection.__exit__` commits on success and **rolls back on any
exception**, so the offset is persisted **iff** its events are. This satisfies
SPEC.md: *"Persist the new byte offset only after the corresponding inserts
commit (same transaction)."*

Note: `MAX(seq)` is read *before* the transaction (`ingest.py:171-173`). That is
safe because the ingester is the sole writer and scans are serialized by
`self._lock`, so no other writer can interleave between the read and the commit.

**Test:** `test_offset_and_events_commit_atomically` forces the `ingest_state`
write to fail via a wrapped connection and asserts the event rows and the run
row are rolled back with it (nothing partially committed). Passes.

---

## F4 — OK (verified): no duplication or skips across a simulated crash

**Where:** `src/agmon/ingest.py:151-210`

Because the offset and its events commit atomically (F3), a crash leaves the DB
in a consistent state: either a scan's events + advanced offset are both
durable, or neither is. On restart the ingester rebuilds its offset from the
committed `ingest_state` row and re-reads only the bytes past it — no line is
ingested twice and none is skipped. This matches SPEC.md's *"a crash never skips
or duplicates events."*

`INSERT OR IGNORE INTO events` is defensive belt-and-suspenders; under the
single-writer / same-transaction invariant it never actually fires.

**Test:** `test_no_duplicates_after_simulated_crash` simulates a crash by
discarding the ingester's connection and building a fresh `Ingester` on the same
DB and runs dir, then appending and re-scanning. Asserts contiguous, duplicate-
free seqs and a no-op final scan. Passes.

---

## Underspecified / minor notes (no test, or noted only)

- **N1 — offset past EOF after truncation/replacement.** SPEC.md assumes
  `.jsonl` files are append-only. If one is ever truncated or replaced shorter
  than the stored offset, `f.seek(off); f.read()` returns `b""` and the run
  silently stops ingesting forever (stuck offset past EOF). There is no
  detection (e.g. comparing `stat().st_size` to `byte_off`). Out of scope per
  the append-only contract, but undetected if the contract is violated.

- **N2 — `_unparseable` is lossy even once F1 is fixed.** The stored payload is
  `raw.decode("utf-8", errors="replace")`, so invalid bytes become U+FFFD and
  the original bytes are unrecoverable. Acceptable for a `TEXT` column, but the
  raw line is not preserved verbatim as the spec's wording ("the raw line as the
  payload") might suggest.

- **N3 — seq is consumed per appended row, not per committed row.** `seq` is
  incremented for every non-blank line before insert. If an event row were ever
  dropped by `INSERT OR IGNORE` (not reachable under the current invariant), the
  seq counter would still have advanced, leaving a permanent gap that clients
  paginating with `after=`/`next_after` would step over silently.
