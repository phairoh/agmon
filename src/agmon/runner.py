"""`agmon run` — launch and spool a headless Claude run.

A verbatim port of the stage-0 `agmon-run` wrapper (formerly installed at
~/.local/bin/agmon-run): launches `claude -p` with stream-json output, tees
every event line to $AGENT_RUNS_DIR/<run_id>.jsonl, and maintains
<run_id>.meta.json as the run's durable record. The spool files are the source
of truth for the whole monitoring system — the collector/API/CLI only read them.

Behaviour is unchanged from the original script; the only refactor is pulling
the argument parser out into ``build_parser`` so it can be smoke-tested.

Prints the run_id on stdout immediately after launch so callers can capture it:
  id=$(agmon run @task.md --cwd ~/src/proj)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .labels import build_labels

RUNS_DIR = Path(os.environ.get("AGENT_RUNS_DIR", "~/agent-runs")).expanduser()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    # Time-sortable and collision-safe enough for one box.
    return time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)


def git_info(cwd: Path) -> dict:
    def _git(*args):
        try:
            out = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
            )
            return out.stdout.strip() if out.returncode == 0 else None
        except Exception:
            return None

    return {
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git("rev-parse", "--short", "HEAD"),
    }


def write_meta(path: Path, meta: dict) -> None:
    """Atomic write so readers (the ingester) never see a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(path)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="agmon run", description="Launch and spool a headless Claude run"
    )
    ap.add_argument(
        "prompt",
        help="prompt text, or @path/to/file to read the prompt from a file",
    )
    ap.add_argument("--cwd", default=".", help="working directory for the run")
    ap.add_argument("--model", default=None)
    ap.add_argument(
        "--permission-mode",
        default=None,
        help="e.g. acceptEdits, bypassPermissions, plan",
    )
    ap.add_argument(
        "--allowed-tools",
        default=None,
        help='passed through as --allowedTools, e.g. "Read Edit Write Bash(git *)"',
    )
    ap.add_argument(
        "--resume",
        default=None,
        metavar="SESSION_ID",
        help="resume a prior session by id",
    )
    ap.add_argument("--max-turns", type=int, default=None)
    ap.add_argument("--max-budget-usd", type=float, default=None)
    ap.add_argument(
        "--bare",
        action="store_true",
        help="skip auto-discovery of hooks/skills/MCP/CLAUDE.md (CI-style runs)",
    )
    ap.add_argument(
        "--label",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="stamp a label (repeatable); keys [a-z0-9_.-]{1,64}, values "
        "non-empty printable ≤256 chars, ≤16 labels, no duplicate keys",
    )
    ap.add_argument("--pipeline", default=None, help="sugar for --label pipeline=X")
    ap.add_argument("--phase", default=None, help="sugar for --label phase=Y")
    ap.add_argument("--parent", default=None, metavar="RUN_ID",
                    help="sugar for --label parent=RUN_ID")
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    try:
        labels = build_labels(
            args.label, pipeline=args.pipeline, phase=args.phase, parent=args.parent
        )
    except ValueError as exc:
        sys.exit(f"error: {exc}")

    prompt = args.prompt
    if prompt.startswith("@"):
        prompt = Path(prompt[1:]).expanduser().read_text()

    cwd = Path(args.cwd).expanduser().resolve()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    run_id = new_run_id()
    jsonl_path = RUNS_DIR / f"{run_id}.jsonl"
    meta_path = RUNS_DIR / f"{run_id}.meta.json"
    stderr_path = RUNS_DIR / f"{run_id}.stderr.log"

    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if args.bare:
        cmd.append("--bare")
    if args.model:
        cmd += ["--model", args.model]
    if args.permission_mode:
        cmd += ["--permission-mode", args.permission_mode]
    if args.allowed_tools:
        cmd += ["--allowedTools", args.allowed_tools]
    if args.resume:
        cmd += ["--resume", args.resume]
    if args.max_turns is not None:
        cmd += ["--max-turns", str(args.max_turns)]
    if args.max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(args.max_budget_usd)]

    PROGRESS_INSTRUCTION = (
        "You are running under a monitoring system. At each meaningful milestone "
        "(completing a phase, starting a distinct sub-task, a discovery that changes "
        "your plan), include one line in your response text, at the start of a line, "
        "of exactly this form: PROGRESS: <one short present-tense sentence>. "
        "At most one per turn. Do not emit them for routine tool calls."
    )

    cmd += ["--append-system-prompt", PROGRESS_INSTRUCTION]

    meta = {
        "run_id": run_id,
        "prompt": prompt,
        "argv": cmd[:1] + ["<prompt omitted>"] + cmd[3:],  # avoid duplicating long prompts
        "cwd": str(cwd),
        "git": git_info(cwd),
        "permission_mode": args.permission_mode,
        "host": os.uname().nodename,
        "session_id": None,
        "pid": None,
        "started_at": now_iso(),
        "ended_at": None,
        "exit_code": None,
        "status": "running",        # running | finished | error
        "result_subtype": None,     # success | error_max_turns | error_during_execution | ...
        "num_turns": None,
        "total_cost_usd": None,
        "labels": labels,          # flat key=value facts; meaning lives in derivation
    }
    if args.model:
        # Requested intent only; the observed model is derived at ingest from
        # the init event. Meta "model" is retired — never write it.
        meta["model_requested"] = args.model

    try:
        with open(jsonl_path, "w") as spool, open(stderr_path, "w") as errlog:
            env = {**os.environ,
                "GIT_AUTHOR_NAME": "agmon agent", "GIT_AUTHOR_EMAIL": "agent@agmon.local"}
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=errlog,
                text=True, bufsize=1, env=env,
            )
            meta["pid"] = proc.pid
            write_meta(meta_path, meta)
            print(run_id, flush=True)

            # Forward signals so `kill <wrapper pid>` cleanly stops the run.
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda s, _f: proc.send_signal(s))

            for line in proc.stdout:
                spool.write(line)
                spool.flush()  # keep `tail -f` and the ingester live
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "system" and event.get("subtype") == "init":
                    meta["session_id"] = event.get("session_id")
                    write_meta(meta_path, meta)
                elif etype == "result":
                    meta["result_subtype"] = event.get("subtype")
                    meta["num_turns"] = event.get("num_turns")
                    meta["total_cost_usd"] = event.get("total_cost_usd")
                    meta["session_id"] = event.get("session_id") or meta["session_id"]

            exit_code = proc.wait()
    except FileNotFoundError:
        meta.update(status="error", ended_at=now_iso(), exit_code=127,
                    result_subtype="claude_not_found")
        write_meta(meta_path, meta)
        sys.exit("error: `claude` not found on PATH")

    meta["exit_code"] = exit_code
    meta["ended_at"] = now_iso()
    ok = exit_code == 0 and meta.get("result_subtype") == "success"
    meta["status"] = "finished" if ok else "error"
    write_meta(meta_path, meta)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
