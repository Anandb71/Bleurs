from __future__ import annotations

from dataclasses import dataclass, field

from ..refs import AbstainReason, Reference


@dataclass
class AnalysisResult:
    """What a language front-end hands to the engine."""

    references: list[Reference] = field(default_factory=list)
    #: File-wide reasons verification is incomplete (e.g. a wildcard import).
    abstentions: set[AbstainReason] = field(default_factory=set)
    parse_error: str | None = None
