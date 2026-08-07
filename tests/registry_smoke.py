"""Live check that the PyPI tier still behaves.

Kept out of the pytest suite on purpose. Everything under `tests/` stubs the
network so the suite is deterministic and works offline; this module is the one
place that talks to the real registry, and CI is allowed to fail it without
turning the build red.

    python tests/registry_smoke.py
"""

from __future__ import annotations

import sys

from bleurs.truth.registry import Registry

#: (name, should_exist). The absent names are chosen to be plausible enough
#: that a model might invent them, and specific enough that nobody will ever
#: register them.
CASES = [
    ("requests", True),
    ("PyYAML", True),
    ("scikit-learn", True),
    ("bleurs-registry-smoke-test-does-not-exist", False),
    ("langchain-vectorstore-utils-nonexistent-xyzzy", False),
]


def main() -> int:
    registry = Registry()
    failures = 0

    for name, expected in CASES:
        actual = registry.exists(name)
        if actual is None:
            print(f"  SKIP  {name}: registry unreachable")
            continue
        status = "ok" if actual is expected else "FAIL"
        if actual is not expected:
            failures += 1
        print(f"  {status:<5} {name}: exists={actual} expected={expected}")

    registry.flush()

    if registry.network_failed:
        print("\nregistry was unreachable; nothing proven either way")
        return 0

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
