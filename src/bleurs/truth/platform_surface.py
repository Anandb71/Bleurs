"""What the platform-varying stdlib modules contain on *other* platforms.

Introspection runs on one machine, which is why `signal.SIGQUIT` -- real on
Unix, absent on Windows -- had to abstain alongside `signal.register_all`,
which is real nowhere. That single rule was the largest remaining source of
declined verdicts.

`platform_surface.json` is the union of what twelve CI jobs actually saw:
Linux, macOS and Windows across Python 3.10 through 3.13. A name in the union
exists somewhere and must never be blocked. A name absent from all twelve, in a
module the table covers, exists nowhere -- and that is a fact, not an inference.

The table is only ever allowed to *permit*. If it has no entry for a module, or
the running interpreter is outside the versions it was generated from, the
answer is None and the caller keeps abstaining. An incomplete table costs
recall and can never cause a false block, which is what makes it safe to ship
a snapshot and regenerate it lazily.

Regenerate with `benchmark/merge_stdlib_surface.py` after downloading the CI
artifacts; see that file for the exact commands.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

_TABLE = Path(__file__).with_name("platform_surface.json")


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    try:
        payload = json.loads(_TABLE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@functools.lru_cache(maxsize=1)
def covered_versions() -> frozenset[str]:
    return frozenset(_data().get("_python_versions") or ())


@functools.lru_cache(maxsize=1)
def _modules() -> dict[str, frozenset[str]]:
    raw = _data().get("modules") or {}
    return {k: frozenset(v) for k, v in raw.items() if isinstance(v, list)}


def running_version_is_covered() -> bool:
    """Was the table generated for the interpreter we are running on?

    A table built for 3.10-3.13 says nothing useful about 3.15, where names may
    have been added or removed. Outside the covered range we decline to use it.
    """
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return version in covered_versions()


def exists_somewhere(container: str, name: str) -> bool | None:
    """Does `container.name` exist on any platform we have data for?

    True  -- seen on at least one platform; never block it.
    False -- absent from every platform in the table; safe to block.
    None  -- no data for this container, or an interpreter the table does not
             cover. The caller must go on abstaining.
    """
    if not name or not running_version_is_covered():
        return None
    members = _modules().get(container)
    if members is None:
        return None
    return name in members
