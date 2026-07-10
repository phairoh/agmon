"""All formatting for the agmon CLI. No I/O, no argument parsing.

Everything here is terminal-independent: functions return plain strings, rich
renderables (Table/Text/Markdown), or ``(headers, rows)`` pairs that ``to_tsv``
and ``to_table`` turn into piped or on-screen output. A future TUI or Emacs
bridge can reuse these directly. Event compaction (``summarize_event``) is
shared by ``tail`` and ``events``.

Value-formatting policy: table/TSV columns render times relative ("3m ago") and
durations as "12m40s"; the raw ISO stays reachable via ``--json``/``--fields``,
which project raw JSON values (``flatten_one`` / ``project_rows``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from .derive import (
    _PROGRESS_RE,
    _blocks,
    _content_to_text,
    _event_type,
    _parse_dt,
    _text_blocks,
    _tool_target,
)

# effective_status -> rich style (ls status column, show header, tail summary).
STATUS_STYLES = {
    "running": "cyan",
    "finished": "green",
    "error": "red",
    "died": "red",
    "interrupted": "yellow",
    "stalled": "yellow",
}


@dataclass
class Styled:
    """A cell/line with text plus an optional rich style. Plain output uses the
    text; rich output applies the style."""

    text: str
    style: str = ""


Cell = "Styled | str"


# -- scalar formatting -------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def relative_time(iso: str | None, now: datetime | None = None) -> str:
    dt = _parse_dt(iso)
    if dt is None:
        return "-"
    secs = (_now(now) - dt).total_seconds()
    if secs < 0:
        secs = 0
    secs = int(secs)
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def format_duration(secs: float | None) -> str:
    if secs is None:
        return "-"
    secs = int(secs)
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def format_cost(v: float | None) -> str:
    if v is None:
        return "-"
    return f"${v:.2f}"


def short_id(run_id: str | None) -> str:
    if not run_id:
        return "-"
    return run_id.rsplit("-", 1)[-1]


def project_basename(cwd: str | None) -> str:
    if not cwd:
        return "-"
    return os.path.basename(cwd.rstrip("/")) or cwd


def truncate(s: str, width: int | None) -> str:
    if width and width > 0 and len(s) > width:
        return s[: max(0, width - 1)] + "…"
    return s


def _duration_secs(
    started_at: str | None, ended_at: str | None, now: datetime | None
) -> int | None:
    start = _parse_dt(started_at)
    end = _parse_dt(ended_at) or _now(now)
    if start is None or end is None:
        return None
    return int((end - start).total_seconds())


def _oneline(s: str) -> str:
    return " ".join(s.split())


# -- event compaction (shared by tail + events) ------------------------------


def summarize_event(event: dict) -> Styled:
    """Compact one-line summary of a raw event, with a style hint."""
    if event.get("type") == "_unparseable":
        return Styled("<unparseable line>", "red")

    etype = _event_type(event)
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}

    if etype == "system":
        sub = event.get("subtype") or payload.get("subtype") or "system"
        return Styled(f"system: {sub}", "dim")

    if etype == "result":
        sub = payload.get("subtype") or event.get("subtype") or "?"
        ok = sub == "success" and payload.get("is_error") is not True
        bits = [f"result: {sub}"]
        if payload.get("total_cost_usd") is not None:
            bits.append(format_cost(payload["total_cost_usd"]))
        if payload.get("num_turns") is not None:
            bits.append(f"{payload['num_turns']} turns")
        return Styled(" · ".join(bits), "green" if ok else "red")

    if etype == "assistant":
        for text in _text_blocks(event):
            matches = _PROGRESS_RE.findall(text)
            if matches:
                return Styled(f"PROGRESS: {matches[-1]}", "bold cyan")
        tool_uses = [b for b in _blocks(event) if b.get("type") == "tool_use"]
        if tool_uses:
            b0 = tool_uses[0]
            name = b0.get("name") or "tool"
            target = _tool_target(b0.get("input")) or ""
            extra = f" (+{len(tool_uses) - 1} more)" if len(tool_uses) > 1 else ""
            return Styled(_oneline(f"→ {name} {target}") + extra, "dim")
        texts = _text_blocks(event)
        if texts:
            return Styled(_oneline(texts[-1]) or "assistant", "dim")
        return Styled("assistant", "dim")

    if etype == "user":
        results = [b for b in _blocks(event) if b.get("type") == "tool_result"]
        errored = [b for b in results if b.get("is_error") is True]
        if errored:
            snip = _oneline(_content_to_text(errored[0].get("content")))
            return Styled(f"error: {snip}" if snip else "error", "red")
        if results:
            snip = _oneline(_content_to_text(results[0].get("content")))
            return Styled(f"tool_result: {snip}" if snip else "tool_result", "dim")
        texts = _text_blocks(event)
        if texts:
            return Styled(_oneline(texts[-1]) or "user", "dim")
        return Styled("user", "dim")

    return Styled(str(etype or event.get("type") or "?"), "dim")


# -- tables (rich) and TSV (piped) -------------------------------------------


def _cell_text(cell) -> str:
    if isinstance(cell, Styled):
        return cell.text
    if cell is None:
        return ""
    return str(cell)


def _cell_renderable(cell):
    if isinstance(cell, Styled):
        return Text(cell.text, style=cell.style)
    return "" if cell is None else str(cell)


_TSV_SEP = {ord("\t"): " ", ord("\n"): " ", ord("\r"): " "}


def _tsv_cell(cell) -> str:
    """One TSV field: a tab/newline/CR inside a value would split the row, so
    map each to a space — every row stays exactly one physical line."""
    return _cell_text(cell).translate(_TSV_SEP)


def to_tsv(headers: list[str], rows: list[list]) -> str:
    """Plain, decoration-free TSV: a header line then one tab-joined row each."""
    lines = ["\t".join(_tsv_cell(h) for h in headers)]
    for row in rows:
        lines.append("\t".join(_tsv_cell(c) for c in row))
    return "\n".join(lines)


def to_table(headers: list[str], rows: list[list], *, title: str | None = None) -> Table:
    table = Table(title=title, header_style="bold", expand=False)
    for h in headers:
        table.add_column(h, overflow="fold")
    for row in rows:
        table.add_row(*(_cell_renderable(c) for c in row))
    return table


# -- fields projection (--fields) --------------------------------------------


def flatten_one(obj: dict) -> dict:
    """Flatten a dict one level with dotted keys: ``{"a": {"b": 1}, "c": 2}``
    -> ``{"a.b": 1, "c": 2}``. Only the first level of nested dicts expands."""
    out: dict = {}
    for k, v in obj.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[f"{k}.{k2}"] = v2
        else:
            out[k] = v
    return out


def field_names(objects: list[dict]) -> list[str]:
    """Ordered union of flattened field names across objects (for bare
    ``--fields``)."""
    seen: list[str] = []
    for obj in objects:
        for k in flatten_one(obj):
            if k not in seen:
                seen.append(k)
    return seen


def _raw_scalar(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def project_rows(objects: list[dict], fields: list[str]) -> tuple[list[str], list[list]]:
    """Project ``fields`` (dotted, one-level) out of each object as raw values."""
    rows = []
    for obj in objects:
        flat = flatten_one(obj)
        rows.append([_raw_scalar(flat.get(f)) for f in fields])
    return fields, rows


def project_json(objects: list[dict], fields: list[str]) -> list[dict]:
    """The same projection, but preserving raw JSON values for ``--json``."""
    out = []
    for obj in objects:
        flat = flatten_one(obj)
        out.append({f: flat.get(f) for f in fields})
    return out


# -- ls ----------------------------------------------------------------------

LS_HEADERS = ["id", "status", "started", "dur", "project", "activity", "issues", "cost"]


def ls_rows(items: list[dict], now: datetime | None = None) -> tuple[list[str], list[list]]:
    rows = []
    for it in items:
        eff = it.get("effective_status") or "?"
        issues = it.get("issue_count") or 0
        dur = _duration_secs(it.get("started_at"), it.get("ended_at"), now)
        rows.append(
            [
                short_id(it.get("run_id")),
                Styled(eff, STATUS_STYLES.get(eff, "")),
                relative_time(it.get("started_at"), now),
                format_duration(dur),
                project_basename(it.get("cwd")),
                it.get("last_event_type") or "-",
                Styled(str(issues), "red" if issues else "dim"),
                format_cost(it.get("total_cost_usd")),
            ]
        )
    return LS_HEADERS, rows


# -- events ------------------------------------------------------------------

EVENTS_HEADERS = ["seq", "time", "type", "summary"]


def _type_label(event: dict) -> str:
    t = event.get("type") or "?"
    sub = event.get("subtype")
    return f"{t}/{sub}" if sub else str(t)


def events_rows(
    events: list[dict], now: datetime | None = None, *, summary_width: int = 80
) -> tuple[list[str], list[list]]:
    rows = []
    for e in events:
        s = summarize_event(e)
        rows.append(
            [
                str(e.get("seq")),
                relative_time(e.get("ingested_at"), now),
                _type_label(e),
                Styled(truncate(s.text, summary_width), s.style),
            ]
        )
    return EVENTS_HEADERS, rows


# -- costs -------------------------------------------------------------------

COSTS_HEADERS = ["date", "runs", "cost", "turns"]


def costs_rows(body: dict) -> tuple[list[str], list[list]]:
    rows = []
    for b in body.get("buckets", []):
        rows.append(
            [
                b.get("bucket"),
                str(b.get("runs", 0)),
                format_cost(b.get("total_cost_usd")),
                str(b.get("total_turns", 0)),
            ]
        )
    totals = body.get("totals", {})
    rows.append(
        [
            Styled("TOTAL", "bold"),
            Styled(str(totals.get("runs", 0)), "bold"),
            Styled(format_cost(totals.get("total_cost_usd")), "bold"),
            Styled(str(totals.get("total_turns", 0)), "bold"),
        ]
    )
    return COSTS_HEADERS, rows


# -- show --------------------------------------------------------------------


def _kv(label: str, value: str) -> Text:
    t = Text()
    t.append(f"{label:>9}: ", style="dim")
    t.append(value)
    return t


def show_renderables(
    summary: dict,
    lineage: dict,
    *,
    now: datetime | None = None,
    full_prompt: bool = False,
    raw: bool = False,
    prompt_lines: int = 6,
) -> list:
    """A digested one-run view as an ordered list of rich renderables."""
    run = summary.get("run") or {}
    status = summary.get("status") or {}
    activity = summary.get("activity") or {}
    metrics = summary.get("metrics") or {}
    issues = summary.get("issues") or []

    out: list = []

    # header
    eff = status.get("effective_status") or "?"
    out.append(Text(run.get("run_id") or "?", style="bold"))
    out.append(_kv("session", str(run.get("session_id") or "-")))
    out.append(
        Text.assemble(
            ("   status: ", "dim"), (eff, STATUS_STYLES.get(eff, ""))
        )
    )
    dur = format_duration(metrics.get("duration_seconds"))
    started = run.get("started_at") or "-"
    ended = run.get("ended_at") or "-"
    out.append(_kv("started", f"{started}  ({relative_time(run.get('started_at'), now)})"))
    out.append(_kv("ended", str(ended)))
    out.append(_kv("duration", dur))
    out.append(_kv("turns", str(metrics.get("num_turns") if metrics.get("num_turns") is not None else "-")))
    out.append(_kv("cost", format_cost(metrics.get("total_cost_usd"))))
    out.append(_kv("model", str(run.get("model") or "-")))
    out.append(_kv("cwd", str(run.get("cwd") or "-")))
    out.append(_kv("branch", str(run.get("git_branch") or "-")))

    # lineage
    if lineage.get("resumed_from"):
        out.append(Text.assemble(("  resumed from ", "dim"), (lineage["resumed_from"], "cyan")))
    for child in lineage.get("resumed_by", []):
        out.append(Text.assemble(("  resumed by ", "dim"), (child, "cyan")))

    # prompt
    prompt = run.get("prompt") or ""
    lines = prompt.splitlines()
    shown = lines if full_prompt else lines[:prompt_lines]
    body = "\n".join(shown)
    if not full_prompt and len(lines) > prompt_lines:
        body += f"\n… ({len(lines) - prompt_lines} more lines; --full-prompt)"
    out.append(Text("\nprompt:", style="dim"))
    out.append(Text(body or "(none)"))

    # progress + last tool
    progress = activity.get("progress")
    if progress:
        out.append(Text.assemble(("\nPROGRESS: ", "bold cyan"), (progress, "bold cyan")))
    last_tool = activity.get("last_tool")
    if last_tool:
        tgt = last_tool.get("target") or ""
        out.append(Text.assemble(("last tool: ", "dim"), _oneline(f"{last_tool.get('tool')} {tgt}")))

    # issues
    out.append(Text(f"\nissues: {len(issues)}", style="red" if issues else "dim"))
    for iss in issues[:5]:
        cat = iss.get("category") or "?"
        tool = iss.get("tool")
        snip = truncate(_oneline(iss.get("snippet") or ""), 100)
        label = f"  [{cat}]" + (f" {tool}" if tool else "") + f": {snip}"
        out.append(Text(label, style="red"))

    # result text
    result_text = summary.get("result_text")
    if result_text:
        out.append(Text("\nresult:", style="dim"))
        out.append(Text(result_text) if raw else Markdown(result_text))

    return out


# -- tail --------------------------------------------------------------------


def tail_event_line(event: dict, width: int | None = None) -> Styled:
    s = summarize_event(event)
    return Styled(truncate(s.text, width), s.style)


def tail_heartbeat(stalled_seconds: int | None) -> Styled:
    n = stalled_seconds if stalled_seconds is not None else "?"
    return Styled(f"⏳ stalled for {n}s", "yellow")


def tail_summary_line(status: dict, metrics: dict) -> Styled:
    eff = status.get("effective_status") or "?"
    bits = [eff]
    if metrics.get("total_cost_usd") is not None:
        bits.append(format_cost(metrics["total_cost_usd"]))
    if metrics.get("duration_seconds") is not None:
        bits.append(format_duration(metrics["duration_seconds"]))
    return Styled("── " + " · ".join(bits), STATUS_STYLES.get(eff, ""))
