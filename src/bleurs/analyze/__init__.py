"""Language front-ends.

Each analyzer turns source text into a flat list of `Reference` objects -- claims
the code makes about the world outside the file -- plus the set of reasons that
file cannot be fully verified.

The Python analyzer uses the interpreter's own `ast` module rather than a
generic parser. Where a language ships an exact parser, using anything else
trades fidelity for uniformity, and fidelity is the entire product here. Other
languages will get tree-sitter front-ends behind this same interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .result import AnalysisResult


class Analyzer(Protocol):
    """Front-end contract. One per language."""

    name: str
    extensions: tuple[str, ...]

    def analyze(self, source: str, path: Path) -> AnalysisResult: ...


#: Every extension any front-end claims. Used by the CLI and the hook so a new
#: language becomes visible to them the moment its analyzer is registered.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".pyi", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
)


def for_path(path: Path) -> Analyzer | None:
    """Pick a front-end by file extension, or None if we don't speak it."""
    from .python import PythonAnalyzer
    from .typescript import TypeScriptAnalyzer

    analyzers: tuple[Analyzer, ...] = (PythonAnalyzer(), TypeScriptAnalyzer())
    suffix = path.suffix.lower()
    for analyzer in analyzers:
        if suffix in analyzer.extensions:
            return analyzer
    return None


__all__ = ["Analyzer", "AnalysisResult", "SUPPORTED_EXTENSIONS", "for_path"]
