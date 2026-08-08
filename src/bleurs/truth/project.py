"""Tier 0, properly.

The first version of this file answered one question: does module M define name
N. That is enough to catch a bad import and nothing else, which meant that in a
real repository -- where most of what an agent invents is a method on *your*
objects, not on the standard library -- the tool mostly abstained.

This one builds a symbol table per module and a shape per class, follows
re-exports across files, and can therefore say whether `repo.find_by_email`
exists on the thing `repo` was actually assigned.

The load-bearing concept is `closed`. A class shape is closed when we can
enumerate its *complete* attribute surface: every base resolved, no unknown
decorator that might have replaced the class, no `__getattr__`, no `setattr` on
self. Only a closed shape may produce a BLOCK. An open one abstains, because a
name missing from a partial surface is not missing from the class -- and that
distinction is the difference between this tool and a nuisance.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from .local import LocalIndex

#: Decorators that leave a class's identity and attribute surface intact.
#: Anything else might return a different object entirely, and we cannot know.
SAFE_CLASS_DECORATORS = frozenset(
    {
        "dataclass",
        "dataclasses.dataclass",
        "final",
        "typing.final",
        "runtime_checkable",
        "typing.runtime_checkable",
        "total_ordering",
        "functools.total_ordering",
        "attrs.define",
        "attr.define",
        "attr.s",
        "attrs.frozen",
        "pydantic.dataclasses.dataclass",
    }
)

#: Bases that add nothing and therefore keep a shape closed.
INERT_BASES = frozenset({"object", "Protocol", "typing.Protocol", "ABC", "abc.ABC"})

#: Calls in a class body that put attributes beyond our reach.
ESCAPE_CALLS = frozenset({"setattr", "vars", "globals", "locals", "exec", "eval"})

_MAX_BASE_DEPTH = 12


@dataclass(frozen=True)
class ClassShape:
    """What a class offers, and whether that list is complete."""

    name: str
    module: str
    own: frozenset[str] = frozenset()
    #: Base expressions exactly as written, e.g. ("Base", "mixins.Loggable").
    bases: tuple[str, ...] = ()
    #: False when something in the definition could add names we cannot see.
    self_closed: bool = True
    #: Why it is not closed, for --explain.
    reason: str = ""


@dataclass(frozen=True)
class Symbol:
    """A top-level name in a project module."""

    name: str
    module: str
    kind: str  # class | function | value | module | unknown
    shape: ClassShape | None = None


@dataclass
class ModuleInfo:
    dotted: str
    path: Path
    symbols: dict[str, Symbol] = field(default_factory=dict)
    #: local name -> (module, original name). `("mod", "*")` means the whole
    #: module was bound, as in `import mod` or `from pkg import mod`.
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    star_import: bool = False
    dynamic: bool = False


class ProjectIndex:
    """Symbol and class resolution across a project's own files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules = LocalIndex(root)
        self._cache: dict[str, ModuleInfo | None] = {}
        #: The file currently being checked, parsed from its *proposed*
        #: content. It shadows whatever is on disk, because the edit under
        #: judgement may define the very class it then uses -- and may not
        #: exist on disk at all.
        self.overlay: ModuleInfo | None = None

    # -- module access ---------------------------------------------------

    def module(self, dotted: str) -> ModuleInfo | None:
        if self.overlay is not None and self.overlay.dotted == dotted:
            return self.overlay
        if dotted in self._cache:
            return self._cache[dotted]

        path = self.modules.path_for(dotted)
        info = None
        if path is not None:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                info = parse_module(dotted, path, source)
            except (OSError, SyntaxError, ValueError):
                info = None

        self._cache[dotted] = info
        return info

    def has_module(self, dotted: str) -> bool:
        if self.overlay is not None and self.overlay.dotted == dotted:
            return True
        return self.modules.has_module(dotted)

    def is_local_root(self, top_level: str) -> bool:
        return self.modules.is_local_root(top_level)

    # -- symbol resolution -----------------------------------------------

    def resolve(self, module: str, name: str, _seen: frozenset = frozenset()) -> Symbol | None:
        """Find `name` as exported by `module`, following re-exports.

        Returns None both for "no such name" and for "we cannot tell", so
        callers must consult `exports_known` before drawing a conclusion from
        a None. Conflating the two here is exactly the mistake this project
        exists to avoid making.
        """
        if module in _seen or len(_seen) > 16:
            return None  # import cycle; refuse rather than loop
        info = self.module(module)
        if info is None:
            return None

        direct = info.symbols.get(name)
        if direct is not None:
            return direct

        target = info.imports.get(name)
        if target is None:
            # `from pkg import submodule` -- the name is a module on disk that
            # the package's __init__ never mentions. Extremely common, and
            # invisible to a pure symbol-table lookup.
            nested = f"{module}.{name}"
            if self.has_module(nested):
                return Symbol(name=name, module=nested, kind="module")
            return None

        origin, original = target
        if original == "*":
            # `import pkg.mod as m` -- the name is bound to a whole module.
            if self.has_module(origin):
                return Symbol(name=name, module=origin, kind="module")
            return None

        if self.has_module(origin):
            return self.resolve(origin, original, _seen | {module})

        # Re-exported from outside the project. Real, but not ours to describe.
        return None

    def exports_known(self, module: str) -> bool:
        """Can we enumerate everything this module exports?"""
        info = self.module(module)
        return info is not None and not info.star_import and not info.dynamic

    # -- class surfaces --------------------------------------------------

    def class_surface(self, shape: ClassShape) -> frozenset[str] | None:
        """Every attribute an instance of this class can have, or None.

        None means the surface is open -- a base we could not resolve, a
        decorator that might have swapped the class out, a `__getattr__`. The
        caller must treat that as "unknown", never as "empty".
        """
        return self._surface(shape, depth=0, seen=set())

    def _surface(
        self, shape: ClassShape, depth: int, seen: set[tuple[str, str]]
    ) -> frozenset[str] | None:
        if not shape.self_closed or depth > _MAX_BASE_DEPTH:
            return None

        key = (shape.module, shape.name)
        if key in seen:
            return frozenset()  # diamond or cycle; contributes nothing new
        seen.add(key)

        names = set(shape.own)
        for base in shape.bases:
            if base in INERT_BASES:
                continue
            resolved = self._resolve_base(shape.module, base)
            if resolved is None or resolved.shape is None:
                return None  # a base we cannot describe opens the whole surface
            inherited = self._surface(resolved.shape, depth + 1, seen)
            if inherited is None:
                return None
            names |= inherited

        return frozenset(names)

    def _resolve_base(self, module: str, base: str) -> Symbol | None:
        """Resolve a base class expression written inside `module`."""
        head, _, rest = base.partition(".")
        if not rest:
            return self.resolve(module, head)

        # `mixins.Loggable` -- head names a module, rest names the class in it.
        info = self.module(module)
        if info is None:
            return None
        target = info.imports.get(head)
        if target is None:
            return None
        origin, original = target
        container = origin if original == "*" else f"{origin}.{original}"
        return self.resolve(container, rest.split(".")[0])


