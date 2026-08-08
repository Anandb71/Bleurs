"""Ground-truth resolvers, in the order the engine consults them.

    Tier 0  local     the project's own modules        (filesystem, no exec)
    Tier 1  stdlib    the interpreter's own list       (no exec)
    Tier 2  env       installed distribution metadata  (no exec)
    Tier 3  introspect what the library really contains (subprocess)
    Tier 4  registry  does the name exist on PyPI      (network, cached)

Cheapest and most certain first. Every tier can answer "present", "absent", or
"I don't know", and only the middle answer is ever allowed to block a write.
"""

from .aliases import IMPORT_TO_DISTRIBUTION, install_name, known_import_name
from .env import (
    PLATFORM_VARYING_STDLIB,
    installed_top_levels,
    is_stdlib,
    normalize_project_name,
    platform_varying,
    stdlib_modules,
    top_level_module_exists,
)
from .introspect import Introspector, Probe
from .local import LocalIndex
from .registry import Registry

__all__ = [
    "IMPORT_TO_DISTRIBUTION",
    "Introspector",
    "LocalIndex",
    "Probe",
    "PLATFORM_VARYING_STDLIB",
    "Registry",
    "install_name",
    "installed_top_levels",
    "is_stdlib",
    "known_import_name",
    "normalize_project_name",
    "platform_varying",
    "stdlib_modules",
    "top_level_module_exists",
]
