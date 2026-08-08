"""Python front-end.

Extracts three kinds of claim:

    import numpy                  -> MODULE     "numpy is importable"
    from pandas import DataFrame  -> MEMBER     "DataFrame is a member of pandas"
    np.linalg.norm(x)             -> ATTRIBUTE  "numpy.linalg.norm exists"
    user.email                    -> BOUND_ATTRIBUTE  "User has an `email`"

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
from dataclasses import dataclass, field
from pathlib import Path

from ..refs import AbstainReason, Reference, RefKind
from .result import AnalysisResult

#: Caught around an import, these mean "this dependency is optional".
_IMPORT_GUARDS = frozenset(
    {"ImportError", "ModuleNotFoundError", "Exception", "BaseException", "OSError"}
)

#: Caught around an attribute access, these mean "this may not be here".
_ATTRIBUTE_GUARDS = frozenset(
    {"AttributeError", "Exception", "BaseException", "OSError", "NameError"}
)

#: Names that make an `if` test a statement about the platform or interpreter
#: version, keyed by the root they hang off.
_PLATFORM_ATTRS = {
    "sys": {"platform", "version_info", "maxsize", "implementation", "winver"},
    "os": {"name", "uname", "sep"},
    "platform": {"system", "machine", "processor", "python_version", "release"},
}


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

        guards = _guarded_nodes(tree)
        bindings, shadowed, star = _collect_bindings(tree)

        if star:
            result.abstentions.add(AbstainReason.STAR_IMPORT)

        self._collect_imports(tree, source, guards, result)
        self._collect_attributes(tree, source, bindings, shadowed, guards, result)
        self._collect_bound_attributes(tree, bindings, shadowed, guards, result)
        return result

    # -- imports ---------------------------------------------------------

    def _collect_imports(
        self,
        tree: ast.AST,
        source: str,
        guards: Guards,
        result: AnalysisResult,
    ) -> None:
        for node in ast.walk(tree):
            optional = id(node) in guards.imports
            if optional:
                result.abstentions.add(guards.reason(node))

            if isinstance(node, ast.Import):
                for alias in node.names:
                    result.references.append(
                        Reference(
                            kind=RefKind.MODULE,
                            module=alias.name,
                            line=node.lineno,
                            col=node.col_offset,
                            source_text=f"import {alias.name}",
                            guarded=optional,
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
                                guarded=optional,
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
                            guarded=optional,
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
        bindings: Bindings,
        shadowed: set[str],
        guards: Guards,
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

            if root not in bindings.modules:
                continue
            if root in shadowed:
                result.abstentions.add(AbstainReason.SHADOWED)
                continue

            guarded = id(node) in guards.attributes
            if guarded:
                result.abstentions.add(guards.reason(node))

            module = bindings.modules[root]
            result.references.append(
                Reference(
                    kind=RefKind.ATTRIBUTE,
                    module=module,
                    path=tuple(attrs),
                    line=node.lineno,
                    col=node.col_offset,
                    source_text=".".join((root, *attrs)),
                    guarded=guarded,
                )
            )


    # -- attributes on things that are not modules -----------------------

    def _collect_bound_attributes(
        self,
        tree: ast.AST,
        bindings: Bindings,
        shadowed: set[str],
        guards: Guards,
        result: AnalysisResult,
    ) -> None:
        """Claims about objects: `user.emial`, `self.reposiory`, `helper.run()`.

        This is where the hallucinations in a real repository actually live.
        An agent rarely invents a stdlib function; it invents a method on the
        class you just showed it.

        Only three roots are trusted, because only three can be pinned to a
        name without inferring types: a variable assigned exactly once from a
        direct constructor call, the receiver of a method, and a name imported
        from somewhere the project index can follow.
        """
        roots: dict[str, tuple[str, str]] = {}
        # Instance bindings are not filtered by `shadowed`: the assignment that
        # created the binding is itself in that set. They carry their own,
        # stricter test -- assigned exactly once, and by no other binding form.
        for name, class_name in bindings.instances.items():
            roots[name] = (class_name, "instance")
        for name in bindings.members:
            if name not in shadowed:
                roots[name] = (name, "symbol")

        self._emit_bound(tree, roots, guards, result, scope=None)

        # `self` is only meaningful inside the method that declares it, so it
        # gets its own pass scoped to each class body rather than a file-wide
        # binding that would leak between classes.
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if _is_staticmethod(child):
                    # A static method has no receiver. Treating its first
                    # parameter as `self` claimed that every argument was an
                    # instance of the enclosing class -- which is how
                    # `config.getini(...)` inside a @staticmethod came back as
                    # "Cache has no attribute 'getini'".
                    continue
                params = child.args.posonlyargs + child.args.args
                if not params:
                    continue
                receiver = params[0].arg
                if receiver in bindings.modules or receiver in bindings.instances:
                    continue  # ambiguous; the file-wide pass owns this name
                self._emit_bound(
                    child,
                    {receiver: (node.name, "self")},
                    guards,
                    result,
                    scope=child,
                )

    def _emit_bound(
        self,
        tree: ast.AST,
        roots: dict[str, tuple[str, str]],
        guards: Guards,
        result: AnalysisResult,
        scope: ast.AST | None,
    ) -> None:
        if not roots:
            return

        inner: set[int] = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
        }

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or id(node) in inner:
                continue
            # `self.cache = {}` creates the attribute; it does not claim one.
            if not isinstance(node.ctx, ast.Load):
                continue

            unwound = _unwind_attribute(node)
            if unwound is None:
                continue
            root, attrs = unwound
            if root not in roots:
                continue

            owner, owner_kind = roots[root]
            guarded = id(node) in guards.attributes
            if guarded:
                result.abstentions.add(guards.reason(node))

            result.references.append(
                Reference(
                    kind=RefKind.BOUND_ATTRIBUTE,
                    module="",
                    path=tuple(attrs),
                    line=node.lineno,
                    col=node.col_offset,
                    source_text=".".join((root, *attrs)),
                    guarded=guarded,
                    owner=owner,
                    owner_kind=owner_kind,
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
        guarded=optional,
    )


@dataclass
class Guards:
    """Nodes the author has already said might not resolve."""

    imports: set[int] = field(default_factory=set)
    attributes: set[int] = field(default_factory=set)
    reasons: dict[int, AbstainReason] = field(default_factory=dict)

    def mark(self, node: ast.AST, reason: AbstainReason, *, kind: str) -> None:
        target = self.imports if kind == "import" else self.attributes
        target.add(id(node))
        self.reasons.setdefault(id(node), reason)

    def reason(self, node: ast.AST) -> AbstainReason:
        return self.reasons.get(id(node), AbstainReason.GUARDED)


def _guarded_nodes(tree: ast.AST) -> Guards:
    """Find references the source itself admits may not resolve.

    Two forms, and both are everywhere in real code:

        try:                         if sys.platform == "win32":
            import ujson                 import msvcrt
        except ImportError:              msvcrt.getch()
            ujson = None

    In each case the author has stated in code that the reference may not be
    there. Reporting it as a hallucination would be telling them something
    they already knew, about code that is correct. `ctypes.windll` exists on
    Windows and nowhere else; a checker that cannot express "this is fine"
    fails on the first cross-platform file it meets.
    """
    guards = Guards()

    # Annotations are the other type-only position. Under `from __future__
    # import annotations` they are never evaluated at all, and even without it
    # a type checker resolves them against stubs rather than the live object.
    for node in ast.walk(tree):
        for annotation in _annotation_slots(node):
            for child in ast.walk(annotation):
                if isinstance(child, ast.Attribute):
                    guards.mark(child, AbstainReason.TYPE_ONLY, kind="attribute")

    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            catches_import = any(
                _handler_catches(h, _IMPORT_GUARDS) for h in node.handlers
            )
            catches_attribute = any(
                _handler_catches(h, _ATTRIBUTE_GUARDS) for h in node.handlers
            )
            if not (catches_import or catches_attribute):
                continue
            for stmt in node.body:
                for child in ast.walk(stmt):
                    if catches_import and isinstance(
                        child, (ast.Import, ast.ImportFrom, ast.Call)
                    ):
                        guards.mark(child, AbstainReason.GUARDED, kind="import")
                    if catches_attribute and isinstance(child, ast.Attribute):
                        guards.mark(child, AbstainReason.GUARDED, kind="attribute")

        elif isinstance(node, ast.BoolOp) and _contains_hasattr(node):
            # `if hasattr(socket, "AF_UNIX") and sock.family == socket.AF_UNIX:`
            # -- the guarded use sits in the same boolean expression as the
            # guard, not in the body, so the expression itself has to be swept.
            for child in ast.walk(node):
                if isinstance(child, ast.Attribute):
                    guards.mark(child, AbstainReason.EXISTENCE_GUARDED, kind="attribute")

        elif (
            isinstance(node, (ast.If, ast.IfExp, ast.While))
            and _contains_hasattr(node.test)
        ):
            branches = [node.test, *_branch_bodies(node)]
            for branch in branches:
                for child in ast.walk(branch):
                    if isinstance(child, ast.Attribute):
                        guards.mark(
                            child, AbstainReason.EXISTENCE_GUARDED, kind="attribute"
                        )

        elif isinstance(node, ast.If) and _is_type_checking_test(node.test):
            # `if TYPE_CHECKING:` never runs. Names in it are resolved by a
            # type checker against stubs, and stubs routinely declare things
            # the runtime module does not expose -- `warnings._ActionKind`
            # being the case that found this. Runtime introspection is simply
            # the wrong oracle for that position.
            for stmt in node.body:
                for child in ast.walk(stmt):
                    if isinstance(child, (ast.Import, ast.ImportFrom, ast.Call)):
                        guards.mark(child, AbstainReason.TYPE_ONLY, kind="import")
                    elif isinstance(child, ast.Attribute):
                        guards.mark(child, AbstainReason.TYPE_ONLY, kind="attribute")

        elif isinstance(node, ast.If) and _is_platform_test(node.test):
            # Both branches, not just the taken one. Which branch is live
            # depends on the machine running the code, not the machine
            # running the checker.
            for stmt in (*node.body, *node.orelse):
                for child in ast.walk(stmt):
                    if isinstance(child, (ast.Import, ast.ImportFrom, ast.Call)):
                        guards.mark(
                            child, AbstainReason.PLATFORM_GUARDED, kind="import"
                        )
                    elif isinstance(child, ast.Attribute):
                        guards.mark(
                            child, AbstainReason.PLATFORM_GUARDED, kind="attribute"
                        )

    return guards


def _annotation_slots(node: ast.AST) -> list[ast.expr]:
    """Every expression on `node` that is a type annotation."""
    slots: list[ast.expr] = []
    if isinstance(node, ast.AnnAssign) and node.annotation is not None:
        slots.append(node.annotation)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.returns is not None:
            slots.append(node.returns)
        args = node.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            if arg.annotation is not None:
                slots.append(arg.annotation)
        for extra in (args.vararg, args.kwarg):
            if extra is not None and extra.annotation is not None:
                slots.append(extra.annotation)
    return slots


def _contains_hasattr(node: ast.AST) -> bool:
    """Does this expression test for an attribute's existence?

    `hasattr(x, "y")` and `getattr(x, "y", default)` are the two idiomatic ways
    of saying "this may not be here" without a try/except, and both appear
    constantly in cross-platform code.
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = child.func.id if isinstance(child.func, ast.Name) else ""
        if name == "hasattr":
            return True
        if name == "getattr" and len(child.args) >= 3:
            return True
    return False


