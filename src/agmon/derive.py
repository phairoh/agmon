"""Pure derivation functions: raw run/event data in, answer dicts out.

Every function here takes plain data (a run row dict, a list of event dicts,
an injected ``now``, a stubbed ``pid_alive``, config values) and returns a
plain dict. No database, filesystem, network, or environment access — so tests
can drive these directly. Deliberately imports neither sqlite3, fastapi, nor os.

An event dict has the same shape the API loads and serves:
``{"seq": int, "type": str|None, "subtype": str|None, "payload": <parsed>}``
where ``payload`` is the parsed raw event (itself carrying "type", "message",
"usage", ...). Content blocks live at ``payload["message"]["content"]``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# effective_status vocabulary (see derive_status): finished, error, interrupted,
# died, stalled, running.

_PERMISSION_RE = re.compile(
    r"permission|not allowed|isn'?t allowed|approval|denied|blocked|"
    r"requires? .*approv|user (?:has not|hasn'?t|did not|didn'?t|denied)",
    re.IGNORECASE,
)
_PROGRESS_RE = re.compile(r"^PROGRESS: (.+)$", re.MULTILINE)


# -- shared helpers ----------------------------------------------------------


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Treat naive timestamps as UTC so arithmetic across sources is consistent.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _payload(event: dict) -> dict:
    p = event.get("payload")
    return p if isinstance(p, dict) else {}


def _event_type(event: dict) -> object:
    t = event.get("type")
    return t if t is not None else _payload(event).get("type")


def _blocks(event: dict) -> list[dict]:
    """Content blocks of a message event, or []."""
    msg = _payload(event).get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text_blocks(event: dict) -> list[str]:
    """Text of every text block, in order. A bare string content counts as one."""
    msg = _payload(event).get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [content]
    out = []
    for b in _blocks(event):
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            out.append(b["text"])
    return out


def _content_to_text(content: object) -> str:
    """Flatten tool_result / result content into a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif isinstance(b.get("content"), str):
                    parts.append(b["content"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


# -- status ------------------------------------------------------------------


def derive_status(
    run: dict,
    last_ingested_at: str | None,
    pid_alive: bool | None,
    now: str,
    stall_seconds: int,
) -> dict:
    """Fold meta status + liveness + recency into a single effective_status.

    - finished                        -> "finished"
    - error, null result_subtype      -> "interrupted" (stream ended with no
                                          result event; the retryable kind)
    - error, non-null result_subtype  -> "error" (the task itself failed)
    - running, pid not alive          -> "died"
    - running, alive, gone quiet past
      stall_seconds                   -> "stalled" (with stalled_seconds)
    - otherwise                       -> "running"
    """
    status = run.get("status")
    result_subtype = run.get("result_subtype")
    out = {"effective_status": "running", "stalled_seconds": None, "pid_alive": pid_alive}

    if status == "finished":
        out["effective_status"] = "finished"
        return out
    if status == "error":
        out["effective_status"] = "interrupted" if result_subtype is None else "error"
        return out
    if status == "running":
        if pid_alive is False:
            out["effective_status"] = "died"
            return out
        started = _parse_dt(last_ingested_at)
        current = _parse_dt(now)
        if started is not None and current is not None:
            elapsed = (current - started).total_seconds()
            if elapsed > stall_seconds:
                out["effective_status"] = "stalled"
                out["stalled_seconds"] = int(elapsed)
                return out
    return out


# -- activity ----------------------------------------------------------------


def derive_activity(events: list[dict]) -> dict:
    """Most recent tool call, assistant text, and self-reported progress."""
    last_tool = None
    last_text = None
    progress = None

    for event in events:
        if _event_type(event) != "assistant":
            continue
        tool_uses = [b for b in _blocks(event) if b.get("type") == "tool_use"]
        if tool_uses:
            block = tool_uses[-1]
            last_tool = {
                "seq": event.get("seq"),
                "tool": block.get("name"),
                "target": _tool_target(block.get("input")),
            }
        texts = _text_blocks(event)
        if texts:
            last_text = texts[-1][:200]

    # progress: latest PROGRESS: line across all text, in event order.
    for event in events:
        for text in _text_blocks(event):
            matches = _PROGRESS_RE.findall(text)
            if matches:
                progress = matches[-1]

    return {"last_tool": last_tool, "last_text": last_text, "progress": progress}


def _tool_target(input_: object) -> str | None:
    if not isinstance(input_, dict):
        return None
    if isinstance(input_.get("file_path"), str):
        target = input_["file_path"]
    elif isinstance(input_.get("command"), str):
        target = input_["command"]
    else:
        target = next((v for v in input_.values() if isinstance(v, str)), None)
    return target[:120] if isinstance(target, str) else None


# -- issues ------------------------------------------------------------------


def derive_issues(events: list[dict]) -> list[dict]:
    """Errored tool results and non-success run results, most recent 50.

    tool names are resolved by matching a tool_result's tool_use_id against the
    tool_use blocks seen earlier in the stream.
    """
    tool_names: dict[str, str] = {}
    issues: list[dict] = []

    for event in events:
        seq = event.get("seq")
        for block in _blocks(event):
            btype = block.get("type")
            if btype == "tool_use":
                tid = block.get("id")
                if isinstance(tid, str):
                    tool_names[tid] = block.get("name")
            elif btype == "tool_result" and block.get("is_error") is True:
                snippet = _content_to_text(block.get("content"))[:200]
                tid = block.get("tool_use_id")
                category = (
                    "permission" if _PERMISSION_RE.search(snippet) else "tool_error"
                )
                issues.append(
                    {
                        "seq": seq,
                        "category": category,
                        "tool": tool_names.get(tid) if isinstance(tid, str) else None,
                        "snippet": snippet,
                    }
                )
        if _event_type(event) == "result":
            payload = _payload(event)
            subtype = payload.get("subtype")
            if subtype != "success":
                snippet = (_content_to_text(payload.get("result")) or str(subtype))[:200]
                issues.append(
                    {
                        "seq": seq,
                        "category": "run_error",
                        "tool": None,
                        "snippet": snippet,
                    }
                )

    return issues[-50:]


# -- metrics -----------------------------------------------------------------


def derive_metrics(run: dict, events: list[dict], now: str) -> dict:
    """Event/tool counts, wall-clock duration, and pass-through cost/usage."""
    tool_counts: dict[str, int] = {}
    usage = None
    for event in events:
        for block in _blocks(event):
            if block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    tool_counts[name] = tool_counts.get(name, 0) + 1
        if _event_type(event) == "result":
            u = _payload(event).get("usage")
            if u is not None:
                usage = u

    started = _parse_dt(run.get("started_at"))
    end = _parse_dt(run.get("ended_at")) or _parse_dt(now)
    duration = (
        int((end - started).total_seconds())
        if started is not None and end is not None
        else None
    )

    return {
        "num_events": len(events),
        "tool_counts": tool_counts,
        "duration_seconds": duration,
        "num_turns": run.get("num_turns"),
        "total_cost_usd": run.get("total_cost_usd"),
        "usage": usage,
    }
