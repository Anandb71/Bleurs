from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .engine import Config, Engine
from .refs import Report
from .report import Painter, exit_code, render

_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "site-packages",
    "build",
    "dist",
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        return _cmd_check(args)
    if args.command == "hook":
        from .hook import run

        return run(sys.argv[2:])
    if args.command == "mcp":
        from .mcp import serve

        return serve(Path(args.root).resolve() if args.root else Path.cwd())
    if args.command == "surface":
        return _cmd_surface(args)
    if args.command == "demo":
        return _cmd_demo(args)
    if args.command == "install-hook":
        return _cmd_install_hook(args)

    parser.print_help()
    return 0


# -- commands ------------------------------------------------------------


def _cmd_check(args) -> int:
    paths = _expand(args.paths, args.exclude or [])
    if not paths:
        print("no Python files found", file=sys.stderr)
        return 0

    root = Path(args.root).resolve() if args.root else _infer_root(paths[0])
    engine = Engine(
        Config(
            project_root=root,
            introspect=not args.no_introspect,
            network=not args.offline,
            strict_imports=not args.no_strict_imports,
        )
    )

    reports = [engine.check_file(p) for p in paths]

    if args.format == "json":
        print(json.dumps(_as_json(reports), indent=2))
    else:
        print(render(reports, explain=args.explain))

    return exit_code(reports)


def _cmd_surface(args) -> int:
    """Project an API surface instead of reading the file that implements it."""
    from .surface import estimate_tokens, installed_surface, local_surface, render

    target = args.target
    path = Path(target)

    if path.suffix in {".py", ".pyi"} and path.exists():
        surface = local_surface(path, private=args.all)
        original = path.read_text(encoding="utf-8", errors="replace")
    else:
        surface = installed_surface(target)
        original = None

    text = render(surface, summaries=not args.no_summaries)
    print(text)

    if args.stats:
        paint = Painter()
        estimated = estimate_tokens(text)
        line = f"\n~{estimated} tokens"
        if original:
            full = estimate_tokens(original)
            line += (
                f" vs ~{full} for the whole file "
                f"{paint.dash} {full / max(estimated, 1):.0f}x smaller"
            )
        print(paint(line + "  (estimated at 4 chars/token)", "dim"))

    return 0 if surface.ok else 1


def _cmd_demo(args) -> int:
    """Run the bundled samples. This is the thirty-second version of the pitch."""
    samples = Path(__file__).parent / "demo"
    files = sorted(samples.glob("*.py"))
    if not files:
        print("demo samples missing from this install", file=sys.stderr)
        return 1

    paint = Painter()
    engine = Engine(
        Config(
            project_root=samples,
            network=not args.offline,
        )
    )

    print(paint("\nbleurs demo", "bold"))
    print(
        paint(
            "Realistic LLM output. Every reference below was checked against "
            "this machine's\nactual environment. No model, no heuristics, no "
            "guessing.\n",
            "dim",
        )
    )

    reports: list[Report] = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        header = source.splitlines()[0].lstrip("# ").strip()
        print(paint(f"  {path.name}", "cyan"), paint(f"{paint.dash} {header}", "dim"))
        # Report against the bare filename. The samples live wherever pip put
        # them, and a wall of site-packages paths is not the point of the demo.
        reports.append(engine.check_source(source, Path(path.name)))

    print(render(reports, explain=args.explain))
    return 0


def _cmd_install_hook(args) -> int:
    target = (
        Path.home() / ".claude" / "settings.json"
        if args.user
        else Path.cwd() / ".claude" / "settings.json"
    )

    settings: dict = {}
    if target.exists():
        try:
            settings = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"{target} is not valid JSON; refusing to touch it", file=sys.stderr)
            return 1
        if not isinstance(settings, dict):
            print(f"{target} is not a JSON object; refusing to touch it", file=sys.stderr)
            return 1

    entry = {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [{"type": "command", "command": "bleurs hook"}],
    }

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print("existing 'hooks' key is not an object; refusing to touch it", file=sys.stderr)
        return 1
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        print("existing 'PreToolUse' key is not a list; refusing to touch it", file=sys.stderr)
        return 1

    already = any(
        isinstance(item, dict)
        and any(
            isinstance(h, dict) and "bleurs hook" in str(h.get("command", ""))
            for h in item.get("hooks", [])
        )
        for item in pre
    )
    if already:
        print(f"already installed in {target}")
        return 0

    pre.append(entry)

    print(f"This will add a PreToolUse hook to {target}:\n")
    print(json.dumps(entry, indent=2))
    if not args.yes:
        try:
            answer = input("\nWrite it? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if answer not in {"y", "yes"}:
            print("aborted")
            return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"\ninstalled → {target}")
    print("Claude Code picks this up on the next session.")
    return 0