def _branch_bodies(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.IfExp):
        return [node.body, node.orelse]
    return [*getattr(node, "body", []), *getattr(node, "orelse", [])]


def _is_type_checking_test(test: ast.expr) -> bool:
    """Is this `if TYPE_CHECKING:` in any of its spellings?"""
    for node in ast.walk(test):
        if isinstance(node, ast.Name) and node.id == "TYPE_CHECKING":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING":
            return True
    return False


def _is_platform_test(test: ast.expr) -> bool:
    """Is this `if` asking about the platform or the interpreter version?"""
    for node in ast.walk(test):
        if not isinstance(node, ast.Attribute):
            continue
        unwound = _unwind_attribute(node)
        if unwound is None:
            continue
        root, attrs = unwound
        allowed = _PLATFORM_ATTRS.get(root)
        if allowed and attrs and attrs[0] in allowed:
            return True
    return False


def _handler_catches(handler: ast.ExceptHandler, names: frozenset[str]) -> bool:
    if handler.type is None:  # bare except catches everything
        return True
    candidates = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    for node in candidates:
        if isinstance(node, ast.Name) and node.id in names:
            return True
        if isinstance(node, ast.Attribute) and node.attr in names:
            return True
    return False


@dataclass
class Bindings:
    """What each name in the file is bound to, where we can tell."""

    #: `import numpy as np` -> np: numpy. Attribute access resolves against
    #: the module.
    modules: dict[str, str] = field(default_factory=dict)
    #: `user = User()` -> user: "User". Only recorded when the name is assigned
    #: exactly once in the whole file and the right-hand side is a bare
    #: constructor call. Anything less and we do not know what it holds.
    instances: dict[str, str] = field(default_factory=dict)
    #: Names bound by `from x import y`. The engine resolves what y actually is.
    members: set[str] = field(default_factory=set)


