"""The proxy end to end, through a real child process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from flight_recorder.chain import verify_file

FAKE = str(Path(__file__).with_name("fake_server.py"))


def _req(rid: Any, method: str, **params: Any) -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n"
    ).encode()


SESSION = b"".join(
    [
        _req(
            0,
            "initialize",
            protocolVersion="2025-06-18",
            clientInfo={"name": "test-client", "version": "9"},
        ),
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
        _req(1, "tools/list"),
        _req(2, "tools/call", name="echo", arguments={"text": "hello"}),
        _req("a", "tools/call", name="fail", arguments={}),
        _req(3, "tools/call", name="tool_error", arguments={}),
    ]
)


def _run(
    tmp_path: Path, stdin: bytes, *extra: str, exit_code: int = 0
) -> tuple[subprocess.CompletedProcess[bytes], Path]:
    log_dir = tmp_path / "logs"
    env = {**os.environ, "FAKE_EXIT": str(exit_code)}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "flight_recorder.cli",
            "proxy",
            "--log-dir",
            str(log_dir),
            "--name",
            "fake",
            *extra,
            "--",
            sys.executable,
            FAKE,
        ],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    logs = sorted(log_dir.glob("*.ndjson"))
    assert len(logs) == 1
    return proc, logs[0]


def _entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_relay_is_byte_for_byte(tmp_path: Path) -> None:
    direct = subprocess.run([sys.executable, FAKE], input=SESSION, capture_output=True, check=True)
    proxied, _ = _run(tmp_path, SESSION)
    assert proxied.returncode == 0
    assert proxied.stdout == direct.stdout


def test_calls_and_results_are_paired_and_the_log_verifies(tmp_path: Path) -> None:
    _, log = _run(tmp_path, SESSION)
    assert verify_file(log).valid
    entries = _entries(log)
    actions = [e["event"]["action"] for e in entries]
    # The two relay directions write concurrently, so entries are in the order
    # they were written, not request/response lockstep. What must hold: the
    # session opens and closes the log, and each result follows its own call.
    assert actions[0] == "session.start" and actions[-1] == "session.end"
    assert sorted(actions[1:-1]) == sorted(
        ["session.initialize", "session.initialized"] + ["tool.call", "tool.result"] * 3
    )
    seq = {
        (e["event"]["action"], e["flight"].get("request_id")): e["event"]["sequence"]
        for e in entries
    }
    for rid in ("2", '"a"', "3"):
        assert seq[("tool.call", rid)] < seq[("tool.result", rid)]
    results = [e for e in entries if e["event"]["action"] == "tool.result"]
    assert [r["flight"]["tool"] for r in results] == ["echo", "fail", "tool_error"]
    assert [r["event"]["outcome"] for r in results] == ["success", "failure", "failure"]
    assert all(r["event"]["duration"] >= 0 for r in results)
    echo = results[0]["flight"]["result"]["value"]
    assert echo["content"][0]["text"] == "hello"


def test_initialize_records_both_sides(tmp_path: Path) -> None:
    _, log = _run(tmp_path, SESSION)
    entries = {e["event"]["action"]: e for e in _entries(log)}
    assert entries["session.initialize"]["flight"]["client"]["name"] == "test-client"
    assert entries["session.initialized"]["flight"]["server_info"]["name"] == "fake"


def test_ids_of_different_types_are_not_confused(tmp_path: Path) -> None:
    stdin = _req(1, "tools/call", name="echo", arguments={"text": "num"}) + _req(
        "1", "tools/call", name="fail", arguments={}
    )
    _, log = _run(tmp_path, stdin)
    results = [e for e in _entries(log) if e["event"]["action"] == "tool.result"]
    assert {(r["flight"]["request_id"], r["flight"]["tool"]) for r in results} == {
        ("1", "echo"),
        ('"1"', "fail"),
    }


def test_a_line_that_is_not_json_is_relayed_and_noted(tmp_path: Path) -> None:
    proc, log = _run(tmp_path, _req(5, "tools/call", name="noise", arguments={}))
    assert b"this is not json\n" in proc.stdout
    entries = _entries(log)
    errors = [e for e in entries if e["event"]["action"] == "recorder.error"]
    assert len(errors) == 1 and errors[0]["flight"]["side"] == "server"
    assert verify_file(log).valid


def test_secrets_in_arguments_never_reach_the_log(tmp_path: Path) -> None:
    token = "ghp_" + "z" * 36
    proc, log = _run(tmp_path, _req(6, "tools/call", name="echo", arguments={"text": token}))
    assert token.encode() in proc.stdout  # relayed unchanged to the client
    assert token not in log.read_text(encoding="utf-8")  # but not recorded


def test_a_big_result_is_kept_as_a_preview(tmp_path: Path) -> None:
    _, log = _run(
        tmp_path, _req(7, "tools/call", name="big", arguments={"n": 5000}), "--max-payload", "256"
    )
    result = next(e for e in _entries(log) if e["event"]["action"] == "tool.result")
    kept = result["flight"]["result"]
    assert kept["truncated"] is True and kept["bytes"] > 5000


def test_the_servers_exit_code_is_returned_and_recorded(tmp_path: Path) -> None:
    proc, log = _run(tmp_path, b"", exit_code=3)
    assert proc.returncode == 3
    end = _entries(log)[-1]
    assert end["event"]["action"] == "session.end"
    assert end["event"]["outcome"] == "failure" and end["flight"]["exit_code"] == 3


def test_a_call_left_unanswered_is_named_at_the_end(tmp_path: Path) -> None:
    stdin = _req(9, "tools/call", name="hang", arguments={}) + _req(
        10, "tools/call", name="echo", arguments={"text": "ok"}
    )
    _, log = _run(tmp_path, stdin)
    assert _entries(log)[-1]["flight"]["unanswered"] == ["hang"]


def test_a_server_that_cannot_start_is_recorded(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "flight_recorder.cli",
            "proxy",
            "--log-dir",
            str(log_dir),
            "--",
            "/nonexistent/server",
        ],
        input=b"",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 127
    (log,) = log_dir.glob("*.ndjson")
    assert _entries(log)[-1]["event"]["outcome"] == "failure"
    assert verify_file(log).valid


def test_without_a_command_it_says_so(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "flight_recorder.cli", "proxy", "--log-dir", str(tmp_path)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2 and b"after --" in proc.stderr


class _BrokenRecorder:
    def append(self, *_: Any, **__: Any) -> None:
        raise OSError("disk full")


def _proxy(strict: bool) -> Any:
    from flight_recorder.proxy import Options, Proxy

    options = Options(command=["x"], log_dir=Path("."), name="x", session_id="s", strict=strict)
    return Proxy(options, _BrokenRecorder())  # type: ignore[arg-type]


def test_strict_refuses_to_forward_a_call_it_could_not_record() -> None:
    from flight_recorder.proxy import _StrictError

    proxy = _proxy(strict=True)
    try:
        proxy.from_client(_req(1, "tools/call", name="echo", arguments={}))
    except _StrictError:
        pass
    else:
        raise AssertionError("the call would have been forwarded")
    assert proxy.failed.is_set()


def test_without_strict_a_failed_write_is_survived(capsys: Any) -> None:
    proxy = _proxy(strict=False)
    proxy.from_client(_req(1, "tools/call", name="echo", arguments={}))
    proxy.from_client(_req(2, "tools/call", name="echo", arguments={}))
    assert not proxy.failed.is_set()
    assert capsys.readouterr().err.count("could not write the log") == 1  # warned once
