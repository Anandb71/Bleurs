"""Tier 1 + 2: the standard library and what is actually installed here.

Both answers come from the interpreter itself rather than from a bundled list,
which means they are correct for the user's environment on the day they run it
instead of correct for whatever was true when we cut a release.
"""

from __future__ import annotations

import functools
import importlib.metadata
import importlib.util
import sys


#: Standard library modules whose *contents* differ between operating systems.
#:
#: This is the one place where introspection is structurally the wrong oracle.
#: `signal.SIGQUIT` and `socket.AF_UNIX` are real, and a Unix-only source file
#: may use them with no guard at all -- correctly, because that file never runs
#: on Windows. Introspecting on one platform cannot distinguish that from an
#: invented name, so for these containers absence proves nothing and we abstain.
#:
#: The list is deliberately shallow: `os` varies, `os.path` does not, so
#: `os.path.join_all` is still caught. Entries cost recall and buy correctness,
#: which is the trade this project makes everywhere. Additions welcome.
PLATFORM_VARYING_STDLIB: frozenset[str] = frozenset(
    {
        "signal",
        "socket",
        "os",
        "sys",
        "ssl",
        "select",
        "selectors",
        "errno",
        "mmap",
        "time",
        "stat",
        "ctypes",
        "subprocess",
        "resource",
        "termios",
        "fcntl",
        "msvcrt",
        "winreg",
        "curses",
        "multiprocessing",
        "asyncio",
        "shutil",
        "platform",
        "locale",
    }
)


def platform_varying(container: str) -> bool:
    """Is this container a stdlib module whose surface depends on the OS?"""
    return container in PLATFORM_VARYING_STDLIB


@functools.lru_cache(maxsize=1)
def stdlib_modules() -> frozenset[str]:
    """Top-level stdlib module names for the running interpreter."""
    names = set(sys.stdlib_module_names)
    names.update(sys.builtin_module_names)
    return frozenset(names)


def is_stdlib(module: str) -> bool:
    return module.split(".")[0] in stdlib_modules()


@functools.lru_cache(maxsize=1)
def installed_top_levels() -> frozenset[str]:
    """Every top-level importable name provided by an installed distribution.

    `packages_distributions()` reads the installed metadata -- the `top_level.txt`
    and `RECORD` files -- so it maps *import* names to distributions without
    importing anything. That distinction matters: this is the one tier that
    answers "does it exist" with no code execution at all.
    """
    names: set[str] = set()
    try:
        mapping = importlib.metadata.packages_distributions()
    except Exception:  # pragma: no cover - metadata can be malformed in the wild
        mapping = {}
    names.update(mapping.keys())
    return frozenset(names)


@functools.lru_cache(maxsize=1)
def distribution_names() -> frozenset[str]:
    """Installed distribution names, normalized per PEP 503."""
    out: set[str] = set()
    try:
        for dist in importlib.metadata.distributions():
            name = dist.metadata["Name"]
            if name:
                out.add(normalize_project_name(name))
    except Exception:  # pragma: no cover
        pass
    return frozenset(out)


@functools.lru_cache(maxsize=4096)
def top_level_module_exists(name: str) -> bool:
    """Is this top-level module importable in this environment?

    `find_spec` on a *top-level* name searches the path without executing the
    module. We never call it on a dotted path, because resolving `a.b` imports
    `a` for real -- and importing things is the introspection tier's job, done
    in a subprocess where it belongs.
    """
    top = name.split(".")[0]
    if top in stdlib_modules():
        return True
    if top in installed_top_levels():
        return True
    if top in sys.modules:
        return True
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError, AttributeError, TypeError):
        return False
    except Exception:  # pragma: no cover - defensive; find_spec runs finders
        return False


def normalize_project_name(name: str) -> str:
    """PEP 503 normalization: lowercase, runs of -_. collapse to a single -."""
    out: list[str] = []
    prev_dash = False
    for ch in name.lower():
        if ch in "-_.":
            if not prev_dash:
                out.append("-")
            prev_dash = True
        else:
            out.append(ch)
            prev_dash = False
    return "".join(out).strip("-")
