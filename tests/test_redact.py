"""Secrets never reach the log, and big payloads are cut visibly."""

from __future__ import annotations

import hashlib
import json

import pytest

from flight_recorder.redact import Redactor, capped, keep

# Test-only fakes in each format. None is a real credential.
SECRETS = {
    "aws-access-key": "AKIAABCDEFGHIJKLMNOP",
    "github-token": "ghp_" + "a" * 36,
    "slack-token": "xoxb-1234567890-abcdefghij",
    "google-api-key": "AIza" + "B" * 35,
    "api-key": "sk-proj-" + "c" * 32,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl",
}


@pytest.mark.parametrize(("name", "secret"), sorted(SECRETS.items()))
def test_each_builtin_secret_format_is_replaced(name: str, secret: str) -> None:
    clean, count = Redactor().text(f"run with {secret} please")
    assert secret not in clean
    assert f"[REDACTED:{name}]" in clean
    assert count == 1


def test_a_private_key_block_is_replaced_whole() -> None:
    block = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----"
    clean, _ = Redactor().text(f"key:\n{block}\nend")
    assert "AAAA" not in clean and "[REDACTED:private-key]" in clean


def test_a_bearer_header_keeps_the_scheme_and_loses_the_token() -> None:
    clean, _ = Redactor().text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert clean == "Authorization: Bearer [REDACTED:bearer]"


def test_an_assignment_keeps_the_name_and_loses_the_value() -> None:
    clean, _ = Redactor().text("curl https://x.example/?token=s3cr3tvalue&page=2")
    assert clean == "curl https://x.example/?token=[REDACTED:assignment]&page=2"


def test_sensitive_keys_are_replaced_whatever_their_value() -> None:
    clean, count = Redactor().value(
        {"user": "chris", "password": "hunter2", "nested": [{"api_key": "x"}]}
    )
    assert clean == {
        "user": "chris",
        "password": "[REDACTED:key]",
        "nested": [{"api_key": "[REDACTED:key]"}],
    }
    assert count == 2


def test_ordinary_text_is_untouched() -> None:
    text = "Edited src/app.py: renamed token_count to tokens; tests pass."
    assert Redactor().text(text) == (text, 0)


def test_custom_patterns_are_applied() -> None:
    clean, count = Redactor(extra=[r"ACME-\d{6}"]).text("ticket ACME-123456 opened")
    assert clean == "ticket [REDACTED:custom-1] opened" and count == 1


def test_a_small_payload_is_kept_whole() -> None:
    assert capped({"a": 1}, 100) == {"value": {"a": 1}}


def test_a_large_payload_is_cut_with_its_size_and_hash() -> None:
    value = "x" * 1000
    data = json.dumps(value).encode()
    kept = capped(value, 100)
    assert kept["truncated"] is True
    assert kept["bytes"] == len(data)
    assert kept["sha256"] == hashlib.sha256(data).hexdigest()
    assert len(kept["preview"]) == 100


def test_a_cut_never_splits_a_character_into_garbage() -> None:
    kept = capped("é" * 200, 51)  # 51 bytes lands inside a two-byte character
    assert "�" not in kept["preview"]


def test_redaction_happens_before_the_cut() -> None:
    secret = SECRETS["github-token"]
    kept = keep(Redactor(), secret + " " + "y" * 500, 60)
    assert secret not in kept["preview"]
    assert kept["redactions"] == 1
