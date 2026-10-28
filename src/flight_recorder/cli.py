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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