def _collect_bindings(tree: ast.AST) -> tuple[Bindings, set[str], bool]:
    """Work out what each name refers to, plus a shadow set.

    Returns (bindings, shadowed, saw_star_import).

    Module bindings come from `import` statements. `from x import y` binds a
    member whose kind we cannot know here, so it is recorded as a member and
    left for the engine to resolve against the project index.

    Instance bindings are the new and delicate part: `user = User()` tells us
    what `user` is, but only if nothing else in the file ever assigns to that
    name. A single reassignment anywhere and the binding is discarded, because
    a wrong type is worse than no type.
    """
    bindings = Bindings()
    shadowed: set[str] = set()
    star = False
    assignments: dict[str, list[ast.expr]] = {}
    hard_bound: set[str] = set()
    ambiguous: set[str] = set()

    for node in ast.walk(tree):
        # -- module bindings
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    _bind_module(bindings, ambiguous, alias.asname, alias.name)
                else:
                    # `import os.path` binds the name `os`.
                    top = alias.name.split(".")[0]
                    _bind_module(bindings, ambiguous, top, top)

        elif isinstance(node, ast.ImportFrom):
            if any(a.name == "*" for a in node.names):
                star = True
            else:
                for alias in node.names:
                    bindings.members.add(alias.asname or alias.name)

        # -- everything that could rebind a name
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                shadowed |= _assigned_names(target)
                # A plain `name = <expr>` is the one binding form we can read a
                # type out of, so its right-hand side is kept rather than just
                # counted.
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)
                else:
                    hard_bound |= _assigned_names(target)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            names = _assigned_names(node.target)
            shadowed |= names
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.value is not None
            ):
                assignments.setdefault(node.target.id, []).append(node.value)
            else:
                hard_bound |= names
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            shadowed |= _assigned_names(node.target)
            hard_bound |= _assigned_names(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    shadowed |= _assigned_names(item.optional_vars)
                    hard_bound |= _assigned_names(item.optional_vars)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                shadowed.add(node.name)
                hard_bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            shadowed.add(node.name)
            shadowed |= _parameter_names(node.args)
            hard_bound.add(node.name)
            hard_bound |= _parameter_names(node.args)
        elif isinstance(node, ast.Lambda):
            shadowed |= _parameter_names(node.args)
            hard_bound |= _parameter_names(node.args)
        elif isinstance(node, ast.ClassDef):
            shadowed.add(node.name)
            hard_bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            shadowed |= set(node.names)
            hard_bound |= set(node.names)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                shadowed |= _assigned_names(target)
                hard_bound |= _assigned_names(target)
        elif isinstance(node, comprehension_types):
            for gen in node.generators:
                shadowed |= _assigned_names(gen.target)
                hard_bound |= _assigned_names(gen.target)
        elif isinstance(node, ast.NamedExpr):
            shadowed |= _assigned_names(node.target)
            hard_bound |= _assigned_names(node.target)
        elif isinstance(node, ast.Match):
            # Pattern captures bind names too. Rather than model the whole
            # pattern grammar, take every capture name conservatively.
            for child in ast.walk(node):
                if isinstance(child, ast.MatchAs) and child.name:
                    shadowed.add(child.name)
                    hard_bound.add(child.name)
                elif isinstance(child, ast.MatchStar) and child.name:
                    shadowed.add(child.name)
                    hard_bound.add(child.name)
                elif isinstance(child, ast.MatchMapping) and child.rest:
                    shadowed.add(child.rest)
                    hard_bound.add(child.rest)

    for name in ambiguous:
        bindings.modules.pop(name, None)

    escaped = _escaping_names(tree)
    for name, values in assignments.items():
        if len(values) != 1 or name in hard_bound:
            continue  # assigned more than once, or bound some other way too
        if name in escaped:
            # The object was handed to something else, which may have attached
            # attributes to it. See _escaping_names.
            continue
        constructed = _constructor_name(values[0])
        if constructed:
            bindings.instances[name] = constructed

    return bindings, shadowed, star


def _is_staticmethod(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else target.id if isinstance(target, ast.Name) else ""
        )
        if name == "staticmethod":
            return True
    return False


def _escaping_names(tree: ast.AST) -> set[str]:
    """Names whose object is handed to something we cannot see.

    Python objects are open: any code holding a reference can attach an
    attribute to it. `setattr(user, "x", 1)`, or

        def decorate(u):
            u.decorated = True

        decorate(user)

    both give `user` an attribute that no reading of its class will ever show.
    So the moment a bound name is used as a bare value -- passed to a call,
    returned, stored in a container -- we stop claiming to know its surface.

    Reading through it does not count: in `print(user.email)` the argument is
    `user.email`, not `user`, and the object itself never leaves.
    """
    inner: set[int] = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }

    escaped: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if id(node) not in inner:
                escaped.add(node.id)
    return escaped


