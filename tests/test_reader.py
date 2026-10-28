"""Reading back: verify and tail, including following a growing log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flight_recorder.chain import Recorder
from flight_recorder.cli import main
from flight_recorder.reader import ChainError, entries, follow


def _log(path: Path, n: int, session: str = "s1") -> Recorder:
    rec = Recorder(path, session_id=session, agent="test")
    for i in range(n):
        rec.append("tool.call", flight={"tool": f"t{i}"})
    return rec


def test_entries_yields_every_verified_entry(tmp_path: Path) -> None:
    path = tmp_path / "a.ndjson"
    _log(path, 3).close()
    assert [e["flight"]["tool"] for e in entries(path)] == ["t0", "t1", "t2"]


def test_entries_stops_at_a_tampered_entry(tmp_path: Path) -> None:
    path = tmp_path / "a.ndjson"
    _log(path, 3).close()
    path.write_text(path.read_text(encoding="utf-8").replace('"t1"', '"tX"'), encoding="utf-8")
    seen = []
    with pytest.raises(ChainError) as err:
        for entry in entries(path):
            seen.append(entry["flight"]["tool"])
    assert seen == ["t0"]  # nothing past the break is handed to a consumer
    assert err.value.line == 2


def test_follow_picks_up_new_entries_and_new_files(tmp_path: Path) -> None:
    first = _log(tmp_path / "a.ndjson", 1)
    rounds = iter(range(3))
    later: list[Recorder] = []

    def stop() -> bool:
        step = next(rounds, None)
        if step == 0:
            first.append("tool.call", flight={"tool": "grew"})
        elif step == 1:
            later.append(_log(tmp_path / "b.ndjson", 1, session="s2"))
        return step is None

    tools = [e["flight"]["tool"] for e in follow(tmp_path, poll=0, stop=stop)]
    first.close()
    later[0].close()
    assert tools == ["t0", "grew", "t0"]


def test_follow_waits_for_a_partly_written_line(tmp_path: Path) -> None:
    path = tmp_path / "a.ndjson"
    _log(path, 1).close()
    whole = path.read_text(encoding="utf-8")
    path.write_text(whole[: len(whole) // 2], encoding="utf-8")  # mid-write
    calls = iter([False, True])

    def stop() -> bool:
        path.write_text(whole, encoding="utf-8")  # the write completes
        return next(calls)

    assert len(list(follow(path, poll=0, stop=stop))) == 1


def test_verify_reports_ok_and_broken_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _log(tmp_path / "good.ndjson", 2).close()
    bad = tmp_path / "bad.ndjson"
    _log(bad, 2).close()
    bad.write_text(bad.read_text(encoding="utf-8").replace('"t0"', '"x"'), encoding="utf-8")
    assert main(["verify", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "ok      " in out and "good.ndjson  2 entries" in out
    assert "BROKEN  " in out and "bad.ndjson  line 1: contents changed" in out


def test_verify_json_and_head(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "a.ndjson"
    _log(path, 2).close()
    assert main(["verify", "--json", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] and result["entries"] == 2
    assert main(["verify", "--head", str(path)]) == 0
    assert capsys.readouterr().out.split()[0] == result["head"]


def test_verify_with_no_logs_exits_2(tmp_path: Path) -> None:
    assert main(["verify", str(tmp_path)]) == 2


def test_tail_prints_a_line_per_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "a.ndjson"
    _log(path, 2).close()
    assert main(["tail", str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 and "#1  tool.call  t0" in lines[0]


def test_tail_exits_1_at_a_break(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "a.ndjson"
    _log(path, 2).close()
    path.write_text(path.read_text(encoding="utf-8").replace('"t1"', '"x"'), encoding="utf-8")
    assert main(["tail", "--json", str(path)]) == 1
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1 and "chain broken" in captured.err
