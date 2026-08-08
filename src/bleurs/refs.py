"""Core data model.

The whole design turns on one distinction that most "AI code checker" tools blur:

    unresolved  !=  invalid

A reference we could not resolve tells us nothing. A reference we resolved and
found *absent* is a proven defect. Only the second one is allowed to block, and
keeping those two states in separate boxes is what buys the zero-false-positive
property. Everything in this module exists to keep that boundary sharp.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path


class RefKind(enum.Enum):
    """What sort of thing the source code claimed exists."""

    #: `import foo` / `import foo.bar` -- claims a module is importable.
    MODULE = "module"
    #: `from foo import bar` -- claims `bar` is a member of module `foo`.
    MEMBER = "member"
    #: `foo.bar(...)` where `foo` is a module binding -- claims an attribute path.
    ATTRIBUTE = "attribute"


class Confidence(enum.Enum):
    """How sure the engine is, in the only two directions that matter."""

    #: We resolved it and it exists.
    PRESENT = "present"
    #: We resolved the container and the thing is provably not in it.
    ABSENT = "absent"
    #: We could not resolve it. This is not evidence of anything.
    UNRESOLVED = "unresolved"


class Verdict(enum.Enum):
    """The engine's output for a single reference."""

    #: Resolved, or unresolvable. Either way the patch is not blocked on it.
    ALLOW = "allow"
    #: Real thing, but not present in this environment (a missing install).
    #: Worth saying out loud; never worth blocking a write over.
    WARN = "warn"
    #: Proven absent. This is a hallucination.
    BLOCK = "block"


#: Why the engine declined to judge a reference. Recorded rather than discarded
#: because the abstain set is the honest measure of the tool's blind spots --
#: and because `bleurs check --explain` shows it to the user instead of
#: pretending the file was fully verified.
class AbstainReason(enum.Enum):
    STAR_IMPORT = "wildcard import makes the namespace unknowable"
    SHADOWED = "name is reassigned in this file"
    OPTIONAL_IMPORT = "import is guarded by try/except"
    GUARDED = "reference is inside a try/except that handles it failing"
    PLATFORM_GUARDED = "reference is inside a platform or version test"
    DYNAMIC_MODULE = "module defines __getattr__, so any attribute may be valid"
    NOT_INTROSPECTABLE = "module could not be introspected safely"
    LOCAL_UNRESOLVED = "resolves to project-local code we could not index"
    RELATIVE_IMPORT = "relative import outside the indexed project root"
    NO_NETWORK = "package is not installed and the registry was unreachable"
    INTROSPECTION_DISABLED = "attribute checking is disabled (--no-introspect)"


@dataclass(frozen=True)
class Reference:
    """A single claim the source code makes about the outside world."""

    kind: RefKind
    #: Dotted module path the claim is rooted at, e.g. "numpy" or "os.path".
    module: str
    #: Dotted attribute path within the module, e.g. "linalg.norm". Empty for
    #: a bare MODULE reference.
    path: tuple[str, ...] = ()
    line: int = 0
    col: int = 0
    #: Source text as written, so error messages quote what the author typed
    #: rather than our normalized reconstruction of it.
    source_text: str = ""
    #: Set when the author has explicitly handled this reference failing to
    #: resolve -- a try/except around it, or a platform or version test
    #: enclosing it. Either way they have said in code that it may not be
    #: there, and contradicting them is not our place.
    guarded: bool = False
    #: Leading-dot count for a relative import. Non-zero means `module` is a
    #: fragment that only means something relative to the importing file, and
    #: the engine must anchor it against the project index before judging it.
    level: int = 0

    @property
    def dotted(self) -> str:
        return ".".join((self.module, *self.path))

    @property
    def display(self) -> str:
        return self.source_text or self.dotted

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.kind.value}:{self.dotted}@{self.line}"


@dataclass
class Finding:
    """The engine's judgement on one reference, with its receipts."""

    reference: Reference
    verdict: Verdict
    confidence: Confidence
    #: One-line statement of what we found. Written to be read by a human in a
    #: terminal at 2am, and by a model that has just had its edit rejected.
    message: str
    #: Nearest real name, when we have a container to compare against.
    suggestion: str | None = None
    #: Populated only when we abstained.
    abstained: AbstainReason | None = None
    #: The container's real API surface, attached to blocks. This is what turns
    #: a rejection into an answer: the agent is told not just that
    #: `json.loads_safe` is fiction, but what `json` actually offers -- so it
    #: can correct in one turn instead of guessing again.
    surface: str | None = None
    #: Which resolver tier produced the answer. Useful for auditing the
    #: zero-false-positive claim: every BLOCK should name the tier that proved it.
    resolver: str = ""

    @property
    def blocking(self) -> bool:
        return self.verdict is Verdict.BLOCK


@dataclass
class Report:
    """The verdict for one file."""

    path: Path
    findings: list[Finding] = field(default_factory=list)
    #: Reasons this file could not be fully verified, deduplicated.
    abstentions: set[AbstainReason] = field(default_factory=set)
    #: Set when the file did not parse. A syntax error is not a hallucination
    #: and is emphatically not ours to block -- the language's own tooling will
    #: say so more clearly than we can.
    parse_error: str | None = None

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict is Verdict.BLOCK]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict is Verdict.WARN]

    @property
    def ok(self) -> bool:
        return not self.blocks

    @property
    def checked(self) -> int:
        """References we actually reached a conclusion on."""
        return sum(
            1 for f in self.findings if f.confidence is not Confidence.UNRESOLVED
        )