# -- parsing -------------------------------------------------------------


def parse_module(dotted: str, path: Path, source: str) -> ModuleInfo:
    """Build a symbol table for one file. No imports are executed."""
    info = ModuleInfo(dotted=dotted, path=path)
    tree = ast.parse(source)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "__getattr__":
                info.dynamic = True
            info.symbols[node.name] = Symbol(node.name, dotted, "function")

        elif isinstance(node, ast.ClassDef):
            shape = class_shape(node, dotted)
            info.symbols[node.name] = Symbol(node.name, dotted, "class", shape)

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    info.symbols[target.id] = Symbol(target.id, dotted, "value")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            info.symbols[node.target.id] = Symbol(node.target.id, dotted, "value")

        elif isinstance(node, ast.Import):
            for alias in node.names:
                info.imports[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0],
                    "*",
                )

        elif isinstance(node, ast.ImportFrom):
            origin = _absolute_origin(dotted, path, node)
            if origin is None:
                info.star_import = True  # unanchorable; stop claiming completeness
                continue
            for alias in node.names:
                if alias.name == "*":
                    info.star_import = True
                    continue
                info.imports[alias.asname or alias.name] = (origin, alias.name)

    return info


def _absolute_origin(dotted: str, path: Path, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module
    parts = dotted.split(".")
    if path.name != "__init__.py":
        parts = parts[:-1]
    if node.level > 1:
        parts = parts[: -(node.level - 1)]
    if not parts:
        return None
    if node.module:
        parts.append(node.module)
    return ".".join(parts)


def class_shape(node: ast.ClassDef, module: str) -> ClassShape:
    """Everything a class defines, plus an honest verdict on completeness."""
    reason = ""

    for decorator in node.decorator_list:
        name = _dotted(decorator)
        if name not in SAFE_CLASS_DECORATORS:
            reason = f"decorated with @{name or '...'}"
            break

    names: set[str] = set()
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(child.name)
            if child.name in {"__getattr__", "__getattribute__"}:
                reason = reason or "defines __getattr__"
            names |= _self_assignments(child)
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            names.add(child.target.id)

    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _dotted(child.func) in ESCAPE_CALLS:
            reason = reason or "attributes set dynamically"
            break

    bases = tuple(_dotted(b) or "?" for b in node.bases)
    if any(b == "?" for b in bases):
        reason = reason or "computed base class"
    if node.keywords:  # metaclass=..., or anything else that rewrites the class
        reason = reason or "class keyword arguments"

    return ClassShape(
        name=node.name,
        module=module,
        own=frozenset(names),
        bases=bases,
        self_closed=not reason,
        reason=reason,
    )


def _self_assignments(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Attributes assigned onto the first parameter, conventionally `self`."""
    params = func.args.posonlyargs + func.args.args
    if not params:
        return set()
    receiver = params[0].arg

    names: set[str] = set()
    for node in ast.walk(func):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            for inner in ast.walk(target):
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == receiver
                ):
                    names.add(inner.attr)
    return names


def _dotted(node: ast.expr | None) -> str:
    """Flatten a dotted name expression, or "" if it is not one."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Call):  # e.g. @decorator(arg) -- take the callee
        return _dotted(current.func)
    if not isinstance(current, ast.Name):
        return ""
    parts.append(current.id)
    return ".".join(reversed(parts))
