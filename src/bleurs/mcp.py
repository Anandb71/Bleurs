"""MCP server: JSON-RPC 2.0 over stdio, implemented against the wire format.

No SDK, because the dependency budget for this project is zero and the protocol
is a few hundred lines of message dispatch. What it costs in boilerplate it buys
back in `uvx bleurs mcp` working on a machine with nothing installed.

Two tools, corresponding to the two halves of the thesis:

    surface   what exists   -- ask the index instead of reading the file
    verify    what does not -- check an edit before committing to it

`surface` is the one that changes how a session runs. An agent that can ask for
an exact API surface at a fraction of a file read stops filling its context with
implementation it never needed, and -- because the answer is derived from the
code rather than remembered from the transcript -- it can ask again after a
compaction for the same tiny price. Context becomes a cache instead of a ledger.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .engine import Config, Engine
from .surface import estimate_tokens, installed_surface, local_surface, render

#: Spoken if the client does not name a version it wants.
DEFAULT_PROTOCOL = "2025-06-18"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "surface",
        "description": (
            "Get the exact API of a Python module, class, or project file: every "
            "public name with its real signature and a one-line summary. Reads "
            "the installed package or parses the project file, so the answer is "
            "always current.\n\n"
            "Use this INSTEAD OF reading a file when you need to know how to call "
            "something. It is typically 5-10x fewer tokens than the source and "
            "contains everything needed to make a correct call. Cheap enough to "
            "re-ask at any time rather than holding it in context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "A dotted module or class ('json', 'datetime.datetime', "
                        "'numpy.linalg') or a path to a .py file."
                    ),
                },
                "private": {
                    "type": "boolean",
                    "description": (
                        "Include underscore-prefixed names. Use when editing the "
                        "module itself rather than calling it."
                    ),
                    "default": False,
                },
                "summaries": {
                    "type": "boolean",
                    "description": "Include one-line docstring summaries.",
                    "default": True,
                },
                "limit": {
                    "type": "integer",
                    "description": "Cap the number of members returned.",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "verify",
        "description": (
            "Check Python source for references that provably do not exist -- "
            "invented packages, invented methods on real libraries, helpers "
            "missing from this project. Returns the blocked references and the "
            "real API of whatever each one got wrong.\n\n"
            "Run this on code you are about to write. It only reports what it can "
            "prove absent, so a clean result is meaningful and a block is never a "
            "guess."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Python source to check."},
                "path": {
                    "type": "string",
                    "description": (
                        "Where this code will live. Improves project-local "
                        "resolution and lets relative imports be checked."
                    ),
                },
            },
            "required": ["code"],
        },
    },
]


# -- tool implementations ------------------------------------------------


def _tool_surface(args: dict[str, Any], root: Path | None) -> str:
    target = str(args.get("target") or "").strip()
    if not target:
        return "error: 'target' is required"

    path = Path(target)
    if path.suffix in {".py", ".pyi"}:
        candidate = path if path.is_absolute() else ((root or Path.cwd()) / path)
        if candidate.exists():
            projected = local_surface(candidate, private=bool(args.get("private")))
        else:
            return f"error: no such file: {target}"
    else:
        projected = installed_surface(target)

    text = render(
        projected,
        summaries=args.get("summaries", True) is not False,
        limit=args.get("limit"),
    )
    return f"{text}\n\n(~{estimate_tokens(text)} tokens)"


def _tool_verify(args: dict[str, Any], root: Path | None) -> str:
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return "error: 'code' is required"

    raw_path = args.get("path")
    path = Path(raw_path) if raw_path else Path("proposed.py")

    engine = Engine(Config(project_root=root))
    report = engine.check_source(code, path)

    if report.parse_error:
        return f"could not parse: {report.parse_error}"

    if not report.blocks:
        detail = f"{report.checked} reference(s) verified"
        if report.abstentions:
            unverified = ", ".join(sorted(a.value for a in report.abstentions))
            detail += f"; not verified: {unverified}"
        return f"OK - no hallucinated references found. {detail}."

    lines = [f"BLOCKED - {len(report.blocks)} reference(s) do not exist:", ""]
    surfaces: dict[str, str] = {}
    for finding in report.blocks:
        ref = finding.reference
        entry = f"  {path.name}:{ref.line}  {ref.display} - {finding.message}"
        if finding.suggestion:
            entry += f"  ({finding.suggestion})"
        lines.append(entry)
        if finding.surface:
            surfaces.setdefault(finding.surface.splitlines()[0], finding.surface)

    if surfaces:
        lines += ["", "What those containers actually provide:", ""]
        for text in surfaces.values():
            lines += [text, ""]

    return "\n".join(lines).rstrip()


HANDLERS: dict[str, Callable[[dict[str, Any], Path | None], str]] = {
    "surface": _tool_surface,
    "verify": _tool_verify,
}


# -- protocol ------------------------------------------------------------


class Server:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")

        # A notification has no id and takes no reply, ever. Answering one is
        # a protocol violation that some clients treat as fatal.
        if request_id is None:
            return None

        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            return _result(
                request_id,
                {
                    "protocolVersion": requested or DEFAULT_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "bleurs", "version": __version__},
                },
            )

        if method == "tools/list":
            return _result(request_id, {"tools": TOOLS})

        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            handler = HANDLERS.get(name)
            if handler is None:
                return _error(request_id, -32602, f"unknown tool: {name}")
            try:
                text = handler(params.get("arguments") or {}, self.root)
            except Exception as exc:  # a tool crash is a tool result, not a hangup
                return _result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": f"error: {exc}"}],
                        "isError": True,
                    },
                )
            return _result(request_id, {"content": [{"type": "text", "text": text}]})

        if method == "ping":
            return _result(request_id, {})

        return _error(request_id, -32601, f"method not found: {method}")


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(root: Path | None = None, stdin=None, stdout=None) -> int:
    """Read newline-delimited JSON-RPC on stdin, answer on stdout.

    stdout carries protocol only. Anything this process wants to say to a human
    goes to stderr, because a stray print here corrupts the channel and the
    client simply disconnects.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server(root)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue

        response = server.handle(message)
        if response is None:
            continue
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()

    return 0