# -- helpers -------------------------------------------------------------


def _expand(paths: list[str], exclude: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for child in sorted(path.rglob("*.py")):
                if any(part in _SKIP_DIRS for part in child.parts):
                    continue
                if _excluded(child, exclude):
                    continue
                out.append(child)
        elif path.suffix in {".py", ".pyi"}:
            out.append(path)
    return out


def _excluded(path: Path, patterns: list[str]) -> bool:
    posix = path.as_posix()
    return any(path.match(p) or p in posix.split("/") for p in patterns)


def _infer_root(path: Path) -> Path | None:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", ".git")
    for parent in [path.resolve().parent, *path.resolve().parents]:
        if any((parent / m).exists() for m in markers):
            return parent
    return None


def _as_json(reports: list[Report]) -> dict:
    return {
        "blocked": any(r.blocks for r in reports),
        "files": [
            {
                "path": str(r.path),
                "parse_error": r.parse_error,
                "verified": r.checked,
                "unverified_reasons": sorted(a.value for a in r.abstentions),
                "findings": [
                    {
                        "verdict": f.verdict.value,
                        "confidence": f.confidence.value,
                        "reference": f.reference.dotted,
                        "source": f.reference.display,
                        "line": f.reference.line,
                        "column": f.reference.col,
                        "message": f.message,
                        "suggestion": f.suggestion,
                        "resolver": f.resolver,
                    }
                    for f in r.findings
                    if f.verdict.value != "allow"
                ],
            }
            for r in reports
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bleurs",
        description="A deterministic firewall for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"bleurs {__version__}")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="check files or directories")
    check.add_argument("paths", nargs="*", default=["."])
    check.add_argument("--root", help="project root for local module resolution")
    check.add_argument(
        "--no-introspect",
        action="store_true",
        help="never import libraries (disables API checking)",
    )
    check.add_argument("--offline", action="store_true", help="never contact PyPI")
    check.add_argument(
        "--no-strict-imports",
        action="store_true",
        help="warn instead of blocking on packages absent from PyPI",
    )
    check.add_argument(
        "--exclude",
        action="append",
        metavar="PATTERN",
        help="skip paths matching a glob or directory name (repeatable)",
    )
    check.add_argument(
        "--explain",
        action="store_true",
        help="also list what could not be verified, and why",
    )
    check.add_argument("--format", choices=["text", "json"], default="text")

    hook = sub.add_parser("hook", help="run as a Claude Code PreToolUse hook")
    hook.add_argument(
        "--json",
        action="store_true",
        help="emit a JSON permission decision instead of using exit codes",
    )

    surface = sub.add_parser(
        "surface",
        help="project an API surface instead of reading the whole file",
    )
    surface.add_argument(
        "target", help="a dotted module/class (json, datetime.datetime) or a .py path"
    )
    surface.add_argument(
        "--all", action="store_true", help="include private (underscore) names"
    )
    surface.add_argument(
        "--no-summaries", action="store_true", help="names and signatures only"
    )
    surface.add_argument(
        "--stats", action="store_true", help="report the estimated token cost"
    )

    mcp = sub.add_parser(
        "mcp", help="run as an MCP server so agents can query the index"
    )
    mcp.add_argument("--root", help="project root for local resolution")

    demo = sub.add_parser("demo", help="see it catch real hallucinations")
    demo.add_argument("--offline", action="store_true")
    demo.add_argument("--explain", action="store_true")

    install = sub.add_parser("install-hook", help="wire the hook into Claude Code")
    install.add_argument(
        "--user",
        action="store_true",
        help="install to ~/.claude/settings.json instead of ./.claude",
    )
    install.add_argument("--yes", action="store_true", help="skip the confirmation")

    return parser


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
