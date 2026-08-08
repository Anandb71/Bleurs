"""Claude Code PreToolUse adapter.

Runs between the agent deciding to write and the bytes landing on disk. It
reconstructs what the file *would* contain after the edit, checks that, and
rejects the call if it contains a proven-nonexistent reference.

Checking the post-edit content rather than the diff fragment matters: an import
added at the top of a file and a call added at the bottom are one claim, and
only the whole file shows whether the name was ever bound.

Failure is always open. A crash, a timeout, an unparseable payload -- anything
unexpected lets the write through. A firewall that fails closed on its own bugs
is a firewall that gets uninstalled the first time it has one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .analyze import SUPPORTED_EXTENSIONS
from .engine import Config, Engine
from .refs import Report
from .report import render_agent_message

#: Tools whose payload we know how to reconstruct.
_SUPPORTED = {"Write", "Edit", "MultiEdit", "update_file", "create_file"}

ALLOW = 0
DENY = 2


def run(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else []
    emit_json = "--json" in argv

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return ALLOW

    if not isinstance(payload, dict):
        return ALLOW

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if tool_name not in _SUPPORTED or not isinstance(tool_input, dict):
        return ALLOW

    resolved = _resolve_content(tool_input)
    if resolved is None:
        return ALLOW
    path, content = resolved

    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return ALLOW

    try:
        cwd = payload.get("cwd")
        root = Path(cwd) if cwd else _infer_root(path)
        engine = Engine(Config(project_root=root))
        report = engine.check_source(content, path)
    except Exception:
        # Our bug is not the user's problem.
        return ALLOW

    if not report.blocks:
        return ALLOW

    reason = render_agent_message([report])
    if emit_json:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return ALLOW

    sys.stderr.write(reason + "\n")
    return DENY


def _resolve_content(tool_input: dict) -> tuple[Path, str] | None:
    """Reconstruct the file's post-edit content."""
    raw_path = tool_input.get("file_path") or tool_input.get("path")
    if not raw_path:
        return None
    path = Path(raw_path)

    if "content" in tool_input:
        content = tool_input["content"]
        return (path, content) if isinstance(content, str) else None

    existing = _read(path)
    if existing is None:
        return None

    if "edits" in tool_input and isinstance(tool_input["edits"], list):
        content = existing
        for edit in tool_input["edits"]:
            if not isinstance(edit, dict):
                return None
            content = _apply(content, edit)
            if content is None:
                return None
        return (path, content)

    if "old_string" in tool_input:
        content = _apply(existing, tool_input)
        return (path, content) if content is not None else None

    return None


def _apply(content: str, edit: dict) -> str | None:
    old = edit.get("old_string")
    new = edit.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    if old and old not in content:
        # The edit will fail on its own terms. Nothing for us to judge.
        return None
    if edit.get("replace_all"):
        return content.replace(old, new)
    return content.replace(old, new, 1)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _infer_root(path: Path) -> Path | None:
    """Walk up for a project marker so local modules resolve."""
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "package.json", ".git")
    for parent in [path.parent, *path.parents]:
        if any((parent / m).exists() for m in markers):
            return parent
    return None
