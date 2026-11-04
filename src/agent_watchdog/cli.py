"""The `watchdog` command.

watchdog run         watch an organization's Devin sessions, with the dashboard
watchdog demo        the whole thing against a scripted fake of Devin, no keys
watchdog proxy       record an MCP server's tool calls (put it in front of one)
watchdog tools       the shell/editor MCP server Devin uses through the proxy
watchdog mcp-setup   print what to paste into Devin
watchdog approve | decline | replay | verify | tail | import devin
"""

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

from agent_watchdog import __version__, logcli
from agent_watchdog.engine import State, Watchdog
from agent_watchdog.judge import Judge
from agent_watchdog.logcli import default_log_dir, default_state_dir
from agent_watchdog.settings import Config


def _flag(name: str) -> bool | None:
    value = (os.environ.get(name) or "").strip().lower()
    return None if not value else value not in ("0", "false", "no", "off")


def _judge(config: Config) -> Judge | None:
    """The OpenAI judge, when OPENAI_API_KEY is set. Whether it is asked is a setting."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from agent_watchdog.judge import OpenAIJudge

        return OpenAIJudge(config)
    except ImportError:
        print(
            "watchdog: the AI judge needs `pip install '.[judge]'`; running without it",
            file=sys.stderr,
        )
        return None


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Command-line flags, as settings for this run only (never saved)."""
    out: dict[str, Any] = {}
    env_dry = _flag("WATCHDOG_DRY_RUN")
    if getattr(args, "live", False):
        out["general.dry_run"] = False
    elif getattr(args, "dry_run", False):
        out["general.dry_run"] = True
    elif env_dry is not None:
        out["general.dry_run"] = env_dry
    for flag, key in (
        ("cooldown", "ladder.cooldown_seconds"),
        ("interval", "general.poll_seconds"),
        ("tick", "general.tick_seconds"),
    ):
        if getattr(args, flag, None) is not None:
            out[key] = getattr(args, flag)
    if getattr(args, "tag", None):
        out["general.session_tags"] = ",".join(args.tag)
    model = getattr(args, "judge_model", None) or os.environ.get("WATCHDOG_JUDGE_MODEL")
    if model:
        out["judge.model"], out["judge.enabled"] = model, True
    return out


def _info(**extra: Any) -> dict[str, Any]:
    """What is connected, for the settings panel. Whether a secret is set, never its value."""
    env = os.environ.get
    return {
        "Devin API key": "set" if env("DEVIN_API_KEY") else "not set (DEVIN_API_KEY)",
        "Devin organization": env("DEVIN_ORG_ID") or "from the key",
        "Teams webhook": "set" if env("TEAMS_WEBHOOK_URL") else "not set (TEAMS_WEBHOOK_URL)",
        "OpenAI API key": "set" if env("OPENAI_API_KEY") else "not set (OPENAI_API_KEY)",
        "OpenAI endpoint": env("OPENAI_BASE_URL") or "api.openai.com",
        "Public URL": env("WATCHDOG_PUBLIC_URL") or "not set (WATCHDOG_PUBLIC_URL)",
        "Approval links": "on"
        if env("WATCHDOG_PUBLIC_URL") and env("WATCHDOG_APPROVAL_TOKEN")
        else "off (WATCHDOG_PUBLIC_URL + WATCHDOG_APPROVAL_TOKEN)",
        "MCP log ingest": "on" if env("WATCHDOG_INGEST_TOKEN") else "off (WATCHDOG_INGEST_TOKEN)",
        **extra,
    }


def _hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "127.0.0.1", int(port)


def _public(engine: Watchdog, address: str, log_dir: Path, ingest_token: str) -> tuple[str, Any]:
    """Start the public listener (ingest + approval pages). Returns its URL."""
    from agent_watchdog.ingest import IngestStore
    from agent_watchdog.public import serve_public

    host, port = _hostport(address)
    server = serve_public(
        engine,
        IngestStore(log_dir) if ingest_token else None,
        engine.signer,
        host,
        port,
        ingest_token=ingest_token,
    )
    bound_host, bound_port = server.server_address[:2]
    return f"http://{bound_host!s}:{bound_port}", server


