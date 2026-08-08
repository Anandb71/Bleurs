"""Merge per-platform stdlib dumps into the shipped union table.

    gh run download <run-id> -D surfaces/
    python benchmark/merge_stdlib_surface.py surfaces/ \
        > src/bleurs/truth/platform_surface.json

The inputs come from `dump_stdlib_surface.py`, which every CI job runs. Three
operating systems times four Python versions gives twelve views of the modules
whose contents depend on the platform, and their union is the set of names that
exist *somewhere*.

Regenerate whenever the supported Python range changes. A stale table can only
cost recall: a name it fails to list keeps the old abstain-by-default
behaviour, so it can never produce a false block.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1

    root = Path(sys.argv[1])
    files = sorted(root.rglob("*.json"))
    if not files:
        print(f"no dumps under {root}", file=sys.stderr)
        return 1

    names: dict[str, set[str]] = collections.defaultdict(set)
    platforms: set[str] = set()
    pythons: set[str] = set()

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        platforms.add(payload.get("platform", "?"))
        pythons.add(payload.get("python", "?"))
        for module, members in (payload.get("modules") or {}).items():
            names[module].update(m for m in members if isinstance(m, str))

    json.dump(
        {
            "_generated_from": sorted(platforms),
            "_python_versions": sorted(pythons),
            "_sources": len(files),
            "modules": {k: sorted(v) for k, v in sorted(names.items())},
        },
        sys.stdout,
        indent=0,
        sort_keys=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
