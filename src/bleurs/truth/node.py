"""Ground truth for the Node ecosystem.

The Python tiers lean on `importlib.metadata` and live introspection. Node has
neither, so the equivalents are the filesystem and the registry:

    builtin      a hardcoded list, because `node:fs` is not on disk anywhere
    relative     resolve ./x against the importing file, with the extension and
                 index fallbacks Node and TypeScript actually apply
    installed    walk up for node_modules, exactly as Node does at runtime
    declared     named in package.json but not installed yet
    unknown      a tsconfig path alias, or something we simply cannot follow

The last state is the important one. Path aliases (`@/components/Button`) look
exactly like bare package specifiers and are wildly common in TypeScript
projects. Blocking one would be a false positive on completely ordinary code,
so anything that could be an alias resolves to `unknown` and abstains.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: Node's own modules. Importable as `fs` or `node:fs`; the prefixed form is
#: always valid even for names added after this list was written, so a
#: `node:`-prefixed specifier is never blocked.
BUILTINS: frozenset[str] = frozenset(
    """
    assert async_hooks buffer child_process cluster console constants crypto
    dgram diagnostics_channel dns domain events fs http http2 https inspector
    module net os path perf_hooks process punycode querystring readline repl
    sqlite stream string_decoder sys test timers tls trace_events tty url util
    v8 vm wasi worker_threads zlib
    """.split()
)

#: Extensions tried in order, mirroring TypeScript's own resolution.
EXTENSIONS = (
    ".ts",
    ".tsx",
    ".d.ts",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
    ".vue",
    ".svelte",
)

DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)

#: A specifier is bare (a package) if it does not start with . or / or a scheme.
_RELATIVE = re.compile(r"^\.{1,2}/|^\.{1,2}$|^/")


@dataclass(frozen=True)
class Resolution:
    """What a module specifier turned out to be."""

    kind: str  # builtin | file | missing_file | installed | declared | absent | unknown
    #: The resolved file, when the specifier named one in this project.
    path: Path | None = None
    #: The package root for a bare specifier, e.g. "@scope/name".
    package: str = ""
    #: Why we could not decide, when kind == "unknown".
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.kind in {"builtin", "file", "installed"}


def is_relative(specifier: str) -> bool:
    return bool(_RELATIVE.match(specifier))


def package_root(specifier: str) -> str:
    """`@scope/name/sub/path` -> `@scope/name`; `lodash/fp` -> `lodash`."""
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def is_builtin(specifier: str) -> bool:
    if specifier.startswith("node:"):
        return True  # always valid, including names newer than our list
    root = specifier.split("/")[0]
    return root in BUILTINS


class NodeProject:
    """Filesystem-backed resolution for one project tree."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- entry point -----------------------------------------------------

    def resolve(self, specifier: str, importer: Path) -> Resolution:
        if not specifier or specifier.startswith(("http:", "https:", "data:")):
            return Resolution("unknown", reason="not a module specifier")

        if is_builtin(specifier):
            return Resolution("builtin")

        if is_relative(specifier):
            found = self._resolve_file(importer.parent / specifier)
            if found is not None:
                return Resolution("file", path=found)
            return Resolution("missing_file")

        # Bare specifier. Could be a package, or a tsconfig alias wearing the
        # same clothes.
        package = package_root(specifier)
        installed = self._find_in_node_modules(package, importer)
        if installed is not None:
            return Resolution("installed", path=installed, package=package)

        alias = self._alias_reason(specifier)
        if alias:
            return Resolution("unknown", package=package, reason=alias)

        if package in self.declared_dependencies():
            return Resolution("declared", package=package)

        return Resolution("absent", package=package)

    # -- filesystem ------------------------------------------------------

    def _resolve_file(self, target: Path) -> Path | None:
        """Apply Node/TypeScript extension and index fallbacks."""
        try:
            if target.is_file():
                return target
        except OSError:
            return None

        # `./utils` -> ./utils.ts, and `./utils.js` -> ./utils.ts, which is how
        # ESM-style TypeScript imports its own source.
        candidates = [target.with_name(target.name + ext) for ext in EXTENSIONS]
        if target.suffix in {".js", ".mjs", ".cjs", ".jsx"}:
            stem = target.with_suffix("")
            candidates = [stem.with_name(stem.name + ext) for ext in EXTENSIONS] + candidates

        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue

        for ext in EXTENSIONS:
            index = target / f"index{ext}"
            try:
                if index.is_file():
                    return index
            except OSError:
                continue
        return None

    def _find_in_node_modules(self, package: str, importer: Path) -> Path | None:
        """Walk up from the importing file, exactly as Node does."""
        start = importer.parent if importer.suffix else importer
        for directory in [start, *start.parents]:
            candidate = directory / "node_modules" / package
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                pass
            if directory == self.root:
                break
        # Monorepos hoist to the workspace root, which may sit above the
        # project root we were given.
        for directory in [self.root, *self.root.parents][:4]:
            candidate = directory / "node_modules" / package
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue
        return None

    # -- manifests -------------------------------------------------------

    @lru_cache(maxsize=1)
    def declared_dependencies(self) -> frozenset[str]:
        data = _read_json(self.root / "package.json")
        if not isinstance(data, dict):
            return frozenset()
        names: set[str] = set()
        for field in DEPENDENCY_FIELDS:
            section = data.get(field)
            if isinstance(section, dict):
                names.update(k for k in section if isinstance(k, str))
        return frozenset(names)

    @lru_cache(maxsize=1)
    def _alias_prefixes(self) -> tuple[tuple[str, ...], bool]:
        """(prefixes from tsconfig `paths`, whether `baseUrl` is set).

        A project with `baseUrl` can import its own files with bare-looking
        specifiers, so in that case no bare specifier can be called invented
        purely because it is missing from node_modules.
        """
        prefixes: set[str] = set()
        base_url = False
        for name in ("tsconfig.json", "jsconfig.json"):
            data = _read_json(self.root / name)
            if not isinstance(data, dict):
                continue
            options = data.get("compilerOptions")
            if not isinstance(options, dict):
                continue
            if options.get("baseUrl"):
                base_url = True
            paths = options.get("paths")
            if isinstance(paths, dict):
                prefixes.update(k for k in paths if isinstance(k, str))
        return tuple(sorted(prefixes)), base_url

    def _alias_reason(self, specifier: str) -> str:
        prefixes, base_url = self._alias_prefixes()
        for pattern in prefixes:
            head = pattern.split("*")[0]
            if head and specifier.startswith(head):
                return f"matches tsconfig path alias {pattern!r}"
            if pattern == specifier:
                return f"declared as tsconfig path {pattern!r}"
        if base_url:
            return "project sets tsconfig baseUrl, so bare specifiers may be local"
        return ""


def _read_json(path: Path):
    """Parse JSON that may contain comments and trailing commas.

    tsconfig.json is JSON with comments by convention, and package.json
    occasionally picks up a trailing comma. A parse failure here must never
    produce a verdict, so callers treat None as "unknown".
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    stripped = _strip_jsonc(text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    i = 0
    in_string = False
    length = len(text)
    while i < length:
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < length:
                out.append(text[i + 1])
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = length if end == -1 else end + 2
            continue
        out.append(char)
        i += 1

    # Trailing commas before a closing brace or bracket.
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))
