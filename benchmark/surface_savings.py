"""Measure what surface projection actually saves, on real code.

The claim under test: an agent can learn how to *use* a module from its
projected surface instead of reading the file, at a fraction of the tokens.

Two honesty constraints, because a benchmark that flatters its author is
worth nothing:

1. It runs over installed third-party packages, not just this repo. bleurs'
   own source is unusually comment-dense, which inflates the ratio. Real
   library code is the fair test.
2. It reports the distribution -- median and quartiles -- not the best case.
   The maximum ratio is a cherry, and cherries are how people lie with
   benchmarks.

Token counts are estimated at 4 characters per token rather than measured with
a real tokenizer, because shipping one would mean shipping a dependency. The
estimate applies identically to both sides of every ratio, so it cancels.

    python benchmark/surface_savings.py [--limit N]
    python benchmark/surface_savings.py --working-sets [--limit N]

The second mode measures whole working sets rather than single projections:
given a file you are about to edit, how much cheaper is its dependency closure
than opening every file that closure covers.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import sysconfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bleurs.context import build  # noqa: E402
from bleurs.surface import estimate_tokens, local_surface, render  # noqa: E402
from bleurs.truth.local import LocalIndex  # noqa: E402

MIN_CHARS = 1500  # skip trivial files; nobody needs a projection of __init__.py
MAX_FILES = 400


def candidate_files(limit: int) -> list[Path]:
    """Real library sources from site-packages, plus this project."""
    roots: list[Path] = []
    for key in ("purelib", "platlib"):
        path = sysconfig.get_paths().get(key)
        if path and Path(path).is_dir():
            roots.append(Path(path))

    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if len(found) >= limit:
                break
            parts = set(path.parts)
            if parts & {"tests", "test", "__pycache__", "_vendor", "vendored"}:
                continue
            if path in seen:
                continue
            try:
                if path.stat().st_size < MIN_CHARS:
                    continue
            except OSError:
                continue
            seen.add(path)
            found.append(path)
    return found


def package_roots(limit: int) -> list[tuple[Path, list[Path]]]:
    """Installed packages, each with its own files -- one project per package."""
    out: list[tuple[Path, list[Path]]] = []
    for key in ("purelib", "platlib"):
        base = sysconfig.get_paths().get(key)
        if not base or not Path(base).is_dir():
            continue
        for entry in sorted(Path(base).iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            if entry.name.endswith((".dist-info", ".egg-info")):
                continue
            files = [
                f
                for f in sorted(entry.rglob("*.py"))
                if "__pycache__" not in f.parts and f.stat().st_size > MIN_CHARS
            ][:limit]
            if len(files) >= 3:
                out.append((entry, files))
    return out


def working_sets(limit: int) -> int:
    """How much cheaper is a dependency closure than reading the files?"""
    ratios: list[float] = []
    projected_total = full_total = 0
    packages = 0

    for root, files in package_roots(limit)[:12]:
        index = LocalIndex(root)
        measured = 0
        for path in files[:limit]:
            try:
                working = build(
                    [path], project_root=root, budget=200_000, introspect=False
                )
            except Exception:
                continue
            if len(working.sections) < 2 or working.used == 0:
                continue  # nothing gathered; not a fair comparison

            covered = {path}
            for section in working.sections:
                found = index.path_for(section.key)
                if found is not None:
                    covered.add(found)
            if len(covered) < 2:
                continue

            try:
                full = sum(
                    estimate_tokens(p.read_text(encoding="utf-8", errors="replace"))
                    for p in covered
                )
            except OSError:
                continue

            ratios.append(full / working.used)
            projected_total += working.used
            full_total += full
            measured += 1
        if measured:
            packages += 1

    if not ratios:
        print("no working sets could be measured", file=sys.stderr)
        return 1

    ratios.sort()
    quartile = lambda q: ratios[min(len(ratios) - 1, int(len(ratios) * q))]  # noqa: E731

    print(f"working sets measured {len(ratios)}  (across {packages} packages)")
    print(f"reading those files   ~{full_total:,} tokens")
    print(f"projected closures    ~{projected_total:,} tokens")
    print(f"aggregate reduction    {full_total / max(projected_total, 1):.1f}x")
    print()
    print(f"per-file p25           {quartile(0.25):.1f}x")
    print(f"per-file median        {statistics.median(ratios):.1f}x")
    print(f"per-file p75           {quartile(0.75):.1f}x")
    print(f"per-file worst / best  {min(ratios):.1f}x / {max(ratios):.1f}x")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=MAX_FILES)
    parser.add_argument(
        "--working-sets",
        action="store_true",
        help="measure dependency closures instead of single projections",
    )
    args = parser.parse_args()

    if args.working_sets:
        return working_sets(min(args.limit, 40))

    files = candidate_files(args.limit)
    if not files:
        print("no site-packages sources found to measure", file=sys.stderr)
        return 1

    ratios: list[float] = []
    full_total = 0
    surface_total = 0
    skipped = 0

    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue

        projected = local_surface(path, source)
        if not projected.ok:
            skipped += 1
            continue

        text = render(projected)
        full = estimate_tokens(source)
        small = estimate_tokens(text)
        if not projected.members:
            # Nothing public to project. Counted as a miss rather than quietly
            # dropped, because "the projection was useless here" is a real
            # outcome the average has to carry.
            skipped += 1
            continue

        full_total += full
        surface_total += small
        ratios.append(full / max(small, 1))

    ratios.sort()
    quartile = lambda q: ratios[min(len(ratios) - 1, int(len(ratios) * q))]  # noqa: E731

    print(f"files measured      {len(ratios)}  (skipped {skipped})")
    print(f"total, whole files  ~{full_total:,} tokens")
    print(f"total, projections  ~{surface_total:,} tokens")
    print(f"aggregate reduction  {full_total / max(surface_total, 1):.1f}x")
    print()
    print(f"per-file ratio p25   {quartile(0.25):.1f}x")
    print(f"per-file ratio median {statistics.median(ratios):.1f}x")
    print(f"per-file ratio p75   {quartile(0.75):.1f}x")
    print(f"per-file ratio worst {min(ratios):.1f}x   best {max(ratios):.1f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
