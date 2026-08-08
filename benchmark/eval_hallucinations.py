"""Measure detection and false positives on real code.

The README used to cite someone else's precision figure. This produces our own,
and it is built to be hostile to its author.

**Precision.** Run bleurs over unmutated files from site-packages. Those files
are installed, importable and working, so every reference in them resolves by
construction. Any BLOCK is therefore a false positive -- no labelling, no
judgement call, no opportunity to grade my own homework. This is the number
that matters, because a false positive is the failure that gets a tool
uninstalled.

**Recall.** Take the same real files and plant a hallucination: rename a method
to something plausible that does not exist, or swap a package for one nobody
published. Ground truth is exact because we know what we broke and where. It
counts as caught only if bleurs blocks that specific reference.

Two honesty rules the first version of this file got wrong:

1. Every planted name is verified absent before it is scored. `os_toolkit` and
   `argparse_utils` are both real PyPI projects -- mutations that land on a
   published name are not hallucinations and are discarded.
2. Outcomes are three-way. A planted hallucination bleurs *declined* to judge
   is a miss, but a principled one, produced by the same rules that keep the
   false positive rate at zero. Folding those in with silent misses would hide
   the real weakness, which is the last column: cases it looked at and got
   wrong.

    python benchmark/eval_hallucinations.py [--limit N] [--offline]
"""

from __future__ import annotations

import argparse
import ast
import random
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bleurs import Config, Engine  # noqa: E402
from bleurs.truth.introspect import Introspector  # noqa: E402

#: Suffixes that read like something a model would actually invent.
API_SUFFIXES = ("_safe", "_async", "_utc", "_all", "_json", "_or_none", "_ex")
PKG_SUFFIXES = ("_helpers", "_utils", "_toolkit", "_client", "_sdk")

MIN_CHARS = 1200
SEED = 20260808


@dataclass
class Planted:
    """One deliberately broken copy of a real file."""

    kind: str
    source: str
    line: int
    name: str


@dataclass
class Tally:
    caught: int = 0
    abstained: int = 0
    silent: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.caught + self.abstained + self.silent

    @property
    def rate(self) -> float:
        return self.caught / self.total if self.total else 0.0

    @property
    def judged_rate(self) -> float:
        """Recall over the cases where a verdict was actually reached."""
        judged = self.caught + self.silent
        return self.caught / judged if judged else 0.0


# -- corpus --------------------------------------------------------------


def corpus(limit: int) -> list[Path]:
    roots = [
        Path(p)
        for key in ("purelib", "platlib")
        if (p := sysconfig.get_paths().get(key)) and Path(p).is_dir()
    ]
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if len(files) >= limit:
                return files
            if set(path.parts) & {"__pycache__", "tests", "test", "_vendor"}:
                continue
            if path in seen:
                continue
            try:
                if path.stat().st_size < MIN_CHARS:
                    continue
            except OSError:
                continue
            seen.add(path)
            files.append(path)
    return files


# -- mutation ------------------------------------------------------------


def _replace_span(source: str, line: int, col: int, length: int, new: str) -> str:
    lines = source.splitlines(keepends=True)
    if not 1 <= line <= len(lines):
        return source
    text = lines[line - 1]
    lines[line - 1] = text[:col] + new + text[col + length :]
    return "".join(lines)


