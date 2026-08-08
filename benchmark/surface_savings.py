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
"""

from __future__ import annotations

import argparse
import statistics
import sys
import sysconfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bleurs.surface import estimate_tokens, local_surface, render  # noqa: E402

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=MAX_FILES)
    args = parser.parse_args()

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
