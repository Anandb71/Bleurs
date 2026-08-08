"""The router.

Takes references from a language front-end, walks them down the resolver tiers,
and produces a verdict for each. Every rule in here exists to serve one
invariant, stated once so the rest of the file can be read against it:

    BLOCK requires positive evidence of absence.

Not "we couldn't find it". Not "it's probably wrong". Absence, demonstrated by a
named tier that looked in a container it successfully opened. Anything short of
that is ALLOW, and the reason we abstained is recorded so the user can see the
shape of what we did not check.

The asymmetry is intentional. A false negative means a hallucination slips
through to a test run that would have caught it anyway. A false positive means
the tool blocks correct code, and the user turns it off -- after which it
catches nothing at all, forever. Recall is worth spending; precision is not.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from .analyze import for_path
from .refs import (
    AbstainReason,
    Confidence,
    Finding,
    Reference,
    RefKind,
    Report,
    Verdict,
)
from .truth import (
    Introspector,
    LocalIndex,
    Probe,
    Registry,
    install_name,
    is_stdlib,
    known_import_name,
    top_level_module_exists,
)
from .truth.aliases import NAMESPACE_ROOTS


@dataclass
class Config:
    """Knobs. Defaults are chosen so that `bleurs check` is safe to leave on."""

    #: Index the project's own modules so local helpers resolve.
    project_root: Path | None = None
    #: Load installed libraries in a subprocess to check attributes.
    #: Off means we can still catch fake packages, but not fake APIs.
    introspect: bool = True
    #: Consult PyPI for names not installed locally.
    network: bool = True
    #: When a package is neither installed nor on PyPI, block rather than warn.
    strict_imports: bool = True
    introspect_timeout: float = 20.0


class Engine:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.local = (
            LocalIndex(self.config.project_root) if self.config.project_root else None
        )
        self.introspector = Introspector(
            enabled=self.config.introspect,
            timeout=self.config.introspect_timeout,
        )
        self.registry = Registry(enabled=self.config.network)

    # -- entry points ----------------------------------------------------

    def check_source(self, source: str, path: Path) -> Report:
        """Verify source text that may not exist on disk yet.

        This is the signature that matters: an agent's proposed edit is a
        string, and checking it *before* the write is the whole point.
        """
        report = Report(path=path)
        analyzer = for_path(path)
        if analyzer is None:
            return report

        analysis = analyzer.analyze(source, path)
        report.abstentions |= analysis.abstentions
        if analysis.parse_error:
            report.parse_error = analysis.parse_error
            return report

        references: list[Reference] = []
        for reference in analysis.references:
            anchored = self._anchor(reference, path)
            if anchored is None:
                report.abstentions.add(AbstainReason.RELATIVE_IMPORT)
                continue
            references.append(anchored)

        probes = self._gather_probes(references)
        for reference in references:
            finding = self._judge(reference, probes)
            report.findings.append(finding)
            if finding.abstained is not None:
                report.abstentions.add(finding.abstained)

        self._attach_surfaces(report)

        self.registry.flush()
        if self.registry.network_failed:
            report.abstentions.add(AbstainReason.NO_NETWORK)
        if not self.config.introspect:
            report.abstentions.add(AbstainReason.INTROSPECTION_DISABLED)
        return report

    def check_file(self, path: Path) -> Report:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report = Report(path=path)
            report.parse_error = str(exc)
            return report
        return self.check_source(source, path)

    # -- grounding -------------------------------------------------------

    def _attach_surfaces(self, report: Report) -> None:
        """Give every block the real API of the thing it got wrong.

        Rejecting an edit without saying what was available leaves the agent
        exactly where it started, and it will guess again. Attaching the
        container's actual surface turns the block into the answer -- and costs
        one extra subprocess, only on the path where we were about to reject
        the write anyway.
        """
        blocks = report.blocks
        if not blocks:
            return

        from .surface import local_surface, render

        wanted: list[tuple[str, tuple[str, ...]]] = []
        for finding in blocks:
            ref = finding.reference
            if ref.kind is RefKind.MODULE or self._is_local(ref.module):
                continue
            # Project the container, not the missing name: the whole point is
            # to show what is there instead of what is not.
            wanted.append((ref.module, ref.path[:-1] if ref.path else ()))

        surfaces = self.introspector.surfaces(wanted) if wanted else {}

        for finding in blocks:
            ref = finding.reference
            if self._is_local(ref.module) and self.local is not None:
                path = self.local.path_for(ref.module)
                if path is not None:
                    projected = local_surface(path, module_name=ref.module)
                    if projected.ok and projected.members:
                        finding.surface = render(projected, summaries=False, limit=60)
                continue

            key = (ref.module, ref.path[:-1] if ref.path else ())
            projected = surfaces.get(key)
            if projected is not None and projected.ok and projected.members:
                finding.surface = render(projected, summaries=False, limit=60)

    # -- relative imports ------------------------------------------------

    def _anchor(self, ref: Reference, path: Path) -> Reference | None:
        """Turn `from ..refs import X` into a claim about `pkg.refs`.

        Returns None when the file's own position in the project is unknown,
        because a relative import we cannot anchor is a question we cannot ask,
        let alone answer.
        """
        if not ref.level:
            return ref
        if self.local is None:
            return None

        own = self.local.dotted_for(path)
        if own is None:
            return None

        parts = own.split(".")
        # `_dotted_name` already strips `__init__`, so a package's own dotted
        # name *is* its package name; a module's is one level deeper.
        if path.name != "__init__.py":
            parts = parts[:-1]
        if ref.level > 1:
            parts = parts[: -(ref.level - 1)]
        if not parts:
            return None

        if ref.module:
            parts.append(ref.module)

        return Reference(
            kind=ref.kind,
            module=".".join(parts),
            path=ref.path,
            line=ref.line,
            col=ref.col,
            source_text=ref.source_text,
            guarded=ref.guarded,
        )

    # -- probe batching --------------------------------------------------

    def _gather_probes(
        self, references: list[Reference]
    ) -> dict[tuple[str, tuple[str, ...]], Probe]:
        """Collect every introspection question, then ask them all at once."""
        if not self.config.introspect:
            return {}

        queries: list[tuple[str, tuple[str, ...]]] = []
        for ref in references:
            if self._is_local(ref.module):
                continue
            if ref.kind is RefKind.MODULE:
                # Only dotted modules need loading; a top-level name is settled
                # by metadata, and metadata never executes anything.
                if "." in ref.module:
                    queries.append((ref.module, ()))
            else:
                queries.append((ref.module, ref.path))
        return self.introspector.resolve(queries)

    # -- judgement -------------------------------------------------------

    def _judge(
        self, ref: Reference, probes: dict[tuple[str, tuple[str, ...]], Probe]
    ) -> Finding:
        finding = (
            self._judge_module(ref, probes)
            if ref.kind is RefKind.MODULE
            else self._judge_member(ref, probes)
        )

        # A guarded reference can still be reported, but never blocked. The
        # author wrapped it in a try/except or a platform test, which is them
        # telling us in code that it may not resolve here. `ctypes.windll` is
        # absent on Linux and that is not a defect.
        if ref.guarded and finding.verdict is Verdict.BLOCK:
            return Finding(
                reference=ref,
                verdict=Verdict.WARN,
                confidence=finding.confidence,
                message=finding.message,
                suggestion=finding.suggestion,
                abstained=AbstainReason.GUARDED,
                resolver=finding.resolver,
            )
        return finding

    # -- modules ---------------------------------------------------------

    def _judge_module(
        self, ref: Reference, probes: dict[tuple[str, tuple[str, ...]], Probe]
    ) -> Finding:
        module = ref.module
        top = module.split(".")[0]

        if is_stdlib(module) and "." not in module:
            # Only the top-level name is settled by the stdlib list. Taking
            # this shortcut for dotted names would wave through
            # `import os.nonexistent_submodule`, which is exactly the kind of
            # plausible-looking invention we exist to catch.
            return _allow(ref, "standard library", "stdlib")

        if self._is_local(module):
            return _allow(ref, "defined in this project", "local")

        if top_level_module_exists(top):
            if "." not in module:
                return _allow(ref, "installed", "env")
            probe = probes.get((module, ()))
            if probe is None:
                return _abstain(
                    ref,
                    AbstainReason.INTROSPECTION_DISABLED
                    if not self.config.introspect
                    else AbstainReason.NOT_INTROSPECTABLE,
                    f"{top} is installed; submodule not verified",
                )
            if probe.module_ok:
                return _allow(ref, "installed", "introspect")
            if probe.proves_module_absent(module):
                return Finding(
                    reference=ref,
                    verdict=Verdict.BLOCK,
                    confidence=Confidence.ABSENT,
                    message=f"{top} is installed but has no submodule {module!r}",
                    resolver="introspect",
                )
            # Import blew up for some unrelated reason. Not our verdict to give.
            return _abstain(
                ref,
                AbstainReason.NOT_INTROSPECTABLE,
                f"could not load {module}: {probe.module_error}",
            )

        # -- not installed. The only remaining question is whether it is real.
        if top in NAMESPACE_ROOTS or known_import_name(top):
            return Finding(
                reference=ref,
                verdict=Verdict.WARN,
                confidence=Confidence.PRESENT,
                message=f"{module} is not installed",
                suggestion=f"pip install {install_name(top)}",
                resolver="aliases",
            )

        if self.local is not None and self.local.is_local_root(top):
            return _allow(ref, "project-local package", "local")

        exists = self.registry.exists(install_name(top))
        if exists is None:
            return _abstain(
                ref,
                AbstainReason.NO_NETWORK,
                f"{module} is not installed and PyPI could not be reached",
            )
        if exists:
            return Finding(
                reference=ref,
                verdict=Verdict.WARN,
                confidence=Confidence.PRESENT,
                message=f"{module} exists on PyPI but is not installed",
                suggestion=f"pip install {install_name(top)}",
                resolver="registry",
            )

        # Proven: not installed, not stdlib, not local, not a known alias, and
        # no project of that name has ever been published. This is invented.
        verdict = Verdict.BLOCK if self.config.strict_imports else Verdict.WARN
        return Finding(
            reference=ref,
            verdict=verdict,
            confidence=Confidence.ABSENT,
            message=f"no package named {top!r} exists on PyPI",
            suggestion=self._nearest_installed(top),
            resolver="registry",
        )

    # -- members and attributes ------------------------------------------

    def _judge_member(
        self, ref: Reference, probes: dict[tuple[str, tuple[str, ...]], Probe]
    ) -> Finding:
        module = ref.module
        wanted = ".".join(ref.path)

        # Project-local modules are answered from the index, never by import --
        # loading the user's own half-written code would be both slow and rude.
        if self._is_local(module):
            return self._judge_local_member(ref)

        top = module.split(".")[0]
        if self.local is not None and self.local.is_local_root(top):
            # A project package we know about, but this exact module is not in
            # the index. Could be generated, could be excluded by a skip rule,
            # could be a file the agent is about to create in the same batch.
            # None of those are hallucinations we can prove.
            return _abstain(
                ref,
                AbstainReason.LOCAL_UNRESOLVED,
                f"{module} is project-local but not indexed",
            )

        if not is_stdlib(module) and not top_level_module_exists(top):
            # The module itself is the problem. Judge that instead of inventing
            # a second finding about a member of something that isn't there.
            module_ref = Reference(
                kind=RefKind.MODULE,
                module=module,
                line=ref.line,
                col=ref.col,
                source_text=ref.source_text,
                guarded=ref.guarded,
            )
            return self._judge_module(module_ref, probes)

        probe = probes.get((module, ref.path))
        if probe is None:
            return _abstain(
                ref,
                AbstainReason.INTROSPECTION_DISABLED
                if not self.config.introspect
                else AbstainReason.NOT_INTROSPECTABLE,
                f"{ref.dotted} not verified",
            )

        if probe.dynamic:
            return _abstain(
                ref,
                AbstainReason.DYNAMIC_MODULE,
                f"{module} resolves attributes dynamically",
            )
        if probe.resolved:
            return _allow(ref, "exists", "introspect")
        if not probe.module_ok:
            if probe.proves_module_absent(module):
                return Finding(
                    reference=ref,
                    verdict=Verdict.BLOCK,
                    confidence=Confidence.ABSENT,
                    message=f"no module named {module!r}",
                    resolver="introspect",
                )
            return _abstain(
                ref,
                AbstainReason.NOT_INTROSPECTABLE,
                f"could not load {module}: {probe.module_error}",
            )

        # Loaded the container, walked it, and the name was not there.
        suggestion = _closest(probe.missing_at or wanted, probe.candidates)
        return Finding(
            reference=ref,
            verdict=Verdict.BLOCK,
            confidence=Confidence.ABSENT,
            message=(
                f"{probe.container} has no attribute {probe.missing_at!r}"
                if probe.missing_at
                else f"{ref.dotted} does not exist"
            ),
            suggestion=(
                f"{probe.container}.{suggestion}" if suggestion else None
            ),
            resolver="introspect",
        )

    def _judge_local_member(self, ref: Reference) -> Finding:
        assert self.local is not None
        module = ref.module
        name = ref.path[0] if ref.path else ""

        # `from pkg import subpkg` -- the member is itself a module.
        if self.local.has_module(f"{module}.{name}"):
            return _allow(ref, "project submodule", "local")

        members = self.local.members(module)
        if members is None:
            return _abstain(
                ref,
                AbstainReason.LOCAL_UNRESOLVED,
                f"could not read the surface of {module}",
            )
        if name in members:
            return _allow(ref, "defined in this project", "local")

        # Attribute chains deeper than one level would need real type
        # inference to follow. We only claim what we can prove.
        if ref.kind is RefKind.ATTRIBUTE and len(ref.path) > 1:
            return _abstain(
                ref, AbstainReason.LOCAL_UNRESOLVED, "nested project attribute"
            )

        return Finding(
            reference=ref,
            verdict=Verdict.BLOCK,
            confidence=Confidence.ABSENT,
            message=f"{module} defines no {name!r}",
            suggestion=_closest(name, tuple(members)),
            resolver="local",
        )

    # -- helpers ---------------------------------------------------------

    def _is_local(self, module: str) -> bool:
        return self.local is not None and self.local.has_module(module)

    def _nearest_installed(self, name: str) -> str | None:
        from .truth import installed_top_levels

        match = _closest(name, tuple(installed_top_levels()))
        return f"did you mean {match!r}?" if match else None


def _allow(ref: Reference, message: str, resolver: str) -> Finding:
    return Finding(
        reference=ref,
        verdict=Verdict.ALLOW,
        confidence=Confidence.PRESENT,
        message=message,
        resolver=resolver,
    )


def _abstain(ref: Reference, reason: AbstainReason, message: str) -> Finding:
    return Finding(
        reference=ref,
        verdict=Verdict.ALLOW,
        confidence=Confidence.UNRESOLVED,
        message=message,
        abstained=reason,
        resolver="",
    )


def _closest(name: str, candidates: tuple[str, ...]) -> str | None:
    """Nearest real name, or nothing.

    The cutoff is high on purpose. A confident wrong suggestion is worse than
    no suggestion: the agent will take it, and now the tool has authored the
    bug instead of catching it.
    """
    if not name or not candidates:
        return None
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.8)
    return matches[0] if matches else None
