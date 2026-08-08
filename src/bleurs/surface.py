"""API surface projection.

The second half of the thesis. Verification answers "does this exist"; this
answers "what exists" -- from the same index, at a fraction of the tokens a
file read would cost.

The argument is narrow and worth stating precisely, because the obvious
objection is that this is just lossy compression under a new name:

    You do not need a function's body to call the function correctly.
    You need its name, its signature, and one line about what it does.

That projection is lossy about implementation and *lossless about the interface*
-- which is the only thing a caller is reasoning over. And unlike a summary, it
is derived rather than remembered: it can be recomputed at any moment from the
code as it is right now, so it cannot drift, go stale, or be forgotten
expensively. Context becomes a cache instead of a ledger.

Two backends, one shape:

    local      parse the project's own file  (no execution, exact source text)
    installed  introspect the real object    (subprocess, exact runtime truth)
"""

from __future__ import annotations

import ast
from pathlib import Path

from .truth.introspect import Introspector, Member, Surface

#: Rough characters-per-token for English-plus-code. Used only for reporting a
#: size estimate; every number derived from it is labelled an estimate because
#: shipping a real tokenizer would mean shipping a dependency, and the ratio is
#: the wrong thing to be precise about anyway.
CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


# -- local files ---------------------------------------------------------


def local_surface(
    path: Path,
    source: str | None = None,
    *,
    private: bool = False,
    module_name: str | None = None,
) -> Surface:
    """Project a project file's public API without importing it.

    Signatures come from the source text via `ast.unparse`, so what you read is
    what the author wrote -- defaults, type annotations, keyword-only markers
    and all -- rather than a runtime approximation of it.

    `private=True` includes underscore names. An agent calling a library wants
    the public surface; an agent *editing* that library needs the internals it
    is about to touch, and hiding them would just send it to read the file.
    """
    try:
        text = source if source is not None else path.read_text(
            encoding="utf-8", errors="replace"
        )
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError) as exc:
        return Surface(module=module_name or path.stem, error=str(exc))

    members = tuple(
        m
        for m in (
            _from_node(node, deep=True, private=private) for node in tree.body
        )
        if m
    )
    return Surface(
        module=module_name or path.stem,
        ok=True,
        kind="module",
        summary=_docline(ast.get_docstring(tree)),
        members=members,
    )


def _hidden(name: str, private: bool) -> bool:
    if private:
        return False
    # Dunders stay: __init__ and __enter__ are part of how you call the thing.
    return name.startswith("_") and not name.startswith("__")


def _from_node(node: ast.stmt, *, deep: bool, private: bool = False) -> Member | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if _hidden(node.name, private):
            return None
        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        return Member(
            name=node.name,
            kind=f"{prefix}function".strip(),
            signature=_signature(node),
            summary=_docline(ast.get_docstring(node)),
        )

    if isinstance(node, ast.ClassDef):
        if _hidden(node.name, private):
            return None
        methods: tuple[Member, ...] = ()
        if deep:
            methods = tuple(
                m
                for m in (
                    _from_node(child, deep=False, private=private)
                    for child in node.body
                )
                if m
            )
        bases = ", ".join(_unparse(b) for b in node.bases)
        return Member(
            name=node.name,
            kind="class",
            signature=f"({bases})" if bases else None,
            summary=_docline(ast.get_docstring(node)),
            members=methods,
        )

    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if _hidden(node.target.id, private):
            return None
        return Member(
            name=node.target.id,
            kind="value",
            signature=f": {_unparse(node.annotation)}",
        )

    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and not _hidden(target.id, private):
                return Member(name=target.id, kind="value")
    return None


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = _unparse(node.args)
    returns = f" -> {_unparse(node.returns)}" if node.returns else ""
    return f"({args}){returns}"


def _unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):  # pragma: no cover
        return "..."


def _docline(doc: str | None) -> str | None:
    if not doc:
        return None
    line = doc.strip().split("\n")[0].strip()
    return line[:140] or None


# -- installed packages --------------------------------------------------


def installed_surface(
    dotted: str, introspector: Introspector | None = None
) -> Surface:
    """Project an installed module or class by introspecting the real object."""
    introspector = introspector or Introspector()
    parts = dotted.split(".")

    # `datetime.datetime` is a class inside a module; `os.path` is a module
    # inside a package. Which prefix is the importable part is not knowable
    # from the string, so try the longest module first and walk back.
    for split in range(len(parts), 0, -1):
        module = ".".join(parts[:split])
        path = tuple(parts[split:])
        result = introspector.surface(module, path)
        if result.ok:
            return result
        last = result

    return last


# -- rendering -----------------------------------------------------------


def render(
    surface: Surface,
    *,
    summaries: bool = True,
    indent: str = "  ",
    limit: int | None = None,
) -> str:
    """Compact, greppable projection. Designed to be read by a model.

    No box drawing, no colour, no blank-line padding. Every character in here
    is one a model pays for, so the format spends them on names, signatures and
    the one line of prose that disambiguates two similar functions.

    `limit` caps the member count. Used on the rejection path, where a module
    with five hundred names would otherwise turn a helpful correction into a
    flood -- and where the agent needs the shape of the API, not all of it.
    """
    if not surface.ok:
        return f"{surface.dotted}: unavailable ({surface.error})"

    lines = [f"{surface.dotted}  [{surface.kind}]"]
    if summaries and surface.summary:
        lines.append(f"{indent}# {surface.summary}")

    shown = surface.members if limit is None else surface.members[:limit]
    for member in shown:
        lines.append(_render_member(member, indent, summaries))
        for child in member.members:
            lines.append(_render_member(child, indent * 2, summaries))

    omitted = len(surface.members) - len(shown)
    if omitted > 0:
        lines.append(f"{indent}... {omitted} more (bleurs surface {surface.dotted})")

    if len(lines) == 1:
        lines.append(f"{indent}(no public members)")
    return "\n".join(lines)


def _render_member(member: Member, indent: str, summaries: bool) -> str:
    signature = member.signature or ""
    marker = "class " if member.kind == "class" else ""
    line = f"{indent}{marker}{member.name}{signature}"
    if summaries and member.summary:
        line += f"  # {member.summary}"
    return line
