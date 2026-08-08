"""TypeScript and JavaScript front-end, via tree-sitter.

Python got the interpreter's own `ast` because it ships one. TypeScript does
not, so this uses tree-sitter -- the same parser GitHub's code search runs on,
and the one the roadmap has always named. It is an optional extra: the core
install stays dependency-free and `bleurs[typescript]` adds the grammars.

One structural advantage over the Python front-end is worth noting, because it
buys real recall. ESM import bindings are immutable by specification -- you
cannot reassign an imported name -- so a namespace import is a binding we can
follow without the paranoia the Python analyzer needs around rebinding. The
only thing that can take it away is a redeclaration in the same file, which is
cheap to detect and rare in practice.

What this deliberately does not attempt: type-driven member resolution on
third-party packages. Answering `user.emial` for an npm dependency means
resolving `.d.ts` files, `exports` maps and declaration merging, which is
tsc's job and not something to reimplement badly. Project-local exports and
package existence are both fully decidable without it, and that is what this
front-end claims.
"""

from __future__ import annotations

import functools
from pathlib import Path

from ..refs import AbstainReason, Reference, RefKind
from .result import AnalysisResult

#: Files parsed with the JSX-aware grammar. `.js` is included because JSX in
#: plain `.js` is near-universal in React projects.
_JSX_SUFFIXES = frozenset({".tsx", ".jsx", ".js", ".mjs", ".cjs"})

#: Node types that introduce a name capable of shadowing an import.
_DECLARATORS = frozenset(
    {
        "variable_declarator",
        "function_declaration",
        "function_expression",
        "class_declaration",
        "required_parameter",
        "optional_parameter",
        "formal_parameters",
    }
)


@functools.lru_cache(maxsize=1)
def _grammars():
    """Load tree-sitter lazily so the core install needs no dependencies."""
    try:
        import tree_sitter_typescript as grammar
        from tree_sitter import Language, Parser
    except ImportError:
        return None

    return {
        "ts": Parser(Language(grammar.language_typescript())),
        "tsx": Parser(Language(grammar.language_tsx())),
    }


def available() -> bool:
    return _grammars() is not None


