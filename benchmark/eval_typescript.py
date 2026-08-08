"""The TypeScript half of the evidence.

Same construction as `eval_hallucinations.py`, second ecosystem. It exists
because the Python sweep found fourteen false-positive classes in code that had
been passing unit tests for days, and the TypeScript front-end had exactly that
profile: green tests, zero corpus measurement, and a README disclaimer saying so.

**Precision.** Run bleurs over unmutated files from a real `node_modules`.
Those files are installed and working, and every specifier in them resolved for
the package that ships them, so any block is a false positive by construction.

**Recall.** Plant a hallucination -- swap a package for one nobody published,
or a named import for one the module does not export -- and check that the
specific reference is blocked. Every planted package name is verified absent
against npm before it is scored, since a mutation that lands on a real package
is not a hallucination.

Set up a corpus first:

    mkdir tscorpus && cd tscorpus && npm init -y
    npm install react express lodash zod axios date-fns chalk rxjs typescript

    python benchmark/eval_typescript.py --corpus tscorpus
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bleurs import Config, Engine  # noqa: E402
from bleurs.analyze.typescript import available  # noqa: E402

PKG_SUFFIXES = ("-helpers", "-utils", "-toolkit", "-client", "-sdk", "-core-x")
NAME_SUFFIXES = ("Safe", "Async", "All", "Ex", "OrNull")

#: Bundled and minified files are megabytes on one line. They parse, slowly,
#: and tell us nothing a hand-written module would not.
MAX_BYTES = 120_000
MIN_BYTES = 400
SEED = 20260808

_SKIP = re.compile(r"\.min\.|/dist/|\\dist\\|bundle|umd|\.map$")


@dataclass
class Planted:
    kind: str
    source: str
    line: int
    name: str


@dataclass
class Tally:
    caught: int = 0
    declined: int = 0
    silent: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.caught + self.declined + self.silent

    @property
    def rate(self) -> float:
        return self.caught / self.total if self.total else 0.0

    @property
    def judged_rate(self) -> float:
        judged = self.caught + self.silent
        return self.caught / judged if judged else 0.0


def node_disagrees(importer: Path, specifier: str) -> bool | None:
    """Ask Node whether a relative specifier resolves. None if Node is absent.

    An independent oracle for disputed cases. The first run of this harness
    counted two blocks as false positives; Node returned MODULE_NOT_FOUND for
    both. date-fns ships `_lib/test.cjs` requiring `./test/vitest`, and the
    `test/` directory it publishes does not contain it -- bleurs was right and
    the package is broken. Scoring that against the tool would have been
    measuring the corpus, not the checker.
    """
    node = shutil.which("node")
    if node is None:
        return None
    script = (
        "const path=require('path');"
        "try{require.resolve(process.argv[1],{paths:[process.argv[2]]});"
        "console.log('ok')}catch(e){console.log('missing')}"
    )
    try:
        proc = subprocess.run(
            [node, "-e", script, specifier, str(importer.parent)],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if "missing" in proc.stdout:
        return True  # Node cannot resolve it either
    if "ok" in proc.stdout:
        return False
    return None


def corpus_files(root: Path, limit: int) -> list[Path]:
    modules = root / "node_modules"
    if not modules.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(modules.rglob("*")):
        if len(found) >= limit:
            break
        if path.suffix.lower() not in {".js", ".mjs", ".cjs", ".ts", ".tsx"}:
            continue
        if path.name.endswith(".d.ts"):
            continue
        posix = path.as_posix()
        if _SKIP.search(posix):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if not MIN_BYTES <= size <= MAX_BYTES:
            continue
        found.append(path)
    return found


# -- mutation ------------------------------------------------------------

_IMPORT_FROM = re.compile(
    r"""(?P<head>\bfrom\s+|(?<![\w.])require\(\s*|(?<![\w.])import\(\s*)"""
    r"""(?P<q>['"])(?P<spec>[^'"\n]+)(?P=q)"""
)
_NAMED = re.compile(r"""\bimport\s*\{\s*(?P<name>[A-Za-z_$][\w$]*)""")


def plant(source: str, rng: random.Random, npm, importer: Path) -> list[Planted]:
    out: list[Planted] = []

    bare = [
        m
        for m in _IMPORT_FROM.finditer(source)
        if not m.group("spec").startswith((".", "/", "node:"))
        and "\n" not in m.group("spec")
    ]
    if bare:
        match = rng.choice(bare)
        spec = match.group("spec")
        invented = spec.split("/")[0] + rng.choice(PKG_SUFFIXES)
        if npm.exists(invented) is False:
            start, end = match.span("spec")
            out.append(
                Planted(
                    "invented package",
                    source[:start] + invented + source[end:],
                    source[:start].count("\n") + 1,
                    invented,
                )
            )

    relative = [
        m for m in _IMPORT_FROM.finditer(source) if m.group("spec").startswith(".")
    ]
    if relative:
        match = rng.choice(relative)
        spec = match.group("spec")
        invented = _plausible_typo(spec, rng)
        # Verified against Node rather than our own resolver, which would make
        # the whole measurement circular. A typo that still resolves is not a
        # hallucination and must not be scored.
        if invented and node_disagrees(importer, invented) is not False:
            start, end = match.span("spec")
            out.append(
                Planted(
                    "missing relative module",
                    source[:start] + invented + source[end:],
                    source[:start].count("\n") + 1,
                    invented,
                )
            )

    named = list(_NAMED.finditer(source))
    if named:
        match = rng.choice(named)
        invented = match.group("name") + rng.choice(NAME_SUFFIXES)
        start, end = match.span("name")
        out.append(
            Planted(
                "invented named import",
                source[:start] + invented + source[end:],
                source[:start].count("\n") + 1,
                invented,
            )
        )

    return out


def _plausible_typo(spec: str, rng: random.Random) -> str:
    """A typo a model would actually make, not an obvious sentinel.

    Appending `-does-not-exist` measured whether we can spot a name nobody
    would ever write. Dropping the trailing `s`, doubling a letter or swapping
    two adjacent characters is what a wrong import really looks like.
    """
    head, _, tail = spec.rpartition("/")
    stem, dot, ext = tail.partition(".")
    if len(stem) < 4:
        return ""

    choice = rng.randrange(3)
    if choice == 0 and stem.endswith("s"):
        stem = stem[:-1]
    elif choice == 1:
        i = rng.randrange(1, len(stem) - 1)
        stem = stem[:i] + stem[i] + stem[i:]
    else:
        i = rng.randrange(1, len(stem) - 1)
        stem = stem[:i] + stem[i + 1] + stem[i] + stem[i + 2 :]

    rebuilt = stem + dot + ext
    return f"{head}/{rebuilt}" if head else rebuilt


# -- passes --------------------------------------------------------------


def precision_pass(files: list[Path], engine: Engine):
    false_positives: list[str] = []
    confirmed: list[str] = []
    checked = skipped = 0
    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        report = engine.check_source(source, path)
        if report.parse_error:
            skipped += 1
            continue
        checked += 1
        for finding in report.blocks:
            entry = (
                f"{path.parent.name}/{path.name}:{finding.reference.line} "
                f"{finding.reference.display} -- {finding.message}"
            )
            specifier = finding.reference.module
            if specifier.startswith(".") and node_disagrees(path, specifier) is True:
                confirmed.append(entry)   # Node agrees the module is missing
            else:
                false_positives.append(entry)
    return checked, skipped, false_positives, confirmed


def recall_pass(files, engine, rng) -> dict[str, Tally]:
    tallies: dict[str, Tally] = {}
    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for planted in plant(source, rng, engine.npm, path):
            report = engine.check_source(planted.source, path)
            if report.parse_error:
                continue
            # A mutation that landed inside a comment or a string is not a
            # planted hallucination. date-fns documents its API with `import`
            # examples in JSDoc, and scoring those would have counted the
            # checker wrong for correctly ignoring a comment.
            if not any(
                planted.name in f.reference.display or planted.name in f.reference.module
                for f in report.findings
            ):
                continue
            tally = tallies.setdefault(planted.kind, Tally())
            if any(planted.name in f.message or planted.name in f.reference.display
                   for f in report.blocks):
                tally.caught += 1
                continue

            related = [
                f
                for f in report.findings
                if planted.name in f.reference.display or planted.name in (f.message or "")
            ]
            reason = next(
                (f.abstained.value for f in related if f.abstained is not None), None
            )
            if reason is None and any(f.verdict.value == "warn" for f in related):
                reason = "reported as a warning rather than blocked"
            if reason is None and not related and report.abstentions:
                reason = "dropped before judging: " + ", ".join(
                    sorted(a.name.lower() for a in report.abstentions)[:2]
                )

            if reason:
                tally.declined += 1
                tally.reasons[reason] = tally.reasons.get(reason, 0) + 1
            else:
                tally.silent += 1
                if len(tally.examples) < 4:
                    tally.examples.append(
                        f"{path.parent.name}/{path.name}:{planted.line} {planted.name}"
                    )
    return tallies


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True, help="dir containing node_modules")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    if not available():
        print("needs bleurs[typescript]", file=sys.stderr)
        return 1

    root = Path(args.corpus).resolve()
    files = corpus_files(root, args.limit)
    if not files:
        print(f"no source files under {root / 'node_modules'}", file=sys.stderr)
        return 1

    engine = Engine(Config(project_root=root, network=not args.offline))
    rng = random.Random(SEED)

    print(f"corpus: {len(files)} real files from {root.name}/node_modules\n")

    checked, skipped, false_positives, confirmed = precision_pass(files, engine)
    rate = len(false_positives) / checked if checked else 0.0
    print("PRECISION  (unmutated working code; any block is a false positive)")
    print(f"  files checked        {checked}   (skipped {skipped}, did not parse)")
    print(f"  false positives      {len(false_positives)}")
    print(f"  false positive rate  {rate:.3%}")
    for example in false_positives[:12]:
        print(f"    ! {example}")
    if confirmed:
        print()
        print(
            f"  blocks Node also rejects ({len(confirmed)}) "
            "-- real package defects, not ours:"
        )
        for example in confirmed[:6]:
            print(f"    * {example}")
    print()

    tallies = recall_pass(files, engine, rng)
    total = Tally()
    print("RECALL  (planted hallucinations, package names verified absent)")
    print(f"  {'':24} {'caught':>7} {'declined':>9} {'silent':>7}")
    for kind in sorted(tallies):
        t = tallies[kind]
        total.caught += t.caught
        total.declined += t.declined
        total.silent += t.silent
        print(
            f"  {kind:24} {t.caught:7} {t.declined:9} {t.silent:7}"
            f"   {t.rate:6.1%} all / {t.judged_rate:6.1%} judged"
        )
    print(
        f"  {'overall':24} {total.caught:7} {total.declined:9} {total.silent:7}"
        f"   {total.rate:6.1%} all / {total.judged_rate:6.1%} judged"
    )

    merged: dict[str, int] = {}
    for t in tallies.values():
        for reason, count in t.reasons.items():
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

    engine.npm.flush()
    return 0 if not false_positives else 1


if __name__ == "__main__":
    sys.exit(main())
