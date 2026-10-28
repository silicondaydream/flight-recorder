"""flight-recorder proxy | verify | tail | import"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
