"""Tier 0: the project's own modules.

Checked before anything else, because the most common agent failure in a real
repo is not inventing a PyPI package -- it is inventing a helper in a file it
never opened. `from app.services import send_invoice` when `send_invoice` was
never written is the same class of defect as a hallucinated import, and it is
far more likely to survive review.

Module paths are indexed eagerly (a filesystem walk is cheap). Files are parsed
lazily, only when something actually asks what a module contains, so pointing
this at a large monorepo costs a directory scan rather than a full parse.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

#: Directories that are never project source.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "site-packages",
        "dist",
        "build",
        ".eggs",
        ".idea",
        ".vscode",
        "target",
    }
)

_MAX_FILES = 20_000


class LocalIndex:
    """Dotted module paths defined inside the project, and their members."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._modules: dict[str, Path] = {}
        self._built = False

    # -- public ----------------------------------------------------------

    def has_module(self, dotted: str) -> bool:
        self._build()
        return dotted in self._modules

    def is_local_root(self, top_level: str) -> bool:
        """Does any project module start with this top-level name?"""
        self._build()
        prefix = top_level + "."
        return any(m == top_level or m.startswith(prefix) for m in self._modules)

    def path_for(self, dotted: str) -> Path | None:
        """The file a project module lives in, if we indexed it."""
        self._build()
        return self._modules.get(dotted)

    def dotted_for(self, path: Path) -> str | None:
        """The dotted name of a file inside the project, if it is one.

        Needed to anchor relative imports: `from . import x` in
        `src/pkg/sub/mod.py` is a claim about `pkg.sub`, and there is no way to
        know that without first knowing what `mod.py` is called.
        """
        self._build()
        try:
            resolved = path.resolve()
        except OSError:
            return None
        for dotted, known in self._modules.items():
            if known == resolved:
                return dotted
        # The file may be a new one the agent is proposing, so it will not be in
        # the index yet. Derive its name positionally instead.
        for base in self._bases():
            derived = _dotted_name(resolved, base)
            if derived:
                return derived
        return None

    def _bases(self) -> list[Path]:
        bases = [self.root]
        src = self.root / "src"
        if src.is_dir():
            bases.insert(0, src)
        return bases

    def members(self, dotted: str) -> frozenset[str] | None:
        """Top-level names defined by a project module, or None if unknown."""
        self._build()
        path = self._modules.get(dotted)
        if path is None:
            return None
        return _parse_members(path)

    @property
    def module_count(self) -> int:
        self._build()
        return len(self._modules)

    # -- internals -------------------------------------------------------

    def _build(self) -> None:
        if self._built:
            return
        self._built = True

        roots = [self.root]
        src = self.root / "src"
        if src.is_dir():
            roots.append(src)

        seen = 0
        for base in roots:
            for path in _walk_python_files(base):
                seen += 1
                if seen > _MAX_FILES:
                    return
                dotted = _dotted_name(path, base)
                if dotted:
                    self._modules.setdefault(dotted, path)


def _walk_python_files(base: Path):
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                        stack.append(entry)
                elif entry.suffix == ".py":
                    yield entry
            except OSError:
                continue


def _dotted_name(path: Path, base: Path) -> str | None:
    try:
        relative = path.relative_to(base)
    except ValueError:
        return None
    parts = list(relative.parts)
    if not parts:
        return None
    stem = parts[-1][:-3]  # strip .py
    if stem == "__init__":
        parts = parts[:-1]
    else:
        parts[-1] = stem
    if not parts or any(not p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


@functools.lru_cache(maxsize=2048)
def _parse_members(path: Path) -> frozenset[str] | None:
    """Top-level definitions of a module, or None if it cannot be read.

    Re-exports count: a package whose `__init__.py` does `from .core import Foo`
    genuinely provides `Foo`, and treating it otherwise would flag correct code.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return None

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "__getattr__":
                # PEP 562: this module manufactures attributes on demand, so
                # the set of names it defines is not the set of names it has.
                return None
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    # The module's surface is now unknowable. Say so by
                    # refusing to answer rather than answering incompletely.
                    return None
                names.add(alias.asname or alias.name.split(".")[0])
    return frozenset(names)
