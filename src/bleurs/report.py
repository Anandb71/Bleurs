"""Terminal output.

Two audiences read this, and they want opposite things. A human wants to see
where to look. A model whose edit was just rejected wants to know precisely what
was wrong so it can fix it in one turn rather than five. The rendering here is
tuned for the human; `hook.py` renders the same findings for the model.
"""

from __future__ import annotations

import os
import sys

from .refs import Report, Verdict

_ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "cyan": "\033[36m",
}


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.name == "nt":
        return _enable_windows_ansi()
    return True


def _enable_windows_ansi() -> bool:  # pragma: no cover - platform specific
    """Turn on virtual terminal processing so ANSI works in cmd.exe."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -11 is STD_OUTPUT_HANDLE; 0x4 is ENABLE_VIRTUAL_TERMINAL_PROCESSING.
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x4))
    except Exception:
        return False


def _can_encode(text: str, stream) -> bool:
    """Will this glyph survive the console it is about to be printed to?

    Windows consoles still default to a legacy codepage often enough that an
    em-dash arrives as a replacement character. Degrading to ASCII is better
    than shipping output that looks broken on the first run.
    """
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError, TypeError):
        return False


class Painter:
    def __init__(self, stream=None) -> None:
        stream = stream or sys.stdout
        self.enabled = _supports_color(stream)
        unicode_ok = _can_encode("—↳", stream)
        self.dash = "—" if unicode_ok else "-"
        self.arrow = "↳" if unicode_ok else "->"

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(_ANSI.get(s, "") for s in styles)
        return f"{prefix}{text}{_ANSI['reset']}"


def render(reports: list[Report], *, explain: bool = False, stream=None) -> str:
    """Human-readable summary of one or more file reports."""
    out = stream or sys.stdout
    paint = Painter(out)
    lines: list[str] = []

    total_blocks = 0
    total_warns = 0
    total_checked = 0

    for report in reports:
        if report.parse_error:
            lines.append(
                f"{paint('skipped', 'dim')} {report.path} "
                f"{paint('(' + report.parse_error + ')', 'dim')}"
            )
            continue

        total_checked += report.checked
        blocks = report.blocks
        warns = report.warnings
        total_blocks += len(blocks)
        total_warns += len(warns)

        if not blocks and not warns and not explain:
            continue

        lines.append("")
        lines.append(paint(str(report.path), "bold"))

        for finding in sorted(blocks, key=_position):
            lines.extend(_render_finding(finding, paint, "BLOCK", "red"))
        for finding in sorted(warns, key=_position):
            lines.extend(_render_finding(finding, paint, "warn ", "yellow"))

        if explain and report.abstentions:
            lines.append(paint("  not verified:", "dim"))
            for reason in sorted(report.abstentions, key=lambda r: r.name):
                lines.append(paint(f"    - {reason.value}", "dim"))

    lines.append("")
    if total_blocks:
        summary = paint(
            f"{total_blocks} hallucination{'s' if total_blocks != 1 else ''} blocked",
            "red",
            "bold",
        )
    else:
        summary = paint("no hallucinations found", "green")
    detail = f"{total_checked} reference{'s' if total_checked != 1 else ''} verified"
    if total_warns:
        detail += f", {total_warns} warning{'s' if total_warns != 1 else ''}"
    lines.append(f"{summary}  {paint(detail, 'dim')}")

    return "\n".join(lines)


def _position(finding) -> tuple[int, int]:
    return (finding.reference.line, finding.reference.col)


def _render_finding(finding, paint: Painter, label: str, color: str) -> list[str]:
    ref = finding.reference
    location = paint(f"{ref.line}:{ref.col}", "dim")
    head = (
        f"  {paint(label, color, 'bold')} {location} "
        f"{paint(ref.display, 'cyan')} {paint.dash} {finding.message}"
    )
    lines = [head]
    if finding.suggestion:
        lines.append(f"         {paint(paint.arrow + ' ' + finding.suggestion, 'dim')}")
    return lines


def render_agent_message(reports: list[Report]) -> str:
    """The text a rejected agent sees. Terse, specific, actionable.

    No colour, no decoration, no advice about what the agent should have done
    instead -- just the disproved claims and the nearest real names, which is
    the minimum information needed to correct the edit and nothing that would
    push it toward a different wrong answer.
    """
    lines = ["Bleurs blocked this edit. These references do not exist:"]
    for report in reports:
        for finding in sorted(report.blocks, key=_position):
            ref = finding.reference
            entry = (
                f"  - {report.path.name}:{ref.line}  {ref.display} - {finding.message}"
            )
            if finding.suggestion:
                entry += f"  ({finding.suggestion})"
            lines.append(entry)
    lines.append("")
    lines.append(
        "Each was checked against the real environment, not guessed. "
        "Fix the reference or install the dependency, then retry."
    )
    return "\n".join(lines)


def exit_code(reports: list[Report]) -> int:
    return 1 if any(r.blocks for r in reports) else 0


__all__ = ["Painter", "exit_code", "render", "render_agent_message"]
