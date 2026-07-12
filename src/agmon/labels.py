"""Label constraints — the spool-level primitive.

Labels are flat string->string facts stamped at dispatch. They are the only
thing the spool contract knows about; all *meaning* (pipeline, phase, parent
lineage) lives in the derivation layer, never here. This module owns just one
thing: what makes a label *well-formed*.

The same rules are applied two ways. The wrapper (``agmon run``) is strict —
``build_labels`` raises on any violation, so a bad flag never reaches the spool.
The ingester is lenient — it calls ``validate_label`` per entry and skips
violators (foreign or buggy writers) with a log line, never failing the file.
"""

from __future__ import annotations

import re

# Keys: lowercase word-ish tokens, 1..64 chars. Values: non-empty printable
# strings (spaces allowed, control chars not), up to 256 chars. At most 16
# labels per run.
KEY_RE = re.compile(r"[a-z0-9_.-]{1,64}\Z")
MAX_VALUE_LEN = 256
MAX_LABELS = 16

# Keys the derivation layer interprets as pipeline lineage (see derive.derive_lineage).
# Reserved by convention only — still stored and validated as ordinary labels.
RESERVED_KEYS = ("pipeline", "phase", "parent")


def validate_label(key: object, value: object) -> str | None:
    """Return an error message if ``(key, value)`` is not a well-formed label,
    else ``None``. One rule set, shared by the strict wrapper and the lenient
    ingester."""
    if not isinstance(key, str) or not KEY_RE.match(key):
        return f"invalid label key {key!r}: must match [a-z0-9_.-]{{1,64}}"
    if not isinstance(value, str) or value == "":
        return f"invalid label value for {key!r}: must be a non-empty string"
    if len(value) > MAX_VALUE_LEN:
        return f"invalid label value for {key!r}: exceeds {MAX_VALUE_LEN} chars"
    if not value.isprintable():
        return f"invalid label value for {key!r}: control characters not allowed"
    return None


def build_labels(
    label_args: list[str] | None,
    *,
    pipeline: str | None = None,
    phase: str | None = None,
    parent: str | None = None,
) -> dict[str, str]:
    """Compile ``--label KEY=VALUE`` plus the ``--pipeline/--phase/--parent``
    sugar into a validated flat dict, strictly — this is the wrapper's contract.

    Raises ``ValueError`` with a distinct message per violation: malformed
    ``KEY=VALUE``, bad key/value, duplicate key (including sugar colliding with
    an explicit label for the same reserved key), or more than ``MAX_LABELS``.
    """
    labels: dict[str, str] = {}

    def _add(key: str, value: str, *, source: str) -> None:
        if key in labels:
            raise ValueError(f"duplicate label key {key!r} (from {source})")
        err = validate_label(key, value)
        if err:
            raise ValueError(err)
        labels[key] = value

    for raw in label_args or []:
        if "=" not in raw:
            raise ValueError(f"invalid --label {raw!r}: expected KEY=VALUE")
        key, value = raw.split("=", 1)
        _add(key, value, source="--label")

    # Sugar compiles to ordinary reserved-key labels; no separate storage.
    for key, value in (("pipeline", pipeline), ("phase", phase), ("parent", parent)):
        if value is not None:
            _add(key, value, source=f"--{key}")

    if len(labels) > MAX_LABELS:
        raise ValueError(f"too many labels: {len(labels)} > {MAX_LABELS}")
    return labels
