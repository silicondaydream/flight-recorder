"""flight-recorder proxy | verify | tail | import"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from flight_recorder import __version__


def default_state_dir() -> Path:
    """Importer state lives apart from the logs, so ingesting the log directory
    (into Lincoln, say) never reads a database as if it were a log."""
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "flight-recorder"


def default_log_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(os.environ.get("FLIGHT_RECORDER_DIR") or Path(base) / "flight-recorder")


def _proxy(args: argparse.Namespace) -> int:
    from flight_recorder.proxy import Options, run

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("flight-recorder: give the server command after --", file=sys.stderr)
        return 2
    return run(
        Options(
            command=command,
            log_dir=Path(args.log_dir),
            name=args.name or Path(command[0]).name,
            session_id=args.session_id or uuid.uuid4().hex,
            max_payload=args.max_payload,
            redact=args.redact,
            strict=args.strict,
        )
    )


def _verify(args: argparse.Namespace) -> int:
    from flight_recorder.chain import verify_file
    from flight_recorder.reader import logs_in

    logs = [log for p in args.paths for log in logs_in(Path(p))]
    if not logs:
        print("flight-recorder: no logs found", file=sys.stderr)
        return 2
    broken = 0
    for log in logs:
        result = verify_file(log)
        if args.json:
            print(json.dumps({"path": str(log), **dataclasses.asdict(result)}))
        elif args.head:
            print(f"{result.head}  {log}")
        elif result.valid:
            print(f"ok      {log}  {result.entries} entries  head {result.head}")
        else:
            print(f"BROKEN  {log}  line {result.broken_at}: {result.reason}")
        broken += not result.valid
    return 1 if broken else 0


def _line(entry: dict[str, Any]) -> str:
    event, flight = entry.get("event", {}), entry.get("flight", {})
    parts = [entry.get("@timestamp", ""), f"#{event.get('sequence')}", event.get("action", "")]
    if flight.get("tool"):
        parts.append(flight["tool"])
    if event.get("outcome"):
        parts.append(event["outcome"])
    if "duration" in event:
        parts.append(f"{event['duration'] / 1e6:.0f}ms")
    return "  ".join(str(p) for p in parts)


def _tail(args: argparse.Namespace) -> int:
    from flight_recorder.reader import ChainError, entries, follow

    path = Path(args.path)
    stream = follow(path, poll=args.poll) if args.follow else entries(path)
    try:
        for entry in stream:
            print(json.dumps(entry, ensure_ascii=False) if args.json else _line(entry), flush=True)
    except ChainError as exc:
        print(f"flight-recorder: chain broken at {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def _import_devin(args: argparse.Namespace) -> int:
    import time

    from flight_recorder.importers.devin import DevinClient, DevinError, Importer, State, describe
    from flight_recorder.redact import Redactor

    # The key comes from the environment only: a flag would leave it in shell
    # history and in the process list.
    api_key = os.environ.get("DEVIN_API_KEY")
    if not api_key:
        print("flight-recorder: set DEVIN_API_KEY", file=sys.stderr)
        return 2
    log_dir = Path(args.log_dir)
    client = DevinClient(api_key, base_url=args.base_url)
    state = State(Path(args.state) if args.state else default_state_dir() / "devin-state.db")
    importer = Importer(
        client,
        log_dir,
        state,
        redactor=Redactor(args.redact),
        max_payload=args.max_payload,
        dry_run=args.dry_run,
    )
    try:
        while True:
            try:
                summary = importer.run(tags=args.tag or None)
            except DevinError as exc:
                print(f"flight-recorder: {exc}", file=sys.stderr)
                if not args.watch or "DEVIN_API_KEY" in str(exc):
                    return 2
            else:
                for line in summary.planned:
                    print(line)
                if not args.dry_run:
                    print(describe(summary), flush=True)
            if not args.watch:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()
        state.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flight-recorder",
        description="A tamper-evident record of what an AI agent actually did.",
    )
    parser.add_argument("--version", action="version", version=f"flight-recorder {__version__}")
    sub = parser.add_subparsers(dest="command_name", required=True)

    proxy = sub.add_parser(
        "proxy",
        help="run a stdio MCP server, recording every tool call",
        description="Use in an MCP client's config in place of the server command: "
        "flight-recorder proxy [options] -- <server command...>",
    )
    proxy.add_argument(
        "--log-dir",
        default=str(default_log_dir()),
        help="where session logs go (default: $FLIGHT_RECORDER_DIR or "
        "~/.local/share/flight-recorder)",
    )
    proxy.add_argument("--name", help="label for the server in the log (default: command name)")
    proxy.add_argument("--session-id", help="session id to record under (default: random)")
    proxy.add_argument(
        "--max-payload",
        type=int,
        default=65_536,
        metavar="BYTES",
        help="arguments and results larger than this are kept as a preview plus size "
        "and SHA-256 (default: 65536)",
    )
    proxy.add_argument(
        "--redact",
        action="append",
        default=[],
        metavar="REGEX",
        help="also redact matches of REGEX (repeatable)",
    )
    proxy.add_argument(
        "--strict",
        action="store_true",
        help="stop the session rather than forward a tool call that could not be recorded",
    )
    proxy.add_argument("command", nargs=argparse.REMAINDER, help="-- then the server command")
    proxy.set_defaults(func=_proxy)

    verify = sub.add_parser("verify", help="check that logs have not been altered")
    verify.add_argument("paths", nargs="+", help="log files or directories of logs")
    verify.add_argument(
        "--head",
        action="store_true",
        help="print each log's head hash, to anchor somewhere the agent cannot write",
    )
    verify.add_argument("--json", action="store_true", help="one JSON result per log")
    verify.set_defaults(func=_verify)

    tail = sub.add_parser("tail", help="print a log's entries, verifying each one")
    tail.add_argument("path", help="a log file, or a directory of logs")
    tail.add_argument(
        "-f", "--follow", action="store_true", help="keep reading as entries are written"
    )
    tail.add_argument("--json", action="store_true", help="print entries as JSON lines")
    tail.add_argument("--poll", type=float, default=0.5, help="seconds between reads (--follow)")
    tail.set_defaults(func=_tail)

    imp = sub.add_parser("import", help="import sessions from an agent platform's API")
    sources = imp.add_subparsers(dest="source", required=True)
    devin = sources.add_parser(
        "devin",
        help="import Devin sessions (messages, status changes, metadata)",
        description="Reads DEVIN_API_KEY from the environment. One log per session, "
        "extended on every run; already-recorded messages are skipped.",
    )
    devin.add_argument("--log-dir", default=str(default_log_dir()), help="where logs go")
    devin.add_argument(
        "--state",
        help="importer state database (default: ~/.local/state/flight-recorder/devin-state.db)",
    )
    devin.add_argument("--base-url", default="https://api.devin.ai/v1", help="Devin API base URL")
    devin.add_argument(
        "--tag", action="append", default=[], help="only sessions with this tag (repeatable)"
    )
    devin.add_argument("--watch", action="store_true", help="keep polling")
    devin.add_argument(
        "--interval", type=float, default=60.0, help="seconds between polls (default: 60)"
    )
    devin.add_argument(
        "--dry-run", action="store_true", help="print what would be recorded; write nothing"
    )
    devin.add_argument("--max-payload", type=int, default=65_536, metavar="BYTES")
    devin.add_argument("--redact", action="append", default=[], metavar="REGEX")
    devin.set_defaults(func=_import_devin)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
