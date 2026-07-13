"""Pure artifact derivation: named, queryable things a run produced.

Two families (see the README's artifact model):

- **Dispatch/section** artifacts derived from the run record itself — the
  composed ``prompt`` and its ``FOCUS``/``OVERRIDES`` sections, the ``result``
  text and its ``DECISIONS`` section.
- **File** artifacts reconstructed from a run's Write/Edit tool events — named
  by their path, recoverable from the spool even after the file is deleted on
  disk.

Like ``derive``, this module is pure: it takes plain data (prompt/result text,
event dicts) and returns plain values. It imports neither sqlite3, fastapi, nor
os, so tests drive it directly. Event-block helpers are reused from ``derive``.
"""

from __future__ import annotations

import re

from .derive import _blocks

# The file-writing tools whose payloads we reconstruct from. Discovered from the
# real spool (`~/agent-runs`): Write carries full content, Edit an old->new
# replacement with a replace_all flag. Multi-edit/Notebook variants do not
# appear in the spool, so are deliberately not guessed at (see spec §3).
_WRITE_TOOL = "Write"
_EDIT_TOOL = "Edit"

# A marker line: a bare ALL-CAPS word, optionally prefixed by markdown heading
# syntax (# .. ###) and optionally suffixed with ':' — anchored at line start,
# so the word mid-prose never triggers. This general form ("any bare ALL-CAPS
# heading line") is what bounds one section from the next.
_ANY_MARKER_LINE = re.compile(r"^#{0,3}[ \t]*[A-Z]{2,}:?[ \t]*$", re.MULTILINE)


# -- errors ------------------------------------------------------------------


class ArtifactError(Exception):
    """Base for artifact resolution failures."""


class ArtifactUnknown(ArtifactError):
    """No artifact matches the requested name (maps to HTTP 404)."""


