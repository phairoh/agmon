"""Pure artifact derivation: the catalog of named things a run produced.

Same purity rule as ``derive.py`` — plain data in, plain dicts out; no
sqlite3, fastapi, or os. File artifacts are reconstructed from a run's
Write/Edit tool_use events (the only file-writing tools seen in the real
spool); the spool knows the patches, not live disk state, so a file the run
later deleted still reconstructs — that is the point.
"""

from __future__ import annotations

from .derive import _blocks, _event_type, derive_result_text, derive_section

_WRITE_EDIT_TOOLS = ("Write", "Edit")


class ArtifactNotFound(Exception):
    """No artifact — dispatch, section, or file — matches the given name."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"unknown artifact: {name!r}")


class NotReconstructableError(Exception):
    """A file path is known (edited) but no Write establishes a base to
    reconstruct from."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"{path!r} is not reconstructable: no Write establishes a base")


class ArtifactUnavailable(Exception):
    """A listed artifact (dispatch/section marker absent, or a
    non-reconstructable file) has no content to serve."""

    def __init__(self, reason: str | None):
        self.reason = reason
        super().__init__(reason or "artifact unavailable")


class AmbiguousArtifactName(Exception):
    """A file-name fragment matches more than one written path."""

    def __init__(self, name: str, candidates: list[str]):
        self.name = name
        self.candidates = candidates
        super().__init__(f"{name!r} matches {len(candidates)} files: {candidates}")


# -- file reconstruction -------------------------------------------------------


def _write_edit_ops(events: list[dict]) -> dict[str, list[dict]]:
    """``{path: [{"seq", "op", "input"}, ...]}`` in seq order, from Write/Edit
    tool_use blocks across the run's assistant events."""
    ops_by_path: dict[str, list[dict]] = {}
    for event in events:
        if _event_type(event) != "assistant":
            continue
        seq = event.get("seq")
        for block in _blocks(event):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name not in _WRITE_EDIT_TOOLS:
                continue
            input_ = block.get("input")
            if not isinstance(input_, dict):
                continue
            path = input_.get("file_path")
            if not isinstance(path, str):
                continue
            op = "write" if name == "Write" else "edit"
            ops_by_path.setdefault(path, []).append({"seq": seq, "op": op, "input": input_})
    return ops_by_path


def _apply_ops(ops: list[dict]) -> str:
    content = ""
    for op in ops:
        input_ = op["input"]
        if op["op"] == "write":
            content = input_.get("content") or ""
        else:
            old = input_.get("old_string", "")
            new = input_.get("new_string", "")
            count = -1 if input_.get("replace_all") else 1
            content = content.replace(old, new, count)
    return content


def derive_file_artifacts(events: list[dict]) -> list[dict]:
    """Per written path (sorted): ``{"path", "ops", "first_op", "last_seq",
    "reconstructable", "bytes"}``. ``reconstructable`` is true only when the
    op sequence starts with a Write; edit-only files are listed but honestly
    marked, with ``bytes`` null."""
    ops_by_path = _write_edit_ops(events)
    out = []
    for path in sorted(ops_by_path):
        ops = ops_by_path[path]
        first_op = ops[0]["op"]
        reconstructable = first_op == "write"
        item = {
            "path": path,
            "ops": len(ops),
            "first_op": first_op,
            "last_seq": ops[-1]["seq"],
            "reconstructable": reconstructable,
            "bytes": len(_apply_ops(ops).encode("utf-8")) if reconstructable else None,
        }
        out.append(item)
    return out


def reconstruct_file(events: list[dict], path: str) -> str:
    """The final content of ``path``, ops applied in seq order against the
    evolving reconstruction. Raises ``ArtifactNotFound`` for a path with no
    Write/Edit ops, ``NotReconstructableError`` when the ops don't start from
    a Write."""
    ops = _write_edit_ops(events).get(path)
    if ops is None:
        raise ArtifactNotFound(path)
    if ops[0]["op"] != "write":
        raise NotReconstructableError(path)
    return _apply_ops(ops)