class TypeScriptAnalyzer:
    name = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

    def analyze(self, source: str, path: Path) -> AnalysisResult:
        result = AnalysisResult()
        parsers = _grammars()
        if parsers is None:
            result.parse_error = (
                "TypeScript support needs tree-sitter: pip install 'bleurs[typescript]'"
            )
            return result

        parser = parsers["tsx" if path.suffix in _JSX_SUFFIXES else "ts"]
        data = source.encode("utf-8")
        tree = parser.parse(data)
        root = tree.root_node

        if root.has_error:
            # Same rule as Python: a file that does not parse is broken, loudly,
            # already. It is not our place to add a second opinion.
            result.parse_error = "file does not parse cleanly"
            return result

        declared = _declared_names(root, data)
        namespaces: dict[str, str] = {}

        for node in _walk(root):
            if node.type == "import_statement":
                self._import(node, data, result, namespaces)
            elif node.type == "export_statement":
                self._reexport(node, data, result)
            elif node.type == "call_expression":
                self._dynamic(node, data, result)

        self._namespace_members(root, data, namespaces, declared, result)
        return result

    # -- statements ------------------------------------------------------

    def _import(self, node, data: bytes, result: AnalysisResult, namespaces) -> None:
        specifier = _string_value(node.child_by_field_name("source"), data)
        if specifier is None:
            return

        statement = _text(node, data)
        type_only = statement.startswith("import type")

        result.references.append(
            Reference(
                kind=RefKind.MODULE,
                module=specifier,
                line=node.start_point[0] + 1,
                col=node.start_point[1],
                source_text=f'import "{specifier}"',
                ecosystem="node",
            )
        )

        clause = next(
            (c for c in node.children if c.type == "import_clause"), None
        )
        if clause is None:
            return

        for child in _walk(clause):
            if child.type == "namespace_import":
                name = next(
                    (c for c in child.children if c.type == "identifier"), None
                )
                if name is not None:
                    namespaces[_text(name, data)] = specifier

            elif child.type == "import_specifier":
                if type_only or _text(child, data).startswith("type "):
                    # A type-only import resolves against declarations, which
                    # this front-end does not read. The module still has to
                    # exist; the member is not ours to judge.
                    result.abstentions.add(AbstainReason.TYPE_ONLY)
                    continue
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _text(name_node, data)
                result.references.append(
                    Reference(
                        kind=RefKind.MEMBER,
                        module=specifier,
                        path=(name,),
                        line=child.start_point[0] + 1,
                        col=child.start_point[1],
                        source_text=f'import {{ {name} }} from "{specifier}"',
                        ecosystem="node",
                    )
                )

    def _reexport(self, node, data: bytes, result: AnalysisResult) -> None:
        specifier = _string_value(node.child_by_field_name("source"), data)
        if specifier is None:
            return
        text = _text(node, data)
        if text.startswith("export *"):
            result.abstentions.add(AbstainReason.STAR_IMPORT)
        result.references.append(
            Reference(
                kind=RefKind.MODULE,
                module=specifier,
                line=node.start_point[0] + 1,
                col=node.start_point[1],
                source_text=f'export from "{specifier}"',
                ecosystem="node",
            )
        )

    def _dynamic(self, node, data: bytes, result: AnalysisResult) -> None:
        """`require("x")` and `import("x")`, literal arguments only."""
        function = node.child_by_field_name("function")
        if function is None:
            return
        kind = function.type
        name = _text(function, data)
        if kind != "import" and name != "require":
            return

        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            return
        literal = next((c for c in arguments.children if c.type == "string"), None)
        specifier = _string_value(literal, data)
        if specifier is None:
            return

        result.references.append(
            Reference(
                kind=RefKind.MODULE,
                module=specifier,
                line=node.start_point[0] + 1,
                col=node.start_point[1],
                source_text=f'{"import" if kind == "import" else "require"}("{specifier}")',
                ecosystem="node",
            )
        )

    # -- namespace member access -----------------------------------------

    def _namespace_members(
        self, root, data: bytes, namespaces, declared: set[str], result
    ) -> None:
        """`import * as utils from "./utils"; utils.helper()`.

        ESM bindings cannot be reassigned, so unlike Python this needs no
        single-assignment dance -- only a check that nothing else in the file
        declared the same name.
        """
        live = {n: s for n, s in namespaces.items() if n not in declared}
        if not live:
            return

        for node in _walk(root):
            if node.type != "member_expression":
                continue
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if obj is None or prop is None or obj.type != "identifier":
                continue
            name = _text(obj, data)
            if name not in live:
                continue
            result.references.append(
                Reference(
                    kind=RefKind.ATTRIBUTE,
                    module=live[name],
                    path=(_text(prop, data),),
                    line=node.start_point[0] + 1,
                    col=node.start_point[1],
                    source_text=_text(node, data)[:80],
                    ecosystem="node",
                )
            )


def exports_of(path: Path) -> frozenset[str] | None:
    """Every name a project module exports, or None if that set is open.

    None is returned for `export * from "..."`, which republishes a surface we
    have not read, and for any file that fails to parse. The caller must treat
    it as unknown -- never as empty.
    """
    parsers = _grammars()
    if parsers is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None

    parser = parsers["tsx" if path.suffix in _JSX_SUFFIXES else "ts"]
    root = parser.parse(data).root_node
    if root.has_error:
        return None

    names: set[str] = set()
    for node in _walk(root):
        if node.type != "export_statement":
            continue
        text = _text(node, data)
        if text.startswith("export *"):
            return None
        if text.startswith("export default"):
            names.add("default")

        for child in _walk(node):
            if child.type == "export_specifier":
                alias = child.child_by_field_name("alias")
                name = child.child_by_field_name("name")
                target = alias if alias is not None else name
                if target is not None:
                    names.add(_text(target, data))
            elif child.type in {
                "function_declaration",
                "generator_function_declaration",
                "class_declaration",
                "abstract_class_declaration",
                "interface_declaration",
                "type_alias_declaration",
                "enum_declaration",
                "module",
            }:
                name = child.child_by_field_name("name")
                if name is not None:
                    names.add(_text(name, data))
            elif child.type == "variable_declarator":
                name = child.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    names.add(_text(name, data))

    return frozenset(names)


# -- helpers -------------------------------------------------------------


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _string_value(node, data: bytes) -> str | None:
    """The contents of a string literal, without its quotes."""
    if node is None or node.type != "string":
        return None
    fragment = next((c for c in node.children if c.type == "string_fragment"), None)
    if fragment is not None:
        return _text(fragment, data)
    raw = _text(node, data)
    return raw[1:-1] if len(raw) >= 2 else None


def _declared_names(root, data: bytes) -> set[str]:
    """Every name the file binds itself, which could shadow an import."""
    names: set[str] = set()
    for node in _walk(root):
        if node.type not in _DECLARATORS:
            continue
        for child in _walk(node):
            if child.type == "identifier":
                names.add(_text(child, data))
    return names
