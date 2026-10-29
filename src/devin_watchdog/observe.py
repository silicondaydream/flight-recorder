"""From verified log entries to what the rules look at.

Flight Recorder entries come from three places, and each says something
different about what a session is doing:

- the Devin importer: `message`, `session.status`, `session.snapshot` (ACUs,
  PRs, tags). Devin's API exposes no shell commands or edits, so those are
  read out of Devin's own messages ("Running `npm test`... failed"), which is
  a heuristic and is marked as one.
- the MCP proxy: `tool.call` / `tool.result`, exact, with outcomes.
- anything that records `shell.command` / `file.edit` directly.

`SessionView` folds them into one per-session picture: recent commands with
outcomes, error signatures, edits, messages, status, tags and ACU history.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

Entry = dict[str, Any]

WINDOW = 500  # observations kept per session
NUDGE_MARK = "[Watchdog]"  # every message Watchdog sends starts with this

# -- reading messages ------------------------------------------------------------

_SHELL_START = (
    r"(?:npm|npx|yarn|pnpm|bun|node|deno|pip|pip3|python|python3|pytest|tox|nox|uv|poetry|"
    r"make|cmake|cargo|go|rustc|mvn|gradle|gradlew|\./gradlew|dotnet|bundle|rake|rspec|"
    r"docker|kubectl|terraform|git|gh|bash|sh|tsc|jest|vitest|eslint|ruff|mypy|"
    r"\./[\w./-]+|curl|psql|mysql|rails|php|composer|swift|xcodebuild|flutter|dart)"
)
_CODE_SPAN = re.compile(r"`([^`\n]{2,200})`")
_PROMPT_LINE = re.compile(r"^\s*\$\s+(.{2,200})$", re.MULTILINE)
_SHELL = re.compile(rf"^{_SHELL_START}(?:\s|$)")
_FAILURE = re.compile(
    r"(?i)\b(fail(?:s|ed|ing|ure)?|errors?|exception|traceback|crash(?:ed|es)?|"
    r"exit(?:ed)? (?:code|status) [1-9]\d*|non-zero|not found|cannot|can't|couldn't|"
    r"unable to|still (?:broken|failing|red)|timed? ?out)\b"
)
_SUCCESS = re.compile(
    r"(?i)\b(all (?:\d+ )?tests? pass(?:ed|ing)?|tests? (?:now )?pass(?:ed|es|ing)?|"
    r"build (?:now )?(?:passes|passed|succeeded|is green)|succeeded|works now|is green|"
    r"fixed (?:it|the)|resolved)\b"
)
_ERROR = re.compile(
    r"(\b(?:[A-Z][A-Za-z]*)?(?:Error|Exception)\b:?[^\n]{0,100}"
    r"|(?i:npm ERR!|fatal:|panic:|error(?:\[\w+\])?:)[^\n]{0,100}"
    r"|\bFAILED [^\n]{0,100})"
)
_EDIT = re.compile(
    r"(?i)\b(?:edit(?:ed|ing)?|updat(?:ed|ing|e)|modif(?:ied|ying|y)|chang(?:ed|ing|e)|"
    r"rewr(?:ote|iting|ite)|revert(?:ed|ing)?|patch(?:ed|ing)?|fix(?:ed|ing)? (?:in|up))\s+"
    r"(?:the\s+)?(?:file\s+)?`?([\w./@-]+\.[A-Za-z0-9]{1,8})`?"
)
TEST_OR_BUILD = re.compile(
    r"(?i)\b(test|tests|pytest|jest|vitest|rspec|tox|nox|build|compile|tsc|"
    r"cargo (?:build|test|check)|"
    r"go (?:build|test|vet)|mvn|gradle|make|lint|typecheck|mypy|ruff|eslint)\b"
)
ENV_BLOCKER = re.compile(
    r"(?i)(permission denied|access denied|\b40[13]\b|unauthori[sz]ed|forbidden|"
    r"command not found|no such file or directory|could not resolve host|name or service not known|"
    r"connection refused|network is unreachable|ssl certificate|certificate verify failed|"
    r"rate limit|too many requests|no space left on device|out of memory|\boom\b|killed|"
    r"enospc|eacces|econnrefused|etimedout|missing (?:credentials|api key|token|secret|env)|"
    r"(?:api key|token|secret|credential)s? (?:is |are )?(?:missing|invalid|expired|not set)|"
    r"(?:don't|do not) have (?:access|permission)|need (?:access|credentials|a token)|"
    r"(?:requires|needs) (?:sudo|root|login|2fa|mfa)|captcha|"
    r"(?:secret|environment variable) \w+ (?:is )?not (?:set|defined|found))"
)
_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "have",
        "has",
        "had",
        "was",
        "were",
        "are",
        "not",
        "but",
        "you",
        "your",
        "our",
        "its",
        "into",
        "then",
        "than",
        "them",
        "they",
        "their",
        "there",
        "here",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "would",
        "should",
        "could",
        "can",
        "just",
        "also",
        "only",
        "some",
        "more",
        "most",
        "very",
        "about",
        "after",
        "before",
        "again",
        "still",
        "now",
        "all",
        "any",
        "each",
        "been",
        "being",
        "did",
        "does",
        "doing",
        "done",
        "get",
        "got",
        "make",
        "made",
        "let",
        "lets",
        "use",
        "used",
        "using",
        "like",
        "look",
        "looking",
        "need",
        "needs",
        "try",
        "trying",
        "tried",
        "run",
        "running",
        "ran",
        "see",
        "seems",
        "seem",
        "working",
        "work",
        "works",
        "going",
        "want",
        "okay",
        "sure",
        "thanks",
        "thank",
        "please",
        "next",
        "first",
        "one",
        "two",
        "new",
        "file",
        "files",
        "code",
        "change",
        "changes",
        "changed",
        "update",
        "updated",
        "check",
        "checking",
        "checked",
        "fix",
        "fixed",
        "issue",
        "think",
        "should",
    ]
)


def normalise_command(command: str) -> str:
    """Collapse what varies between runs of "the same" command."""
    text = re.sub(r"\s+", " ", command.strip().strip("`").strip())
    text = re.sub(r"\b[0-9a-f]{7,40}\b", "<hex>", text)
    return re.sub(r"\b\d+\b", "<n>", text)[:160]


def error_signature(text: str) -> str:
    """An error line with its incidental details removed, so repeats group."""
    sig = text.strip().lower()
    sig = re.sub(r"(['\"]).*?\1", "<s>", sig)
    sig = re.sub(r"(/[\w.@-]+)+", "<path>", sig)
    sig = re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b", "<hex>", sig)
    sig = re.sub(r"\d+", "<n>", sig)
    return re.sub(r"\s+", " ", sig)[:120]


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in STOP}


@dataclass(frozen=True)
class Seen:
    """What one message, tool result or command says happened."""

    commands: tuple[str, ...] = ()
    outcome: str | None = None  # "failure" / "success" / None: unknown
    errors: tuple[str, ...] = ()
    edits: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


def read_message(text: str) -> Seen:
    spans = [*_CODE_SPAN.findall(text), *_PROMPT_LINE.findall(text)]
    commands = tuple(dict.fromkeys(s.strip() for s in spans if _SHELL.match(s.strip())))
    outcome = None
    if _SUCCESS.search(text):
        outcome = "success"
    elif _FAILURE.search(text):
        outcome = "failure"
    errors = tuple(dict.fromkeys(m.strip() for m in _ERROR.findall(text)))
    edits = tuple(dict.fromkeys(_EDIT.findall(text)))
    blockers = tuple(dict.fromkeys(m.group(0).lower() for m in ENV_BLOCKER.finditer(text)))
    return Seen(commands, outcome, errors, edits, blockers)


# -- entries ---------------------------------------------------------------------


def epoch(entry: Entry) -> float:
    stamp = str(entry.get("@timestamp") or entry.get("event", {}).get("ingested") or "")
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def kept_value(kept: Any) -> Any:
    """The value inside a `redact.keep` wrapper, or its preview if truncated."""
    if isinstance(kept, dict):
        return kept.get("value", kept.get("preview"))
    return kept


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("message"), str):
            return str(value["message"])
        return " ".join(_text_of(v) for v in value.get("content", []) if isinstance(v, dict | str))
    return ""


_EDIT_TOOL = re.compile(r"(?i)(edit|write|patch|replace|create_file|str_replace|apply_diff)")
_SHELL_ARGS = ("command", "cmd", "script", "shell")
_PATH_ARGS = ("path", "file", "file_path", "filename", "target_file")


@dataclass
class Obs:
    kind: str  # command / error / edit / blocker / message / progress
    at: float
    value: str = ""
    outcome: str | None = None
    source: str = ""  # message / mcp / shell: how sure the observation is


@dataclass
class SessionView:
    session_id: str
    agent: str = ""
    title: str = ""
    url: str = ""
    task: str = ""  # the first user message, else the title
    status: str = "unknown"
    status_detail: str | None = None
    tags: list[str] = field(default_factory=list)
    archived: bool = False
    acus: list[tuple[float, float]] = field(default_factory=list)  # (at, acus_consumed)
    prs: str = ""  # a signature of pull_requests, to see when it changes
    last_progress: float = 0.0
    acus_at_progress: float = 0.0
    first_seen: float = 0.0
    last_seen: float = 0.0
    observations: deque[Obs] = field(default_factory=lambda: deque(maxlen=WINDOW))
    pending: dict[str, tuple[str, str]] = field(default_factory=dict)  # MCP request -> call
    changed: bool = True

    @property
    def is_devin(self) -> bool:
        return self.agent == "devin"

    @property
    def acus_now(self) -> float:
        return self.acus[-1][1] if self.acus else 0.0

    @property
    def waiting_on_human(self) -> bool:
        return self.status_detail in ("waiting_for_user", "waiting_for_approval")

    @property
    def active(self) -> bool:
        if not self.is_devin:  # an MCP proxy session: live until it records its end
            return self.status != "exit"
        return self.status in ("new", "claimed", "running", "resuming") and not self.archived

    def recent(self, kind: str, since: float) -> list[Obs]:
        return [o for o in self.observations if o.kind == kind and o.at >= since]

    def messages(self, since: float = 0.0) -> list[Obs]:
        return [o for o in self.recent("message", since) if o.source == "devin"]

    def progress(self, at: float, why: str) -> None:
        self.last_progress = max(self.last_progress, at)
        self.acus_at_progress = self.acus_now
        self.observations.append(Obs("progress", at, why))

    # -- folding entries in --------------------------------------------------------

    def add(self, entry: Entry) -> None:
        at = epoch(entry)
        self.first_seen = self.first_seen or at
        self.last_seen = max(self.last_seen, at)
        self.agent = self.agent or str(entry.get("agent", {}).get("name", ""))
        self.changed = True
        action = entry.get("event", {}).get("action")
        flight = entry.get("flight") or {}
        handler = getattr(self, "_" + str(action).replace(".", "_"), None)
        if handler is not None:
            handler(at, entry, flight)

    def _session_snapshot(self, at: float, entry: Entry, flight: dict[str, Any]) -> None:
        session = kept_value(flight.get("session")) or {}
        if not isinstance(session, dict):
            return
        self.title = str(session.get("title") or self.title)
        self.url = str(session.get("url") or self.url)
        self.tags = [str(t) for t in session.get("tags") or []]
        self.archived = bool(session.get("is_archived"))
        self.status = str(session.get("status") or self.status)
        self.status_detail = session.get("status_detail") or self.status_detail
        acus = session.get("acus_consumed")
        if isinstance(acus, int | float) and not isinstance(acus, bool):
            self.acus.append((at, float(acus)))
            del self.acus[:-200]
        prs = "|".join(
            f"{p.get('pr_url')}:{p.get('pr_state')}"
            for p in session.get("pull_requests") or []
            if isinstance(p, dict)
        )
        if prs != self.prs:
            if prs:
                self.progress(at, "pull request opened or updated")
            self.prs = prs
        if not self.last_progress:
            self.last_progress, self.acus_at_progress = at, self.acus_now
        self.task = self.task or self.title

    def _session_end(self, at: float, entry: Entry, flight: dict[str, Any]) -> None:
        if not self.is_devin:
            self.status = "exit"

    def _session_status(self, at: float, entry: Entry, flight: dict[str, Any]) -> None:
        self.status = str(flight.get("status") or self.status)
        self.status_detail = flight.get("status_detail")
        if self.status_detail == "finished":
            self.progress(at, "finished")

    def _message(self, at: float, entry: Entry, flight: dict[str, Any]) -> None:
        text = str(kept_value(flight.get("text")) or "")
        sender = str(flight.get("from") or "devin")
        if sender == "user" and text.startswith(NUDGE_MARK):
            # Watchdog's own nudge comes back as a user message: not a person.
            self.observations.append(Obs("message", at, text, source="watchdog"))
            return
        if sender == "user":
            if not self.task or self.task == self.title:
                self.task = text
            # A human replied: whatever came before is not the agent's loop any more.
            self.observations.append(Obs("message", at, text, source="user"))
            self.progress(at, "a person replied")
            return
        self.observations.append(Obs("message", at, text, source="devin"))
        self._seen(at, read_message(text), "message")

    def _seen(self, at: float, seen: Seen, source: str) -> None:
        for command in seen.commands:
            self.observations.append(Obs("command", at, command, seen.outcome, source))
            if seen.outcome == "success" and TEST_OR_BUILD.search(command):
                self.progress(at, f"`{command}` passed")
        for error in seen.errors:
            self.observations.append(Obs("error", at, error_signature(error), source=source))
        for path in seen.edits:
            self.observations.append(Obs("edit", at, path, source=source))
        for blocker in seen.blockers:
            self.observations.append(Obs("blocker", at, blocker, source=source))

    def _tool_call(self, at: float, entry: Entry, flight: dict[str, Any]) -> None:
        tool = str(flight.get("tool", ""))
        args = kept_value(flight.get("arguments"))
        args = args if isinstance(args, dict) else {}
        command = next((str(args[k]) for k in _SHELL_ARGS if k in args), "")
        path = next((str(args[k]) for k in _PATH_ARGS if k in args), "")
        if _EDIT_TOOL.search(tool) and path:
            self.observations.append(Obs("edit", at, path, source="mcp"))
        label = command or f"{tool} {' '.join(f'{k}={v}' for k, v in sorted(args.items()))}"
        self.pending[str(flight.get("request_id"))] = (tool, label)

    def _tool_result(self, at: float, entry: Entry, flight: dict[str, Any]) -> None:
        tool, label = self.pending.pop(str(flight.get("request_id")), (str(flight.get("tool")), ""))
        outcome = entry.get("event", {}).get("outcome")
        self.observations.append(Obs("command", at, label or tool, outcome, "mcp"))
        if outcome == "failure":
            text = _text_of(kept_value(flight.get("result"))) or tool
            first = next((line for line in text.splitlines() if line.strip()), text)
            self.observations.append(Obs("error", at, error_signature(first), source="mcp"))
            for blocker in read_message(text).blockers:
                self.observations.append(Obs("blocker", at, blocker, source="mcp"))
        elif outcome == "success" and TEST_OR_BUILD.search(label):
            self.progress(at, f"{label} passed")

    def _shell_command(self, at: float, entry: Entry, flight: dict[str, Any]) -> None:
        command = str(kept_value(flight.get("command")) or "")
        outcome = entry.get("event", {}).get("outcome")
        output = str(kept_value(flight.get("output")) or "")
        seen = read_message(output)
        self._seen(at, Seen((command,), outcome, seen.errors, (), seen.blockers), "shell")

    def _file_edit(self, at: float, entry: Entry, flight: dict[str, Any]) -> None:
        self.observations.append(Obs("edit", at, str(flight.get("path", "")), source="shell"))
