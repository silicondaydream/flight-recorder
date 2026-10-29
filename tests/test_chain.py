"""The chain's contract: any edit, removal or reordering is caught."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from flight_recorder.chain import GENESIS, Recorder, canonical, entry_hash, safe_name, verify_file


def _log(tmp_path: Path, n: int = 4) -> Path:
    path = tmp_path / "session.ndjson"
    with Recorder(path, session_id="s1", agent="test") as rec:
        for i in range(n):
            rec.append("tool.call", flight={"tool": f"t{i}", "arguments": {"i": i}})
    return path


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def test_a_fresh_log_verifies(tmp_path: Path) -> None:
    result = verify_file(_log(tmp_path))
    assert result.valid and result.entries == 4 and result.head != GENESIS


def test_the_first_entry_links_to_genesis(tmp_path: Path) -> None:
    first = json.loads(_lines(_log(tmp_path))[0])
    assert first["chain"]["previous"] == GENESIS
    assert first["event"]["sequence"] == 1


def test_an_edited_entry_is_caught(tmp_path: Path) -> None:
    path = _log(tmp_path)
    lines = _lines(path)
    lines[1] = lines[1].replace('"t1"', '"rm -rf /"')
    path.write_text("".join(lines), encoding="utf-8")
    result = verify_file(path)
    assert not result.valid and result.broken_at == 2
    assert "changed" in (result.reason or "")


def test_an_edit_that_also_rehashes_the_entry_breaks_the_next_link(tmp_path: Path) -> None:
    path = _log(tmp_path)
    lines = _lines(path)
    entry = json.loads(lines[1])
    entry["flight"]["tool"] = "forged"
    entry["chain"]["hash"] = entry_hash(entry)
    lines[1] = json.dumps(entry) + "\n"
    path.write_text("".join(lines), encoding="utf-8")
    result = verify_file(path)
    assert not result.valid and result.broken_at == 3


def test_a_removed_entry_is_caught(tmp_path: Path) -> None:
    path = _log(tmp_path)
    lines = _lines(path)
    del lines[1]
    path.write_text("".join(lines), encoding="utf-8")
    result = verify_file(path)
    assert not result.valid and result.broken_at == 2


def test_reordered_entries_are_caught(tmp_path: Path) -> None:
    path = _log(tmp_path)
    lines = _lines(path)
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("".join(lines), encoding="utf-8")
    assert not verify_file(path).valid


def test_a_line_that_is_not_an_entry_is_caught(tmp_path: Path) -> None:
    path = _log(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    result = verify_file(path)
    assert not result.valid and result.broken_at == 5


def test_a_truncated_tail_still_verifies_which_is_why_the_head_is_reported(
    tmp_path: Path,
) -> None:
    # The documented limit: a shorter chain is a valid chain. The head hash,
    # anchored elsewhere, is what tells the two apart.
    path = _log(tmp_path)
    full_head = verify_file(path).head
    path.write_text("".join(_lines(path)[:2]), encoding="utf-8")
    result = verify_file(path)
    assert result.valid and result.entries == 2 and result.head != full_head


def test_reopening_continues_the_chain(tmp_path: Path) -> None:
    path = _log(tmp_path, n=2)
    with Recorder(path, session_id="s1", agent="test") as rec:
        rec.append("tool.call")
    result = verify_file(path)
    assert result.valid and result.entries == 3


def test_reopening_a_broken_chain_is_refused(tmp_path: Path) -> None:
    path = _log(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace('"t1"', '"x"'), encoding="utf-8")
    with pytest.raises(ValueError, match="broken chain"):
        Recorder(path, session_id="s1", agent="test")


def test_the_log_is_private_to_its_owner(tmp_path: Path) -> None:
    mode = stat.S_IMODE(_log(tmp_path).stat().st_mode)
    assert mode == 0o600


def test_the_hash_does_not_depend_on_key_order() -> None:
    a = {"b": 1, "a": {"y": 2, "x": 3}, "chain": {"previous": GENESIS}}
    b = {"a": {"x": 3, "y": 2}, "chain": {"previous": GENESIS}, "b": 1}
    assert canonical(a) == canonical(b)
    assert entry_hash(a) == entry_hash(b)


def test_entries_are_ecs_shaped_for_lincoln(tmp_path: Path) -> None:
    entry = json.loads(_lines(_log(tmp_path))[0])
    assert entry["@timestamp"].endswith("Z")
    assert entry["event"]["action"] == "tool.call"
    assert entry["session"]["id"] == "s1" and entry["agent"]["name"] == "test"


def test_an_imported_entry_keeps_when_it_happened_and_when_it_was_written(
    tmp_path: Path,
) -> None:
    path = tmp_path / "s.ndjson"
    with Recorder(path, session_id="s1", agent="test") as rec:
        entry = rec.append("message", timestamp="2026-10-01T12:00:00.000Z")
    assert entry["@timestamp"] == "2026-10-01T12:00:00.000Z"
    assert entry["event"]["ingested"] > entry["@timestamp"]
    assert verify_file(path).valid


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("github", "github"),
        ("../../etc/passwd", "etc-passwd"),
        ("a/b\\c", "a-b-c"),
        ("..", "unnamed"),
    ],
)
def test_untrusted_names_cannot_leave_the_log_directory(raw: str, safe: str) -> None:
    assert safe_name(raw) == safe
