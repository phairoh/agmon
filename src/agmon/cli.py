"""`agmon` command-line client: argument parsing and wiring only.

All formatting lives in ``render``; all HTTP in ``client``. ``main`` accepts an
injected client, output writer, and TTY flag so the whole surface runs in tests
with no network and no terminal. Output layering: a TTY gets rich tables (unless
``--plain``); a pipe gets decoration-free TSV; ``--json`` dumps the underlying
object(s); ``--fields a,b`` projects one-level dotted fields (bare ``--fields``
lists the available names).

Read commands (ls/show/tail/events/costs) work from anywhere with ``$AGMON_URL``
set. ``serve`` and ``run`` execute box-side (they touch the local spool/process).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Callable, TextIO

from rich.console import Console
from rich.text import Text

from . import __version__, render
from .client import Client, ClientError, DEFAULT_URL, compute_lineage, resolve

# Sentinel: `--fields` given with no value (list available field names).
FIELDS_LIST = "\x00__list_fields__"

# tail exit-code contract (also recorded in CLAUDE.md).
_TAIL_TERMINAL = {"finished", "error", "interrupted", "died"}
_TAIL_EXIT = {"finished": 0, "error": 1, "interrupted": 1, "died": 3}
_POLL_SECONDS = 2.0


@dataclasses.dataclass
class Ctx:
    client: Client
    out: TextIO
    err: TextIO
    tty: bool
    console: Console
    now: datetime
    sleep: Callable[[float], None]

    def print_styled(self, styled: render.Styled) -> None:
        self.console.print(Text(styled.text, style=styled.style))


# -- shared output paths -----------------------------------------------------


def _print_json(ctx: Ctx, payload) -> None:
    ctx.out.write(json.dumps(payload, indent=2, default=str) + "\n")


def _print_field_names(ctx: Ctx, objects: list[dict]) -> None:
    for name in render.field_names(objects):
        ctx.out.write(name + "\n")


def _emit_rows(ctx: Ctx, args, headers: list[str], rows: list[list]) -> None:
    if ctx.tty and not args.plain:
        ctx.console.print(render.to_table(headers, rows))
    else:
        ctx.out.write(render.to_tsv(headers, rows) + "\n")


def _split_fields(value: str) -> list[str]:
    return [f.strip() for f in value.split(",") if f.strip()]


def _tabular(ctx: Ctx, args, objects: list[dict], default_builder, *, json_payload) -> int:
    """The ls/events/costs output flow: bare-fields, json, projected, default."""
    if args.fields == FIELDS_LIST:
        _print_field_names(ctx, objects)
        return 0
    if args.json:
        _print_json(ctx, json_payload)
        return 0
    if args.fields:
        headers, rows = render.project_rows(objects, _split_fields(args.fields))
    else:
        headers, rows = default_builder()
    _emit_rows(ctx, args, headers, rows)
    return 0


# -- commands ----------------------------------------------------------------


def cmd_ls(args, ctx: Ctx) -> int:
    fetch_limit = 100_000 if (args.all or args.session) else args.n
    items = ctx.client.list_runs(
        status=args.status, limit=fetch_limit, session=args.session
    )
    if not args.all:
        items = items[: args.n]
    return _tabular(
        ctx, args, items, lambda: render.ls_rows(items, ctx.now), json_payload=items
    )


def cmd_show(args, ctx: Ctx) -> int:
    runs = ctx.client.all_runs()
    run_id = resolve(runs, args.id)
    summary = ctx.client.get_summary(run_id)
    if args.fields == FIELDS_LIST:
        _print_field_names(ctx, [summary])
        return 0
    if args.json:
        _print_json(ctx, summary)
        return 0
    if args.fields:
        headers, rows = render.project_rows([summary], _split_fields(args.fields))
        _emit_rows(ctx, args, headers, rows)
        return 0
    lineage = compute_lineage(runs, run_id)
    for renderable in render.show_renderables(
        summary, lineage, now=ctx.now,
        full_prompt=args.full_prompt, raw=args.raw,
    ):
        ctx.console.print(renderable)
    return 0


def cmd_events(args, ctx: Ctx) -> int:
    run_id = ctx.client.resolve_run_id(args.id)
    resp = ctx.client.get_events(
        run_id, after=args.after, limit=args.n, errors_only=args.errors_only
    )
    events = resp.get("events", [])
    if args.type:
        events = [e for e in events if e.get("type") == args.type]
    return _tabular(
        ctx, args, events,
        lambda: render.events_rows(events, ctx.now), json_payload=events,
    )


def cmd_costs(args, ctx: Ctx) -> int:
    if args.since:
        since = args.since
    else:
        since = (ctx.now - _days(args.days)).isoformat()
    body = ctx.client.get_costs(since=since, until=args.until)
    buckets = body.get("buckets", [])
    return _tabular(
        ctx, args, buckets, lambda: render.costs_rows(body), json_payload=body
    )


def cmd_tail(args, ctx: Ctx) -> int:
    run_id = ctx.client.resolve_run_id(args.id)
    cursor = 0
    if args.last is not None:
        n = (ctx.client.get_summary(run_id).get("metrics") or {}).get("num_events") or 0
        cursor = max(0, n - args.last)
    width = ctx.console.width if ctx.tty else None

    while True:
        batch = ctx.client.get_events(run_id, after=cursor, limit=500)
        events = batch.get("events", [])
        for event in events:
            ctx.print_styled(render.tail_event_line(event, width))
        cursor = batch.get("next_after", cursor)

        result_ev = next(
            (e for e in reversed(events) if e.get("type") == "result"), None
        )
        if result_ev is not None:
            summary = ctx.client.get_summary(run_id)
            ctx.print_styled(
                render.tail_summary_line(
                    summary.get("status") or {}, summary.get("metrics") or {}
                )
            )
            payload = result_ev.get("payload") or {}
            subtype = payload.get("subtype") or result_ev.get("subtype")
            return 0 if subtype == "success" else 1

        if not events:
            summary = ctx.client.get_summary(run_id)
            status = summary.get("status") or {}
            eff = status.get("effective_status")
            if eff in _TAIL_TERMINAL:
                ctx.print_styled(
                    render.tail_summary_line(status, summary.get("metrics") or {})
                )
                return _TAIL_EXIT[eff]
            if eff == "stalled":
                ctx.print_styled(render.tail_heartbeat(status.get("stalled_seconds")))

        ctx.sleep(_POLL_SECONDS)


def cmd_serve(args, ctx: Ctx) -> int:
    import uvicorn

    from .api import create_app
    from .config import Config

    config = Config.from_env()
    config = dataclasses.replace(
        config,
        host=args.host or config.host,
        port=args.port or config.port,
    )
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)
    return 0


def cmd_run(args, ctx: Ctx) -> int:
    from . import runner

    runner.main(args.run_args)  # calls sys.exit with the run's exit code
    return 0


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agmon", description="agent-run monitor CLI")
    parser.add_argument("--version", action="version", version=f"agmon {__version__}")
    sub = parser.add_subparsers(dest="command")

    # --url applies to every command that talks to the API.
    url_parent = argparse.ArgumentParser(add_help=False)
    url_parent.add_argument(
        "--url", default=None,
        help=f"server URL (default: $AGMON_URL or {DEFAULT_URL})",
    )
    # shared view flags for the tabular/digest read commands.
    view_parent = argparse.ArgumentParser(add_help=False, parents=[url_parent])
    view_parent.add_argument("--json", action="store_true", help="emit the full underlying object(s)")
    view_parent.add_argument("--plain", action="store_true", help="force plain TSV even on a TTY")
    view_parent.add_argument(
        "--fields", nargs="?", const=FIELDS_LIST, default=None,
        metavar="a,b,c",
        help="project one-level dotted fields; bare --fields lists available names",
    )

    p_ls = sub.add_parser("ls", parents=[view_parent], help="fleet glance, newest first")
    p_ls.add_argument("-n", type=int, default=20, help="max rows (default 20)")
    p_ls.add_argument("--all", action="store_true", help="all runs, no row cap")
    p_ls.add_argument("--status", default=None, help="filter by raw meta status")
    p_ls.add_argument("--session", default=None, help="filter by session id")
    p_ls.set_defaults(func=cmd_ls)

    p_show = sub.add_parser("show", parents=[view_parent], help="one run, digested")
    p_show.add_argument("id", nargs="?", default=None, help="run id (substring); default latest")
    p_show.add_argument("--full-prompt", action="store_true", help="show the whole prompt")
    p_show.add_argument("--raw", action="store_true", help="do not render result as markdown")
    p_show.set_defaults(func=cmd_show)

    p_tail = sub.add_parser("tail", parents=[url_parent], help="live follow a run")
    p_tail.add_argument("id", nargs="?", default=None, help="run id (substring); default latest")
    p_tail.add_argument("--last", type=int, default=None, metavar="N",
                        help="start N events from the end instead of the beginning")
    p_tail.add_argument("--plain", action="store_true", help="no color")
    p_tail.set_defaults(func=cmd_tail)

    p_events = sub.add_parser("events", parents=[view_parent], help="raw event forensics")
    p_events.add_argument("id", nargs="?", default=None, help="run id (substring); default latest")
    p_events.add_argument("-n", type=int, default=200, help="max events (default 200)")
    p_events.add_argument("--after", type=int, default=0, help="only events with seq > AFTER")
    p_events.add_argument("--errors-only", action="store_true", help="only error-flagged events")
    p_events.add_argument("--type", default=None, help="filter by event type")
    p_events.set_defaults(func=cmd_events)

    p_costs = sub.add_parser("costs", parents=[view_parent], help="cost/turn rollup")
    p_costs.add_argument("--days", type=int, default=30, help="window size in days (default 30)")
    p_costs.add_argument("--since", default=None, help="ISO lower bound (overrides --days)")
    p_costs.add_argument("--until", default=None, help="ISO upper bound")
    p_costs.set_defaults(func=cmd_costs)

    p_serve = sub.add_parser("serve", help="start the collector + API")
    p_serve.add_argument("--host", default=None, help="override AGMON_HOST")
    p_serve.add_argument("--port", type=int, default=None, help="override AGMON_PORT")
    p_serve.set_defaults(func=cmd_serve)

    # add_help=False so `agmon run --help`/`-h` flows through to the wrapper's
    # own parser (which owns the real flag set) rather than a passthrough stub.
    p_run = sub.add_parser(
        "run", add_help=False, help="launch and spool a headless Claude run"
    )
    p_run.add_argument("run_args", nargs=argparse.REMAINDER)
    p_run.set_defaults(func=cmd_run)

    return parser


def _resolve_tty(out: TextIO, tty: bool | None) -> bool:
    if tty is not None:
        return tty
    isatty = getattr(out, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def main(
    argv: list[str] | None = None,
    *,
    client: Client | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    tty: bool | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # `run` is a verbatim passthrough to the wrapper's own parser; intercept it
    # before argparse (whose REMAINDER mishandles a leading --help).
    if raw and raw[0] == "run":
        from . import runner

        runner.main(raw[1:])  # calls sys.exit with the run's exit code
        return 0

    parser = build_parser()
    args = parser.parse_args(raw)
    out = out or sys.stdout
    err = err or sys.stderr
    if not getattr(args, "func", None):
        parser.print_help(out)
        return 0

    tty_resolved = _resolve_tty(out, tty)
    url = getattr(args, "url", None) or os.environ.get("AGMON_URL") or DEFAULT_URL
    ctx = Ctx(
        client=client if client is not None else Client(url),
        out=out,
        err=err,
        tty=tty_resolved,
        console=Console(file=out, force_terminal=tty_resolved or None, no_color=not tty_resolved),
        now=now or datetime.now(timezone.utc),
        sleep=sleep or time.sleep,
    )
    try:
        return args.func(args, ctx) or 0
    except ClientError as exc:
        err.write(f"agmon: {exc}\n")
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
