"""What is kept of a payload: secrets removed, then size capped.

Tool arguments and results routinely carry credentials — an agent pastes a
token into a curl command, a server echoes its environment. The log is meant to
be read later and by other people, so secrets are replaced before anything is
written. Redaction runs first, then the cap, so a secret can never survive in a
truncated preview.

Redaction is pattern-based and therefore best effort: it catches the formats
below and anything the operator adds with `--redact`, not every secret there
is. The count of replacements is recorded, so a reader knows something was
removed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# (name, pattern). The name is what replaces the match: [REDACTED:name]. A
# group named `keep` is context written back in front of it (the header name,
# the parameter name), so a reader can still see what was removed.
BUILTIN_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "private-key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    ),
    ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("api-key", r"\bsk-(?:[A-Za-z]+-)?[A-Za-z0-9_-]{20,}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ("bearer", r"(?i)(?P<keep>\bbearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    (
        "assignment",
        r"(?i)(?P<keep>\b(?:password|passwd|secret|token|api_key|apikey|access_key)[=:])"
        r"[^\s\"'&]{6,}",
    ),
)

# A dict key with one of these names has its whole value replaced, whatever it
# looks like: {"password": "hunter2"} has no pattern to match.
SENSITIVE_KEY = re.compile(
    r"(?i)^(?:password|passwd|secret|client_secret|token|access_token|refresh_token|"
    r"api[_-]?key|authorization|cookie|set-cookie|private_key)$"
)


@dataclass
class Redactor:
    extra: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._patterns = [(name, re.compile(p)) for name, p in BUILTIN_PATTERNS]
        self._patterns += [(f"custom-{i + 1}", re.compile(p)) for i, p in enumerate(self.extra)]

    def text(self, value: str) -> tuple[str, int]:
        total = 0
        for name, pattern in self._patterns:
            marker = f"[REDACTED:{name}]"

            def replace(m: re.Match[str], marker: str = marker) -> str:
                return (m.groupdict().get("keep") or "") + marker

            value, n = pattern.subn(replace, value)
            total += n
        return value, total

    def value(self, value: Any) -> tuple[Any, int]:
        """Redact a JSON value recursively. Returns (clean value, replacements)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            out, total = [], 0
            for item in value:
                clean, n = self.value(item)
                out.append(clean)
                total += n
            return out, total
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            total = 0
            for key, item in value.items():
                if SENSITIVE_KEY.match(str(key)) and item not in (None, "", [], {}):
                    result[key] = "[REDACTED:key]"
                    total += 1
                else:
                    result[key], n = self.value(item)
                    total += n
            return result, total
        return value, 0


def capped(value: Any, limit: int) -> dict[str, Any]:
    """The value itself if it serialises within `limit` bytes, else a preview.

    A truncated payload keeps its full size and SHA-256, so a reader can see
    that it was cut and match it against a copy kept elsewhere.
    """
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(data) <= limit:
        return {"value": value}
    return {
        "truncated": True,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "preview": data[:limit].decode("utf-8", errors="ignore"),
    }


def keep(redactor: Redactor, value: Any, limit: int) -> dict[str, Any]:
    """Redact, then cap. The shape written into a log entry."""
    clean, count = redactor.value(value)
    kept = capped(clean, limit)
    if count:
        kept["redactions"] = count
    return kept