class ArtifactUnavailable(ArtifactError):
    """A listed artifact whose content cannot be produced — an absent section
    marker, or a non-reconstructable (edit-only) file (maps to HTTP 409)."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(reason)


class ArtifactAmbiguous(ArtifactError):
    """A name fragment matches several files (maps to HTTP 400)."""

    def __init__(self, fragment: str, candidates: list[str]):
        self.fragment = fragment
        self.candidates = candidates
        listed = ", ".join(candidates)
        super().__init__(f"{fragment!r} matches {len(candidates)} files: {listed}")


# -- section extraction ------------------------------------------------------


def _marker_line_re(marker: str) -> re.Pattern:
    return re.compile(rf"^#{{0,3}}[ \t]*{re.escape(marker)}:?[ \t]*$", re.MULTILINE)


def derive_section(text: str | None, marker: str) -> str | None:
    """Text from the **last** line-anchored ``marker`` occurrence to the next
    marker line or end of text, heading line excluded; ``None`` when the marker
    is absent or ``text`` is None.

    A marker line is the bare ALL-CAPS ``marker`` word, optionally prefixed by
    ``#``–``###`` and optionally suffixed with ``:``, anchored at line start.
    The single parser behind ``result.decisions``, ``prompt.focus``, and
    ``prompt.overrides``. An empty body (marker present, nothing after) returns
    ``""`` — present, not absent.
    """
    if text is None:
        return None
    matches = list(_marker_line_re(marker).finditer(text))
    if not matches:
        return None
    last = matches[-1]
    # Body begins on the line after the marker line.
    nl = text.find("\n", last.end())
    if nl == -1:
        return ""
    body_start = nl + 1
    # Body ends at the next marker line (any bare ALL-CAPS heading) or EOF.
    nxt = _ANY_MARKER_LINE.search(text, body_start)
    end = nxt.start() if nxt else len(text)
    return text[body_start:end].strip()


# -- file operations ---------------------------------------------------------


def _iter_file_ops(events: list[dict]):
    """Yield ``(path, op)`` for every Write/Edit tool call, in seq order.

    An op is ``{"op": "write", "content": str}`` or ``{"op": "edit", "old",
    "new", "replace_all"}``, tagged with its event ``seq``.
    """
    for event in events:
        seq = event.get("seq")
        for block in _blocks(event):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            inp = block.get("input")
            if not isinstance(inp, dict):
                continue
            path = inp.get("file_path")
            if not isinstance(path, str):
                continue
            if name == _WRITE_TOOL:
                content = inp.get("content")
                yield path, {
                    "op": "write",
                    "seq": seq,
                    "content": content if isinstance(content, str) else "",
                }
            elif name == _EDIT_TOOL:
                yield path, {
                    "op": "edit",
                    "seq": seq,
                    "old": inp.get("old_string") if isinstance(inp.get("old_string"), str) else "",
                    "new": inp.get("new_string") if isinstance(inp.get("new_string"), str) else "",
                    "replace_all": bool(inp.get("replace_all")),
                }


def _ops_by_path(events: list[dict]) -> dict[str, list[dict]]:
    """``{path: [op, ...]}`` in seq order (insertion order preserved for a
    stable, deterministic catalog)."""
    out: dict[str, list[dict]] = {}
    for path, op in _iter_file_ops(events):
        out.setdefault(path, []).append(op)
    for ops in out.values():
        ops.sort(key=lambda o: (o["seq"] is None, o["seq"]))
    return out


def _apply(ops: list[dict]) -> str:
    """Fold a path's ops into final content. First op is always a write (callers
    guard reconstructability). Write replaces; edit applies its old->new against
    the *current* reconstruction, honoring replace_all."""
    content = ""
    for op in ops:
        if op["op"] == "write":
            content = op["content"]
        else:
            if op["replace_all"]:
                content = content.replace(op["old"], op["new"])
            else:
                content = content.replace(op["old"], op["new"], 1)
    return content


def derive_file_artifacts(events: list[dict]) -> list[dict]:
    """Per written path, a catalog row: ``{path, ops, first_op, last_seq,
    reconstructable, bytes}``. Reconstructable means the op sequence starts from
    a Write (known-full content); an edit-only file is listed but honestly
    marked with ``bytes: None``."""
    out = []
    for path, ops in _ops_by_path(events).items():
        reconstructable = ops[0]["op"] == "write"
        content = _apply(ops) if reconstructable else None
        out.append(
            {
                "path": path,
                "ops": len(ops),
                "first_op": ops[0]["op"],
                "last_seq": ops[-1]["seq"],
                "reconstructable": reconstructable,
                "bytes": len(content.encode("utf-8")) if content is not None else None,
            }
        )
    return out


def reconstruct_file(events: list[dict], path: str) -> str:
    """Final text content of ``path``, ops applied in seq order. Raises
    ``ArtifactUnknown`` if the run never wrote to ``path`` and
    ``ArtifactUnavailable`` if its ops start from an edit (base unknown)."""
    ops = _ops_by_path(events).get(path)
    if ops is None:
        raise ArtifactUnknown(f"no file artifact {path!r}")
    if ops[0]["op"] != "write":
        raise ArtifactUnavailable(
            path, "not reconstructable: first operation is an edit, base content unknown"
        )
    return _apply(ops)


# -- catalog assembly --------------------------------------------------------

# The five always-listed dispatch/section artifacts. Each entry maps a name to
# (kind, extractor) where the extractor takes (prompt, result_text) -> content.
_DISPATCH = (
    ("prompt", "dispatch", lambda p, r: p, "no prompt stored"),
    ("prompt.focus", "section", lambda p, r: derive_section(p, "FOCUS"),
     "no FOCUS section in the prompt"),
    ("prompt.overrides", "section", lambda p, r: derive_section(p, "OVERRIDES"),
     "no OVERRIDES section in the prompt"),
    ("result", "dispatch", lambda p, r: r, "run produced no result"),
    ("result.decisions", "section", lambda p, r: derive_section(r, "DECISIONS"),
     "no DECISIONS section in the result"),
)


def _dispatch_content(prompt: str | None, result_text: str | None, name: str):
    """(content, reason) for a dispatch/section name, or raise KeyError if the
    name is not one of the five. content is None when unavailable."""
    for n, _kind, extract, reason in _DISPATCH:
        if n == name:
            return extract(prompt, result_text), reason
    raise KeyError(name)


def build_catalog(
    prompt: str | None, result_text: str | None, events: list[dict]
) -> list[dict]:
    """The full artifact list for a run: the five dispatch/section artifacts
    (always listed, available or not) followed by one row per written file."""
    catalog: list[dict] = []
    for name, kind, extract, reason in _DISPATCH:
        content = extract(prompt, result_text)
        item = {"name": name, "kind": kind, "available": content is not None}
        if content is None:
            item["reason"] = reason
        else:
            item["bytes"] = len(content.encode("utf-8"))
        catalog.append(item)
    for fa in derive_file_artifacts(events):
        item = {
            "name": fa["path"],
            "kind": "file",
            "available": fa["reconstructable"],
            "bytes": fa["bytes"],
            "path": fa["path"],
            "ops": fa["ops"],
            "first_op": fa["first_op"],
            "last_seq": fa["last_seq"],
            "reconstructable": fa["reconstructable"],
        }
        if not fa["reconstructable"]:
            item["reason"] = "not reconstructable: first operation is an edit, base content unknown"
        catalog.append(item)
    return catalog


# -- name resolution ---------------------------------------------------------


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def resolve_content(
    prompt: str | None, result_text: str | None, events: list[dict], name: str
) -> str:
    """Resolve a ``name`` to artifact content. Resolution order: exact dispatch/
    section name; exact file path; unique file basename or substring.

    Raises ``ArtifactUnavailable`` (listed but no content), ``ArtifactAmbiguous``
    (a fragment matching several files), or ``ArtifactUnknown`` (no match).
    """
    # 1. exact dispatch/section name
    try:
        content, reason = _dispatch_content(prompt, result_text, name)
    except KeyError:
        pass
    else:
        if content is None:
            raise ArtifactUnavailable(name, reason)
        return content

    ops_by_path = _ops_by_path(events)

    # 2. exact file path
    if name in ops_by_path:
        return reconstruct_file(events, name)

    # 3. unique file basename or substring (the run-id ergonomic, on paths)
    candidates = [p for p in ops_by_path if _basename(p) == name or name in p]
    if not candidates:
        raise ArtifactUnknown(f"no artifact named {name!r}")
    if len(candidates) > 1:
        raise ArtifactAmbiguous(name, sorted(candidates))
    return reconstruct_file(events, candidates[0])
