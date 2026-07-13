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


# -- dispatch/section artifacts -------------------------------------------------

# The five fixed dispatch/section artifacts, in catalog order.
_DISPATCH_SPECS = (
    ("prompt", "dispatch", "prompt", None),
    ("prompt.focus", "section", "prompt", "FOCUS"),
    ("prompt.overrides", "section", "prompt", "OVERRIDES"),
    ("result", "dispatch", "result", None),
    ("result.decisions", "section", "result", "DECISIONS"),
)

_BASE_MISSING_REASON = {
    "prompt": "no prompt recorded",
    "result": "run produced no result",
}


def _dispatch_items(prompt: str | None, result_text: str | None) -> list[dict]:
    """The five dispatch/section artifacts with resolved content (``None``
    when unavailable) and, when so, a reason."""
    bases = {"prompt": prompt or None, "result": result_text or None}
    items = []
    for name, kind, base_key, marker in _DISPATCH_SPECS:
        base = bases[base_key]
        if marker is None:
            content = base
            reason = None if content is not None else _BASE_MISSING_REASON[base_key]
        elif base is None:
            content = None
            reason = _BASE_MISSING_REASON[base_key]
        else:
            content = derive_section(base, marker)
            reason = None if content is not None else f"{marker} marker not present"
        items.append({"name": name, "kind": kind, "content": content, "reason": reason})
    return items


# -- catalog ---------------------------------------------------------------


def build_catalog(run: dict, events: list[dict]) -> list[dict]:
    """Both artifact families for one run, in catalog order: the five
    dispatch/section artifacts (always listed, available or not) followed by
    file artifacts sorted by path."""
    result_text = derive_result_text(events)
    catalog = []
    for it in _dispatch_items(run.get("prompt"), result_text):
        entry = {"name": it["name"], "kind": it["kind"], "available": it["content"] is not None}
        if entry["available"]:
            entry["bytes"] = len(it["content"].encode("utf-8"))
        else:
            entry["reason"] = it["reason"]
        catalog.append(entry)
    for fa in derive_file_artifacts(events):
        entry = {
            "name": fa["path"],
            "kind": "file",
            "path": fa["path"],
            "ops": fa["ops"],
            "first_op": fa["first_op"],
            "last_seq": fa["last_seq"],
            "reconstructable": fa["reconstructable"],
            "available": fa["reconstructable"],
        }
        if fa["reconstructable"]:
            entry["bytes"] = fa["bytes"]
        else:
            entry["reason"] = "edit-only: no Write establishes a base to reconstruct from"
        catalog.append(entry)
    return catalog


# -- name resolution ---------------------------------------------------------


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _resolve_file(files: list[dict], name: str) -> dict:
    """§4 resolution order for file artifacts: exact path; unique basename;
    unique substring; else ambiguous/not-found."""
    for fa in files:
        if fa["path"] == name:
            return fa
    basename_matches = [fa for fa in files if _basename(fa["path"]) == name]
    if len(basename_matches) == 1:
        return basename_matches[0]
    if len(basename_matches) > 1:
        raise AmbiguousArtifactName(name, sorted(fa["path"] for fa in basename_matches))
    substring_matches = [fa for fa in files if name in fa["path"]]
    if len(substring_matches) == 1:
        return substring_matches[0]
    if len(substring_matches) > 1:
        raise AmbiguousArtifactName(name, sorted(fa["path"] for fa in substring_matches))
    raise ArtifactNotFound(name)


def resolve_artifact_content(run: dict, events: list[dict], name: str) -> str:
    """Resolve ``name`` to content per the §4 order: exact dispatch-artifact
    name, exact file path, unique file basename or substring. Raises
    ``ArtifactNotFound``, ``ArtifactUnavailable``, or ``AmbiguousArtifactName``."""
    result_text = derive_result_text(events)
    for it in _dispatch_items(run.get("prompt"), result_text):
        if it["name"] == name:
            if it["content"] is None:
                raise ArtifactUnavailable(it["reason"])
            return it["content"]

    files = derive_file_artifacts(events)
    match = _resolve_file(files, name)
    if not match["reconstructable"]:
        raise ArtifactUnavailable("edit-only: no Write establishes a base to reconstruct from")
    return reconstruct_file(events, match["path"])
