"""Task-scoped working sets.

`surface` answers "what is in this thing", but only if you already know what to
ask for. An agent starting a task does not, so it reads files to find out --
which is the problem the projection was supposed to solve.

A working set closes that gap without giving up determinism. The seed is a file
you are about to edit, not a sentence describing your intent, so the answer is a
*dependency closure* computed from the code rather than a guess ranked by
similarity:

    everything this file defines,
    plus the surface of everything it imports,
    plus that again for project-local modules, to a bounded depth,
    ordered by how likely you are to need it,
    truncated to a token budget.

That is "what can I call here" answered exactly, for roughly the cost of reading
one of the files it replaces.

The ordering matters as much as the content. A budget always runs out, and what
survives should be the seed's own surface first -- you are editing it -- then
the project code around it, then third-party libraries, which are the most
replaceable because the agent has seen them in training and can ask for any of
them by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .analyze import for_path
from .refs import RefKind
from .surface import (
    estimate_tokens,
    installed_surface,
    local_surface,
    render,
)

#: Modules with no useful surface to project.
SKIP_MODULES = frozenset({"__future__", "__main__", "typing", "typing_extensions"})

#: Third-party surfaces are capped: numpy alone would eat any budget, and the
#: agent can always ask for the rest by name.
THIRD_PARTY_LIMIT = 40
DEFAULT_BUDGET = 6000
DEFAULT_DEPTH = 1

#: Lower sorts first.
SEED = 0
PROJECT = 1
THIRD_PARTY = 2
TRANSITIVE = 3


@dataclass
class Section:
    """One rendered surface, with its place in the priority order."""

    key: str
    body: str
    priority: int
    tokens: int = 0

    def __post_init__(self) -> None:
        self.tokens = estimate_tokens(self.body)


@dataclass
class WorkingSet:
    sections: list[Section] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    budget: int = DEFAULT_BUDGET
    seeds: list[str] = field(default_factory=list)

    @property
    def used(self) -> int:
        return sum(s.tokens for s in self.sections)

    def render(self) -> str:
        head = [
            f"# working set for {', '.join(self.seeds)}",
            f"# {self.used} of {self.budget} tokens, {len(self.sections)} modules",
        ]
        if self.omitted:
            head.append(
                f"# omitted for budget: {', '.join(self.omitted[:12])}"
                + (" ..." if len(self.omitted) > 12 else "")
            )
        body = [s.body for s in self.sections]
        return "\n".join(head) + "\n\n" + "\n\n".join(body) + "\n"


def build(
    seeds: list[Path],
    *,
    project_root: Path | None = None,
    budget: int = DEFAULT_BUDGET,
    depth: int = DEFAULT_DEPTH,
    introspect: bool = True,
) -> WorkingSet:
    """Project everything needed to edit `seeds` correctly."""
    from .truth.local import LocalIndex

    index = LocalIndex(project_root) if project_root else None
    result = WorkingSet(budget=budget, seeds=[s.name for s in seeds])

    collected: dict[str, Section] = {}
    seen_modules: set[str] = set()

    frontier: list[tuple[Path, int]] = [(s, 0) for s in seeds]
    while frontier:
        path, level = frontier.pop(0)
        source = _read(path)
        if source is None:
            continue

        if level == 0:
            # The file being edited, private names included: you are inside it.
            projected = local_surface(path, source, private=True)
            if projected.ok:
                collected.setdefault(
                    str(path),
                    Section(path.name, render(projected), SEED),
                )

        for module in _imported_modules(source, path, index):
            if module in seen_modules:
                continue
            seen_modules.add(module)

            local_path = index.path_for(module) if index else None
            if local_path is not None:
                projected = local_surface(local_path, module_name=module)
                if projected.ok and projected.members:
                    collected.setdefault(
                        module,
                        Section(
                            module,
                            render(projected),
                            PROJECT if level == 0 else TRANSITIVE,
                        ),
                    )
                if level < depth:
                    frontier.append((local_path, level + 1))
                continue

            if not introspect or level > 0:
                continue
            projected = installed_surface(module)
            if projected.ok and projected.members:
                collected.setdefault(
                    module,
                    Section(
                        module,
                        render(projected, limit=THIRD_PARTY_LIMIT),
                        THIRD_PARTY,
                    ),
                )

    for section in sorted(collected.values(), key=lambda s: (s.priority, s.key)):
        if result.used + section.tokens <= budget:
            result.sections.append(section)
        else:
            result.omitted.append(section.key)

    return result


# -- helpers -------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _imported_modules(source: str, path: Path, index) -> list[str]:
    """Every module this file imports, in source order and deduplicated.

    Reuses the analyzer rather than a second import scanner. The last time this
    project had two parsers answering one question they drifted, and the older
    one silently missed everything defined inside a conditional.

    Relative imports are anchored here. Skipping them, as the first version
    did, threw away exactly the project-local modules a working set exists to
    gather -- and most real Python packages import their siblings that way.
    """
    from .truth.project import anchor_relative

    analyzer = for_path(path)
    if analyzer is None:
        return []
    analysis = analyzer.analyze(source, path)
    if analysis.parse_error:
        return []

    own = index.dotted_for(path) if index else None

    out: list[str] = []
    for reference in analysis.references:
        if reference.kind not in {RefKind.MODULE, RefKind.MEMBER}:
            continue

        module = reference.module
        if reference.level:
            if own is None:
                continue
            module = anchor_relative(
                own, path.name == "__init__.py", reference.level, module
            ) or ""

        if not module or module.startswith(".") or module in SKIP_MODULES:
            continue
        if module not in out:
            out.append(module)
    return out
