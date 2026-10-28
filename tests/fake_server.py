"""A minimal stdio MCP server for the proxy tests. Standard library only.

Tools:
- echo: returns its `text` argument.
- fail: returns a JSON-RPC error.
- tool_error: returns a result with isError set.
- noise: prints a line that is not JSON before answering.
- big: returns `n` characters.
- hang: never answers.

Exits with $FAKE_EXIT (default 0) when stdin closes.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def reply(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def handle(message: dict[str, Any]) -> None:
    method, rid = message.get("method"), message.get("id")
    if method == "initialize":
        reply(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1.0"},
                },
            }
        )
    elif method == "tools/list":
        tools = [{"name": n, "inputSchema": {"type": "object"}} for n in ("echo", "fail", "big")]
        reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
    elif method == "tools/call":
        params = message["params"]
        name, args = params["name"], params.get("arguments") or {}
        if name == "hang":
            return
        if name == "fail":
            reply({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "boom"}})
            return
        if name == "noise":
            sys.stdout.write("this is not json\n")
        text = "x" * int(args.get("n", 0)) if name == "big" else str(args.get("text", ""))
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if name == "tool_error":
            result["isError"] = True
        reply({"jsonrpc": "2.0", "id": rid, "result": result})
    elif rid is not None:
        reply({"jsonrpc": "2.0", "id": rid, "result": {}})


def main() -> int:
    for line in sys.stdin:
        if line.strip():
            handle(json.loads(line))
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