def _bind_module(
    bindings: Bindings, ambiguous: set[str], name: str, module: str
) -> None:
    """Bind a name to a module, or mark it ambiguous if it already means another.

    The idiom that found this:

        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib

    `tomllib` means a different module on each branch. Taking whichever import
    the walk happened to reach last produced confident, wrong answers about
    every attribute on it.
    """
    existing = bindings.modules.get(name)
    if existing is not None and existing != module:
        ambiguous.add(name)
        return
    bindings.modules[name] = module


def _constructor_name(value: ast.expr) -> str | None:
    """`Foo()` and `pkg.Foo()` yield a name; anything else yields nothing.

    Deliberately refuses to be clever. `Foo().bar()`, `make_foo()`, a ternary,
    an await -- all of them might produce a Foo and we would usually be right
    to guess, but "usually right" is the failure mode this project is built to
    avoid.
    """
    if not isinstance(value, ast.Call):
        return None
    name = _dotted_name(value.func)
    if not name:
        return None
    head = name.split(".")[-1]
    # A constructor call is conventionally capitalised. Requiring it costs a
    # few real bindings and rejects a great many factory functions, whose
    # return type we genuinely cannot know.
    return name if head[:1].isupper() else None


def _dotted_name(node: ast.expr) -> str:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ""
    parts.append(current.id)
    return ".".join(reversed(parts))


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