def _signer(public_url: str) -> Any:
    from agent_watchdog.approvals import Signer

    key = os.environ.get("WATCHDOG_APPROVAL_TOKEN", "")
    if not public_url or not key:
        return None
    return Signer(key, public_url)


def _serve(engine: Watchdog, address: str) -> str:
    from agent_watchdog.dashboard import serve

    host, port = _hostport(address)
    server = serve(engine, host, port, token=os.environ.get("WATCHDOG_APPROVAL_TOKEN", ""))
    bound_host, bound_port = server.server_address[:2]
    return f"http://{bound_host!s}:{bound_port}/"


def _run(args: argparse.Namespace) -> int:
    from agent_watchdog.actions import DevinActions, Teams
    from agent_watchdog.devin import DevinClient, Importer
    from agent_watchdog.devin import State as ImportState
    from agent_watchdog.redact import Redactor
    from agent_watchdog.runner import loop, start_feed, start_importer

    api_key = os.environ.get("DEVIN_API_KEY", "")
    org_id = args.org_id or os.environ.get("DEVIN_ORG_ID") or None
    log_dir = Path(args.log_dir)
    settings_path = Path(args.settings or Path(args.state).parent / "settings.json")
    config = Config(settings_path, _overrides(args))
    webhook = os.environ.get("TEAMS_WEBHOOK_URL", "")
    public_url = os.environ.get("WATCHDOG_PUBLIC_URL", "")
    engine = Watchdog(
        config,
        State(Path(args.state)),
        devin=DevinActions(api_key, org_id=org_id, base_url=args.base_url) if api_key else None,
        teams=Teams(webhook) if webhook else None,
        judge=_judge(config),
        record_dir=log_dir / "watchdog",
        signer=_signer(public_url),
        info=_info(**{"Logs": str(log_dir), "Settings file": str(settings_path)}),
    )
    if args.public:
        ingest_token = os.environ.get("WATCHDOG_INGEST_TOKEN", "")
        url, _ = _public(engine, args.public, log_dir, ingest_token)
        print(
            f"watchdog: public listener on {url} (ingest {'on' if ingest_token else 'off'}, "
            f"approval links {'on' if engine.signer else 'off'})",
            file=sys.stderr,
        )
    elif public_url:
        print("watchdog: WATCHDOG_PUBLIC_URL is set but --public is not", file=sys.stderr)
    stop = threading.Event()
    if api_key and not args.no_import:
        state_db = default_state_dir() / "devin-state.db"

        def make() -> Importer:
            client = DevinClient(api_key, org_id=org_id, base_url=args.base_url)
            return Importer(client, log_dir, ImportState(state_db), redactor=Redactor())

        since = int(time.time() - args.since_hours * 3600) if args.since_hours else None
        start_importer(make, config, stop, since=since)
    elif not api_key:
        print("watchdog: no DEVIN_API_KEY: reading logs only, alerts only", file=sys.stderr)
    feed = start_feed(log_dir, stop)
    if args.dashboard:
        print(f"watchdog: dashboard on {_serve(engine, args.dashboard)}", file=sys.stderr)
    mode = "DRY RUN" if config.flag("general.dry_run") else "LIVE"
    print(f"watchdog: {mode}, watching {log_dir}, settings in {settings_path}", file=sys.stderr)
    try:
        loop(engine, feed, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        engine.close()
    return 0


def _demo(args: argparse.Namespace) -> int:
    import secrets

    from agent_watchdog import demo
    from agent_watchdog.actions import DevinActions, Teams
    from agent_watchdog.approvals import Signer
    from agent_watchdog.devin import DevinClient, Importer
    from agent_watchdog.devin import State as ImportState
    from agent_watchdog.runner import loop, start_feed, start_importer

    stop = threading.Event()
    fake, _, base_url = demo.start(args.step, stop)
    work = demo.workdir(args.dir)
    log_dir = work / "logs"
    ingest_token = secrets.token_urlsafe(24)
    browser_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host  # noqa: S104
    # Demo pacing: everything happens in about two minutes. Saved (from the
    # settings panel) to the demo's own directory only.
    config = Config(
        work / "settings.json",
        {
            **_overrides(args),
            "general.dry_run": args.dry_run,
            "general.poll_seconds": max(args.step / 2, 2.0),
            "general.tick_seconds": 1.0,
            "ladder.reset_after_seconds": max(args.cooldown * 2, 10.0),
            "rules.acus_without_progress.acus": 3.0,
            "rules.acus_without_progress.minutes": 1.0,
            "teams.per_session_seconds": 0.0,
            "teams.runtime_minutes": 1.0,
            "teams.acus_threshold": 4.0,
        },
    )
    engine = Watchdog(
        config,
        State(work / "watchdog.db"),
        devin=DevinActions("cog_demo", base_url=base_url),
        teams=Teams(f"{base_url}/teams"),
        judge=_judge(config),
        record_dir=log_dir / "watchdog",
        signer=Signer(secrets.token_urlsafe(24), f"http://{browser_host}:{args.port + 1}"),
        info=_info(
            **{
                "Devin API key": "demo (a local fake of Devin's API)",
                "Teams webhook": "demo (cards go to the fake; see the decisions log)",
                "Approval links": "on (demo)",
                "MCP log ingest": "on (demo)",
                "Logs": str(log_dir),
                "Settings file": str(work / "settings.json"),
            }
        ),
    )

    def make() -> Importer:
        return Importer(
            DevinClient("cog_demo", base_url=base_url), log_dir, ImportState(work / "import.db")
        )

    start_importer(make, config, stop)
    feed = start_feed(log_dir, stop, poll=0.25)
    url = _serve(engine, f"{args.host}:{args.port}")
    public, _ = _public(engine, f"{args.host}:{args.port + 1}", log_dir, ingest_token)
    ingest = f"http://127.0.0.1:{args.port + 1}/api/ingest"
    demo.start_mcp_agent(work, ingest, ingest_token, fake, args.step, stop)
    print(f"watchdog demo: dashboard on {url}", file=sys.stderr)
    print(f"watchdog demo: public listener (ingest, approval pages) on {public}", file=sys.stderr)
    print(f"watchdog demo: logs in {log_dir}", file=sys.stderr)
    runner = threading.Thread(target=loop, args=(engine, feed, stop), daemon=True)
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
        time.sleep(1.0)  # let the MCP agent close its session
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
        state.close()
        return 1
    if args.subcommand == "approve":
        ladder.approved_at, ladder.approved_by = time.time(), getpass.getuser()
        done = "approved terminating"
    else:
        ladder.pending, ladder.declined_by = False, getpass.getuser()
        ladder.last_action = time.time()
        done = "declined terminating"
    state.put(args.session, ladder)
    state.close()
    print(f"watchdog: {done} {args.session}; applied on the next tick")
    return 0


def _tools(args: argparse.Namespace) -> int:
    from agent_watchdog import tools

    return tools.main()


KNOWLEDGE = """\
Watchdog tools (MCP server "watchdog-tools"):
- At the start of every session, before anything else, call `link_session` with this
  session's link (https://app.devin.ai/sessions/...).
- Run every test, build, install, lint and other shell command with `run_command`.
- Make file changes with `edit_file` or `write_file`.
These calls are recorded so that a supervisor can tell when a session is stuck.
"""


def _mcp_setup(args: argparse.Namespace) -> int:
    import json

    config = {
        "transport": "STDIO",
        "command": "watchdog",
        "args": ["proxy", "--name", "watchdog-tools", "--", "watchdog", "tools"],
        "env": {
            "WATCHDOG_INGEST_URL": f"{args.public_url.rstrip('/')}/api/ingest",
            "WATCHDOG_INGEST_TOKEN": "$WATCHDOG_INGEST_TOKEN",
        },
    }
    print("1. Install Watchdog on Devin's machine (environment / blueprint step):\n")
    print(f"   pip install 'git+{args.repo}'\n")
    print("2. Add a custom MCP server (Customize -> MCPs -> custom, STDIO):\n")
    print("   " + json.dumps(config, indent=2).replace("\n", "\n   ") + "\n")
    print(
        "   Store WATCHDOG_INGEST_TOKEN as a Devin secret and reference it here, rather\n"
        "   than pasting the value (see Devin's MCP docs on secrets).\n"
    )
    print("3. Add this as organization Knowledge, so Devin uses the tools:\n")
    print("   " + KNOWLEDGE.replace("\n", "\n   "))
    return 0


def _replay(args: argparse.Namespace) -> int:
    from agent_watchdog.observe import epoch
    from agent_watchdog.reader import ChainError, entries

    clock: list[float] = [0.0]
    engine = Watchdog(
        Config(values={"general.dry_run": True, "ladder.cooldown_seconds": args.cooldown}),
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
    from agent_watchdog.observe import epoch

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
        prog="watchdog",
        description="Watchdog: an MCP proxy and supervisor for AI agent sessions. It records "
        "what agents do, notices when a session is stuck or drifting, and steps in.",
    )
    parser.add_argument("--version", action="version", version=f"watchdog {__version__}")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    logcli.register(sub)
    default_state = str(default_state_dir() / "watchdog.db")

    def common(p: argparse.ArgumentParser, cooldown: float | None) -> None:
        p.add_argument(
            "--cooldown",
            type=float,
            default=cooldown,
            help="seconds between steps, per session (default: the saved setting)",
        )
        p.add_argument(
            "--judge-model",
            help="turn the AI judge on with this OpenAI model; needs OPENAI_API_KEY",
        )

    run = sub.add_parser(
        "run",
        help="watch an organization's Devin sessions, with the dashboard",
        description="Secrets come from the environment: DEVIN_API_KEY (cog_...), DEVIN_ORG_ID, "
        "TEAMS_WEBHOOK_URL, OPENAI_API_KEY, WATCHDOG_PUBLIC_URL, WATCHDOG_APPROVAL_TOKEN, "
        "WATCHDOG_INGEST_TOKEN. Everything else is a setting, changed from the dashboard; "
        "these flags override it for one run.",
    )
    common(run, None)
    run.add_argument("--log-dir", default=str(default_log_dir()))
    run.add_argument("--state", default=default_state, help="Watchdog's SQLite state")
    run.add_argument("--settings", help="settings file (default: settings.json beside --state)")
    run.add_argument("--live", action="store_true", help="act on sessions for this run")
    run.add_argument("--dry-run", action="store_true", help="act on nothing for this run")
    run.add_argument(
        "--no-import", action="store_true", help="only read logs; another process imports"
    )
    run.add_argument("--base-url", default="https://api.devin.ai")
    run.add_argument("--org-id")
    run.add_argument("--tag", action="append", default=[], help="only sessions with this tag")
    run.add_argument(
        "--since-hours",
        type=float,
        default=24.0,
        help="first run: sessions updated in the last N hours (0: all)",
    )
    run.add_argument("--interval", type=float, help="seconds between Devin API polls")
    run.add_argument("--tick", type=float, help="seconds between rule evaluations")
    run.add_argument(
        "--dashboard", default="127.0.0.1:8765", help="HOST:PORT for the dashboard ('' for none)"
    )
    run.add_argument(
        "--public",
        default="",
        help="HOST:PORT for the public listener: MCP log ingest (WATCHDOG_INGEST_TOKEN) and "
        "Teams approval links (WATCHDOG_PUBLIC_URL, WATCHDOG_APPROVAL_TOKEN). Off by default",
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
    demo.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    demo.set_defaults(func=_demo)

    for name, text in (("approve", "allow a pending terminate"), ("decline", "keep it running")):
        decide = sub.add_parser(name, help=text)
        decide.add_argument("session")
        decide.add_argument("--state", default=default_state)
        decide.set_defaults(func=_approve)

    tools = sub.add_parser(
        "tools",
        help="the shell/editor MCP server Devin uses (run it behind `watchdog proxy`)",
        description="A stdio MCP server: run_command, edit_file, write_file, read_file, "
        "link_session. With WATCHDOG_INGEST_URL and WATCHDOG_INGEST_TOKEN set, ships the "
        "proxy's log to Watchdog.",
    )
    tools.set_defaults(func=_tools)

    setup = sub.add_parser("mcp-setup", help="print the Devin MCP config and Knowledge note")
    setup.add_argument("public_url", help="WATCHDOG_PUBLIC_URL: where Devin's machines reach you")
    setup.add_argument(
        "--repo",
        default="https://bitbucket.org/<workspace>/watchdog.git",
        help="the git URL Devin's machines pip install Watchdog from",
    )
    setup.set_defaults(func=_mcp_setup)

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
