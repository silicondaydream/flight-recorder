"""watchdog run | demo | approve | replay"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from devin_watchdog.engine import Settings, State, Watchdog
from devin_watchdog.judge import DEFAULT_MODEL, Judge
from devin_watchdog.rules import Thresholds
from flight_recorder.cli import default_log_dir, default_state_dir


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() not in ("0", "false", "no", "off")


def _judge(model: str | None) -> Judge | None:
    if not model:
        return None
    try:
        from devin_watchdog.judge import ClaudeJudge

        return ClaudeJudge(model)
    except ImportError:
        print(
            "watchdog: the judge needs `pip install '.[judge]'`; running without it",
            file=sys.stderr,
        )
        return None


def _hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "127.0.0.1", int(port)


def _serve(engine: Watchdog, address: str) -> str:
    from devin_watchdog.dashboard import serve

    host, port = _hostport(address)
    server = serve(engine, host, port, token=os.environ.get("WATCHDOG_APPROVAL_TOKEN", ""))
    bound_host, bound_port = server.server_address[:2]
    return f"http://{bound_host!s}:{bound_port}/"


def _run(args: argparse.Namespace) -> int:
    from devin_watchdog.actions import DevinActions, Teams
    from devin_watchdog.runner import loop, start_feed, start_importer
    from flight_recorder.importers.devin import DevinClient, Importer
    from flight_recorder.importers.devin import State as ImportState
    from flight_recorder.redact import Redactor

    api_key = os.environ.get("DEVIN_API_KEY", "")
    org_id = args.org_id or os.environ.get("DEVIN_ORG_ID") or None
    log_dir = Path(args.log_dir)
    dry_run = not args.live and _flag("WATCHDOG_DRY_RUN", True)
    settings = Settings(
        dry_run=dry_run,
        cooldown=args.cooldown,
        reset_after=args.reset_after,
        off_tag=args.off_tag,
        thresholds=Thresholds(),
    )
    webhook = os.environ.get("TEAMS_WEBHOOK_URL", "")
    engine = Watchdog(
        settings,
        State(Path(args.state)),
        devin=DevinActions(api_key, org_id=org_id, base_url=args.base_url) if api_key else None,
        teams=Teams(webhook) if webhook else None,
        judge=_judge(args.judge_model or os.environ.get("WATCHDOG_JUDGE_MODEL")),
        record_dir=log_dir / "watchdog",
    )
    stop = threading.Event()
    if api_key and not args.no_import:
        state_db = default_state_dir() / "devin-state.db"

        def make() -> Importer:
            client = DevinClient(api_key, org_id=org_id, base_url=args.base_url)
            return Importer(client, log_dir, ImportState(state_db), redactor=Redactor())

        since = int(time.time() - args.since_hours * 3600) if args.since_hours else None
        start_importer(make, args.interval, stop, since=since, tags=args.tag or None)
    elif not api_key:
        print("watchdog: no DEVIN_API_KEY: reading logs only, alerts only", file=sys.stderr)
    feed = start_feed(log_dir, stop)
    if args.dashboard:
        print(f"watchdog: dashboard on {_serve(engine, args.dashboard)}", file=sys.stderr)
    print(f"watchdog: {'DRY RUN' if dry_run else 'LIVE'}, watching {log_dir}", file=sys.stderr)
    try:
        loop(engine, feed, stop, tick=args.tick)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        engine.close()
    return 0


def _demo(args: argparse.Namespace) -> int:
    from devin_watchdog import demo
    from devin_watchdog.actions import DevinActions, Teams
    from devin_watchdog.runner import loop, start_feed, start_importer
    from flight_recorder.importers.devin import DevinClient, Importer
    from flight_recorder.importers.devin import State as ImportState

    stop = threading.Event()
    fake, _, base_url = demo.start(args.step, stop)
    work = demo.workdir(args.dir)
    log_dir = work / "logs"
    settings = Settings(
        dry_run=args.dry_run,
        cooldown=args.cooldown,
        reset_after=args.cooldown * 2,
        thresholds=Thresholds(acu_budget=3.0, stall_minutes=1.0),
    )
    engine = Watchdog(
        settings,
        State(work / "watchdog.db"),
        devin=DevinActions("cog_demo", base_url=base_url),
        teams=Teams(f"{base_url}/teams"),
        judge=_judge(args.judge_model),
        record_dir=log_dir / "watchdog",
    )

    def make() -> Importer:
        return Importer(
            DevinClient("cog_demo", base_url=base_url), log_dir, ImportState(work / "import.db")
        )

    start_importer(make, args.step / 2, stop)
    feed = start_feed(log_dir, stop, poll=0.25)
    url = _serve(engine, f"{args.host}:{args.port}")
    print(f"watchdog demo: dashboard on {url}", file=sys.stderr)
    print(f"watchdog demo: logs in {log_dir}", file=sys.stderr)
    runner = threading.Thread(target=loop, args=(engine, feed, stop, 1.0), daemon=True)
    runner.start()
    try:
        deadline = time.monotonic() + args.duration if args.duration else None
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        runner.join(timeout=5)
        engine.close()
    sent = ", ".join(f"{sid}: {text[:60]}" for sid, text in fake.sent) or "none"
    print(f"watchdog demo: messages sent to sessions: {sent}", file=sys.stderr)
    print(f"watchdog demo: Teams cards posted: {len(fake.teams)}", file=sys.stderr)
    return 0


def _approve(args: argparse.Namespace) -> int:
    state = State(Path(args.state))
    ladder = state.get(args.session)
    if not ladder.pending:
        print(f"watchdog: {args.session} is not waiting for approval", file=sys.stderr)
        return 1
    ladder.approved_at, ladder.approved_by = time.time(), getpass.getuser()
    state.put(args.session, ladder)
    state.close()
    print(f"watchdog: approved terminating {args.session}; applied on the next tick")
    return 0


def _replay(args: argparse.Namespace) -> int:
    from devin_watchdog.observe import epoch
    from flight_recorder.reader import ChainError, entries

    clock: list[float] = [0.0]
    engine = Watchdog(
        Settings(dry_run=True, cooldown=args.cooldown),
        State(Path(args.state) if args.state else Path(":memory:")),
        clock=lambda: clock[0],
    )
    try:
        stream = sorted(entries(Path(args.path)), key=epoch)
    except ChainError as exc:
        print(f"watchdog: chain broken at {exc}", file=sys.stderr)
        return 1
    with contextlib.suppress(BrokenPipeError):  # `| head` closed the pipe
        _print_replay(engine, stream, clock)
    return 0


def _print_replay(engine: Watchdog, stream: list[dict[str, Any]], clock: list[float]) -> None:
    from devin_watchdog.observe import epoch

    for entry in stream:
        clock[0] = max(clock[0], epoch(entry))
        engine.observe(entry)
        engine.tick()
        for event in engine.events:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(event.at))
            rules = ",".join(event.rules)
            print(
                f"{when}  {event.session_id}  {event.kind:<9} {event.rung:<9} "
                f"{rules}  {event.detail}"
            )
        engine.events.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="watchdog", description="Notices stuck or drifting Devin sessions, and acts."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    default_state = str(default_state_dir() / "watchdog.db")

    def common(p: argparse.ArgumentParser, cooldown: float) -> None:
        p.add_argument(
            "--cooldown", type=float, default=cooldown, help="seconds between rungs, per session"
        )
        p.add_argument(
            "--judge-model",
            help=f"ask an LLM judge before each step, with this model (e.g. {DEFAULT_MODEL}); "
            "needs ANTHROPIC_API_KEY",
        )

    run = sub.add_parser(
        "run",
        help="poll Devin, watch the logs, escalate",
        description="Environment: DEVIN_API_KEY (cog_...), DEVIN_ORG_ID, TEAMS_WEBHOOK_URL, "
        "WATCHDOG_DRY_RUN (default 1), WATCHDOG_JUDGE_MODEL, WATCHDOG_APPROVAL_TOKEN, "
        "ANTHROPIC_API_KEY.",
    )
    common(run, 600.0)
    run.add_argument("--log-dir", default=str(default_log_dir()))
    run.add_argument("--state", default=default_state, help="Watchdog's SQLite state")
    run.add_argument(
        "--live", action="store_true", help="act on sessions (overrides WATCHDOG_DRY_RUN)"
    )
    run.add_argument(
        "--no-import", action="store_true", help="only read logs; another process imports"
    )
    run.add_argument("--base-url", default="https://api.devin.ai")
    run.add_argument("--org-id")
    run.add_argument("--tag", action="append", default=[], help="only sessions with this tag")
    run.add_argument("--since-hours", type=float, default=24.0)
    run.add_argument("--interval", type=float, default=60.0, help="seconds between API polls")
    run.add_argument("--tick", type=float, default=5.0, help="seconds between rule evaluations")
    run.add_argument("--reset-after", type=float, default=1800.0)
    run.add_argument("--off-tag", default="watchdog:off")
    run.add_argument(
        "--dashboard", default="127.0.0.1:8765", help="HOST:PORT for the dashboard ('' for none)"
    )
    run.set_defaults(func=_run)

    demo = sub.add_parser("demo", help="the full pipeline against a scripted fake Devin")
    common(demo, 12.0)
    demo.add_argument("--host", default="127.0.0.1", help="dashboard address (0.0.0.0 in Docker)")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--step", type=float, default=4.0, help="seconds between scripted steps")
    demo.add_argument("--duration", type=float, default=0, help="stop after N seconds (0: never)")
    demo.add_argument("--dir", help="where to keep the demo's logs (default: a temp dir)")
    demo.add_argument("--dry-run", action="store_true", help="hold back nudges and sleeps")
    demo.set_defaults(func=_demo)

    approve = sub.add_parser("approve", help="allow a pending terminate")
    approve.add_argument("session")
    approve.add_argument("--state", default=default_state)
    approve.set_defaults(func=_approve)

    replay = sub.add_parser("replay", help="run the rules over recorded logs; act on nothing")
    replay.add_argument("path", help="a log file or a directory of logs")
    replay.add_argument("--cooldown", type=float, default=600.0)
    replay.add_argument("--state", help="(default: in memory)")
    replay.set_defaults(func=_replay)

    args = parser.parse_args(argv)
    result: Any = args.func(args)
    return int(result)


if __name__ == "__main__":
    sys.exit(main())
