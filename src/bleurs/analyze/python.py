"""Python front-end.

Extracts three kinds of claim:

    import numpy                  -> MODULE     "numpy is importable"
    from pandas import DataFrame  -> MEMBER     "DataFrame is a member of pandas"
    np.linalg.norm(x)             -> ATTRIBUTE  "numpy.linalg.norm exists"

The third one is only emitted when the root name is *provably* still bound to
the module it was imported as. Python lets you rebind anything at any time, so
the analyzer runs a deliberately paranoid shadow pass first: if a name is
touched anywhere in the file by anything other than an import, every attribute
reference through it is dropped. That over-abstains -- `np` reassigned inside
one unrelated function kills checking for the whole file -- and that is the
correct trade. A dropped check costs us recall. A wrong check costs us the
user's trust, permanently.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ..refs import AbstainReason, Reference, RefKind
from .result import AnalysisResult

#: Exception types that, when caught around an import, mean "optional dependency".
_OPTIONAL_GUARDS = frozenset(
    {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
)


class PythonAnalyzer:
    name = "python"
    extensions = (".py", ".pyi")

    def analyze(self, source: str, path: Path) -> AnalysisResult:
        result = AnalysisResult()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            # Not our business. Python's own error message is better than
            # anything we would invent, and a file that does not parse cannot
            # be hallucinating an API -- it is simply broken, loudly, already.
            result.parse_error = f"line {exc.lineno}: {exc.msg}"
            return result

        optional_imports = _optional_import_nodes(tree)
        bindings, shadowed, star = _collect_bindings(tree)

        if star:
            result.abstentions.add(AbstainReason.STAR_IMPORT)

        self._collect_imports(tree, source, optional_imports, result)
        self._collect_attributes(tree, source, bindings, shadowed, result)
        return result

    # -- imports ---------------------------------------------------------

    def _collect_imports(
        self,
        tree: ast.AST,
        source: str,
        optional_imports: set[int],
        result: AnalysisResult,
    ) -> None:
        for node in ast.walk(tree):
            optional = id(node) in optional_imports

            if isinstance(node, ast.Import):
                for alias in node.names:
                    result.references.append(
                        Reference(
                            kind=RefKind.MODULE,
                            module=alias.name,
                            line=node.lineno,
                            col=node.col_offset,
                            source_text=f"import {alias.name}",
                            optional=optional,
                        )
                    )

            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    # Relative imports carry no meaning on their own -- `.utils`
                    # names a different module depending on which file wrote it.
                    # Pass the fragment and the dot count through; only the
                    # engine knows where this file sits in the project.
                    self._relative_import(node, result)
                    continue
                if node.module is None:
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        result.abstentions.add(AbstainReason.STAR_IMPORT)
                        # The module itself is still a checkable claim.
                        result.references.append(
                            Reference(
                                kind=RefKind.MODULE,
                                module=node.module,
                                line=node.lineno,
                                col=node.col_offset,
                                source_text=f"from {node.module} import *",
                                optional=optional,
                            )
                        )
                        continue
                    result.references.append(
                        Reference(
                            kind=RefKind.MEMBER,
                            module=node.module,
                            path=(alias.name,),
                            line=node.lineno,
                            col=node.col_offset,
                            source_text=f"from {node.module} import {alias.name}",
                            optional=optional,
                        )
                    )

            elif isinstance(node, ast.Call):
                ref = _dynamic_import_reference(node, optional)
                if ref is not None:
                    result.references.append(ref)

    def _relative_import(self, node: ast.ImportFrom, result: AnalysisResult) -> None:
        dots = "." * node.level
        fragment = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                result.abstentions.add(AbstainReason.STAR_IMPORT)
                continue
            result.references.append(
                Reference(
                    kind=RefKind.MEMBER,
                    module=fragment,
                    path=(alias.name,),
                    line=node.lineno,
                    col=node.col_offset,
                    source_text=f"from {dots}{fragment} import {alias.name}",
                    level=node.level,
                )
            )

    # -- attribute access ------------------------------------------------

    def _collect_attributes(
        self,
        tree: ast.AST,
        source: str,
        bindings: dict[str, str],
        shadowed: set[str],
        result: AnalysisResult,
    ) -> None:
        # An attribute chain `a.b.c` is three nested nodes. Only the outermost
        # one is a complete claim; the inner ones are prefixes of it and would
        # produce duplicate, weaker findings.
        inner: set[int] = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
        }

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or id(node) in inner:
                continue

            unwound = _unwind_attribute(node)
            if unwound is None:
                continue
            root, attrs = unwound

            if root not in bindings:
                continue
            if root in shadowed:
                result.abstentions.add(AbstainReason.SHADOWED)
                continue

            module = bindings[root]
            result.references.append(
                Reference(
                    kind=RefKind.ATTRIBUTE,
                    module=module,
                    path=tuple(attrs),
                    line=node.lineno,
                    col=node.col_offset,
                    source_text=".".join((root, *attrs)),
                )
            )


# -- helpers -------------------------------------------------------------


def _unwind_attribute(node: ast.Attribute) -> tuple[str, list[str]] | None:
    """Flatten `a.b.c` into ("a", ["b", "c"]).

    Returns None when the chain is not rooted at a plain name -- `f().x` and
    `d["k"].y` are unknowable to us and must not produce a claim.
    """
    attrs: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        attrs.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    attrs.reverse()
    return cur.id, attrs


def _dynamic_import_reference(node: ast.Call, optional: bool) -> Reference | None:
    """Catch `importlib.import_module("foo")` and `__import__("foo")`.

    Only with a literal string argument. A computed module name is exactly the
    sort of thing we must stay quiet about.
    """
    target: str | None = None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "__import__":
        target = "__import__"
    elif isinstance(func, ast.Attribute) and func.attr == "import_module":
        root = _unwind_attribute(func)
        if root is not None and root[0] == "importlib":
            target = "importlib.import_module"
    if target is None or not node.args:
        return None

    first = node.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    name = first.value
    if not name or name.startswith("."):
        return None

    return Reference(
        kind=RefKind.MODULE,
        module=name,
        line=node.lineno,
        col=node.col_offset,
        source_text=f'{target}("{name}")',
        optional=optional,
    )


def _optional_import_nodes(tree: ast.AST) -> set[int]:
    """Ids of import nodes guarded by `try: ... except ImportError:`.

    A guarded import is a deliberate statement that the dependency may be
    absent. Flagging those as missing would make the tool unusable in every
    real codebase, all of which do this.
    """
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_handler_catches_import_error(h) for h in node.handlers):
            continue
        for stmt in node.body:
            for child in ast.walk(stmt):
                if isinstance(child, (ast.Import, ast.ImportFrom, ast.Call)):
                    guarded.add(id(child))
    return guarded


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:  # bare except
        return True
    candidates = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    for node in candidates:
        if isinstance(node, ast.Name) and node.id in _OPTIONAL_GUARDS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _OPTIONAL_GUARDS:
            return True
    return False


def _collect_bindings(tree: ast.AST) -> tuple[dict[str, str], set[str], bool]:
    """Map local names to the modules they were imported as, plus a shadow set.

    Returns (bindings, shadowed, saw_star_import).

    `bindings` only ever contains names bound to *modules* -- `import numpy as
    np` gives np -> numpy, and `import os.path` gives os -> os, because that
    statement binds the top package. `from x import y` binds a member, not a
    module, so it is deliberately excluded: we cannot follow attribute access
    through it without knowing what kind of object y is.
    """
    bindings: dict[str, str] = {}
    shadowed: set[str] = set()
    star = False

    for node in ast.walk(tree):
        # -- module bindings
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    # `import os.path` binds the name `os`.
                    top = alias.name.split(".")[0]
                    bindings[top] = top

        elif isinstance(node, ast.ImportFrom):
            if any(a.name == "*" for a in node.names):
                star = True

        # -- everything that could rebind a name
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                shadowed |= _assigned_names(target)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            shadowed |= _assigned_names(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            shadowed |= _assigned_names(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    shadowed |= _assigned_names(item.optional_vars)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                shadowed.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            shadowed.add(node.name)
            shadowed |= _parameter_names(node.args)
        elif isinstance(node, ast.Lambda):
            shadowed |= _parameter_names(node.args)
        elif isinstance(node, ast.ClassDef):
            shadowed.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            shadowed |= set(node.names)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                shadowed |= _assigned_names(target)
        elif isinstance(node, comprehension_types):
            for gen in node.generators:
                shadowed |= _assigned_names(gen.target)
        elif isinstance(node, ast.NamedExpr):
            shadowed |= _assigned_names(node.target)
        elif isinstance(node, ast.Match):
            # Pattern captures bind names too. Rather than model the whole
            # pattern grammar, take every capture name conservatively.
            for child in ast.walk(node):
                if isinstance(child, ast.MatchAs) and child.name:
                    shadowed.add(child.name)
                elif isinstance(child, ast.MatchStar) and child.name:
                    shadowed.add(child.name)
                elif isinstance(child, ast.MatchMapping) and child.rest:
                    shadowed.add(child.rest)

    return bindings, shadowed, star


comprehension_types = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _assigned_names(target: ast.expr) -> set[str]:
    """Every plain name bound by an assignment target, unpacking included."""
    names: set[str] = set()
    for node in ast.walk(target):
        if isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _parameter_names(args: ast.arguments) -> set[str]:
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names
