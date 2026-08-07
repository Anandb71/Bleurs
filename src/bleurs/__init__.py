"""bleurs -- a deterministic firewall for AI coding agents.

Blocks hallucinated imports and APIs before they reach disk, by checking every
reference a proposed edit makes against the environment it will actually run in.

    from bleurs import Engine, Config

    engine = Engine(Config(project_root=Path(".")))
    report = engine.check_source(proposed_code, Path("app/main.py"))
    if not report.ok:
        ...

The design invariant, in one line: a reference we could not resolve is never
treated as a reference that does not exist.
"""

from .engine import Config, Engine
from .refs import (
    AbstainReason,
    Confidence,
    Finding,
    Reference,
    RefKind,
    Report,
    Verdict,
)

__version__ = "0.1.0"

__all__ = [
    "AbstainReason",
    "Config",
    "Confidence",
    "Engine",
    "Finding",
    "RefKind",
    "Reference",
    "Report",
    "Verdict",
    "__version__",
]