def plant(
    source: str, rng: random.Random, introspector: Introspector, registry
) -> list[Planted]:
    """Break exactly one real reference per returned variant."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = alias.name

    out: list[Planted] = []

    # 1. an invented method on a real module: json.loads -> json.loads_safe
    attributes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
        and node.end_lineno == node.lineno
    ]
    if attributes:
        node = rng.choice(attributes)
        invented = node.attr + rng.choice(API_SUFFIXES)
        probe = introspector.surface(modules[node.value.id])
        if probe.ok and invented not in {m.name for m in probe.members}:
            out.append(
                Planted(
                    "invented API",
                    _replace_span(
                        source,
                        node.end_lineno,
                        node.end_col_offset - len(node.attr),
                        len(node.attr),
                        invented,
                    ),
                    node.lineno,
                    invented,
                )
            )

    # 2. an invented package: import requests -> import requests_sdk
    plain = [
        (node, alias)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if "." not in alias.name
    ]
    if plain:
        _, alias = rng.choice(plain)
        invented = alias.name + rng.choice(PKG_SUFFIXES)
        # A mutation that lands on a published name is not a hallucination.
        if registry.exists(invented) is False:
            out.append(
                Planted(
                    "invented package",
                    _replace_span(
                        source,
                        alias.lineno,
                        alias.col_offset,
                        len(alias.name),
                        invented,
                    ),
                    alias.lineno,
                    invented,
                )
            )

    # 3. an invented name in a from-import: from json import loads_json
    froms = [
        (node, alias)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and not node.level and node.module
        for alias in node.names
        if alias.name != "*"
    ]
    if froms:
        node, alias = rng.choice(froms)
        invented = alias.name + rng.choice(API_SUFFIXES)
        probe = introspector.surface(node.module)
        if probe.ok and invented not in {m.name for m in probe.members}:
            out.append(
                Planted(
                    "invented import name",
                    _replace_span(
                        source,
                        alias.lineno,
                        alias.col_offset,
                        len(alias.name),
                        invented,
                    ),
                    alias.lineno,
                    invented,
                )
            )

    return out


# -- passes --------------------------------------------------------------


def precision_pass(files: list[Path], engines) -> tuple[int, list[str]]:
    """Any block on unmutated, working code is a false positive."""
    false_positives: list[str] = []
    checked = 0
    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        engine = engines.for_file(path)
        report = engine.check_source(source, path)
        if report.parse_error:
            continue
        checked += 1
        for finding in report.blocks:
            false_positives.append(
                f"{path.name}:{finding.reference.line} "
                f"{finding.reference.display} -- {finding.message}"
            )
    return checked, false_positives


def recall_pass(
    files: list[Path], engines, introspector: Introspector, rng: random.Random
) -> dict[str, Tally]:
    tallies: dict[str, Tally] = {}
    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        engine = engines.for_file(path)
        for planted in plant(source, rng, introspector, engine.registry):
            tally = tallies.setdefault(planted.kind, Tally())
            report = engine.check_source(planted.source, path)

            if any(
                planted.name in f.reference.display or planted.name in f.message
                for f in report.blocks
            ):
                tally.caught += 1
                continue

            related = [
                f
                for f in report.findings
                if planted.name in f.reference.display
                or planted.name in (f.message or "")
            ]
            reason = next(
                (f.abstained.value for f in related if f.abstained is not None), None
            )
            if reason is None and any(f.verdict.value == "warn" for f in related):
                reason = "reported as a warning rather than blocked"
            if reason is None and not related and report.abstentions:
                # No finding names this reference at all, which means the
                # analyzer dropped it before the engine ever saw it -- a
                # wildcard import or a shadowed name. Still a miss, but an
                # explained one, and lumping it in with genuine blind spots
                # would overstate the blind spots.
                reason = "dropped before judging: " + ", ".join(
                    sorted(a.name.lower() for a in report.abstentions)[:2]
                )

            if reason:
                tally.abstained += 1
                tally.reasons[reason] = tally.reasons.get(reason, 0) + 1
            else:
                tally.silent += 1
                if len(tally.examples) < 3:
                    tally.examples.append(f"{path.name}:{planted.line} {planted.name}")
    return tallies


class EngineSet:
    """One engine per installed package, rooted at that package's directory.

    The first version of this harness built a single engine with no
    `project_root`, which silently disabled tier 0 entirely -- so the class
    shapes, `self` resolution and instance-attribute checking were never
    measured at all, and a 0% false positive rate was being reported for a
    configuration nobody runs.

    Rooting each file at its own package is the realistic setup: it is what
    happens when an agent edits a file inside the project that owns it.
    """

    def __init__(self, *, network: bool) -> None:
        self.network = network
        self._engines: dict[Path, Engine] = {}
        self._shared_registry = None

    def for_file(self, path: Path) -> Engine:
        root = self._root_for(path)
        engine = self._engines.get(root)
        if engine is None:
            engine = Engine(Config(project_root=root, network=self.network))
            # Share one registry so the corpus is not re-fetched per package.
            if self._shared_registry is None:
                self._shared_registry = engine.registry
            else:
                engine.registry = self._shared_registry
            self._engines[root] = engine
        return engine

    @staticmethod
    def _root_for(path: Path) -> Path:
        for key in ("purelib", "platlib"):
            base = sysconfig.get_paths().get(key)
            if not base:
                continue
            try:
                relative = path.resolve().relative_to(Path(base))
            except (ValueError, OSError):
                continue
            return Path(base) / relative.parts[0] if relative.parts else Path(base)
        return path.parent

    @property
    def registry(self):
        return self._shared_registry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    files = corpus(args.limit)
    if not files:
        print("no site-packages sources found", file=sys.stderr)
        return 1

    rng = random.Random(SEED)
    engines = EngineSet(network=not args.offline)
    introspector = Introspector()

    print(f"corpus: {len(files)} real files from site-packages\n")

    checked, false_positives = precision_pass(files, engines)
    rate = len(false_positives) / checked if checked else 0.0
    print("PRECISION  (unmutated working code; any block is a false positive)")
    print(f"  files checked        {checked}")
    print(f"  false positives      {len(false_positives)}")
    print(f"  false positive rate  {rate:.3%}")
    for example in false_positives[:10]:
        print(f"    ! {example}")
    print()

    tallies = recall_pass(files, engines, introspector, rng)
    total = Tally()
    print("RECALL  (planted hallucinations, each verified absent before counting)")
    print(f"  {'':22} {'caught':>7} {'declined':>9} {'silent':>7}")
    for kind in sorted(tallies):
        tally = tallies[kind]
        total.caught += tally.caught
        total.abstained += tally.abstained
        total.silent += tally.silent
        print(
            f"  {kind:22} {tally.caught:7} {tally.abstained:9} {tally.silent:7}"
            f"   {tally.rate:6.1%} all / {tally.judged_rate:6.1%} judged"
        )
    print(
        f"  {'overall':22} {total.caught:7} {total.abstained:9} {total.silent:7}"
        f"   {total.rate:6.1%} all / {total.judged_rate:6.1%} judged"
    )

    merged: dict[str, int] = {}
    for tally in tallies.values():
        for reason, count in tally.reasons.items():
            merged[reason] = merged.get(reason, 0) + count
    if merged:
        print("\n  why bleurs declined to judge:")
        for reason, count in sorted(merged.items(), key=lambda kv: -kv[1]):
            print(f"    {count:4}  {reason}")

    silent = [e for t in tallies.values() for e in t.examples]
    if silent:
        print("\n  silent misses (looked at it, said nothing):")
        for example in silent[:8]:
            print(f"    {example}")

    return 0 if not false_positives else 1


if __name__ == "__main__":
    sys.exit(main())
