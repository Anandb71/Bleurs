"""Dump this platform's stdlib surface for the modules that vary by OS.

Introspecting on one machine cannot tell `signal.SIGQUIT`, which is real on
Unix, from `signal.register_all`, which is real nowhere. So the checker
abstains on both, and that single rule is the largest remaining source of
declined verdicts.

The fix is to stop introspecting one platform. CI already runs Linux, macOS and
Windows across four Python versions, so each of those twelve jobs can report
what it actually sees, and the union is the set of names that exist *somewhere*.
A name absent from the union is absent everywhere.

The union is only ever allowed to make the checker more permissive. A missing
entry costs recall; it can never cause a false block, because a name we fail to
record simply keeps the old abstain-by-default behaviour.

    python benchmark/dump_stdlib_surface.py > surface-linux-3.12.json
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bleurs.truth.env import PLATFORM_VARYING_STDLIB  # noqa: E402


def collect() -> dict[str, list[str]]:
    surface: dict[str, list[str]] = {}
    for name in sorted(PLATFORM_VARYING_STDLIB):
        try:
            module = __import__(name, fromlist=["__name__"])
        except BaseException:
            # Unimportable here is exactly the information another platform
            # will supply. Recording nothing is correct.
            continue
        try:
            names = sorted(n for n in dir(module) if isinstance(n, str))
        except BaseException:
            continue
        surface[name] = names
    return surface


def main() -> int:
    payload = {
        "platform": sys.platform,
        "machine": platform.machine(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "modules": collect(),
    }
    json.dump(payload, sys.stdout, indent=0, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
