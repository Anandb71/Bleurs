"""Tier 3: ask the installed library what it actually contains.

This is the only tier that can prove an *API* hallucination -- that `pandas` is
real but `pandas.read_jsonl` is not. There is no way to do that from metadata;
you have to load the object and look.

Two consequences, both handled here:

1. Loading a module runs its import side effects. We do it in a subprocess with
   a timeout so a package that hangs, prints, mutates globals, or calls
   `sys.exit` on import cannot take the checker with it. `--no-introspect`
   turns the tier off entirely and the engine degrades to abstaining.

2. Python objects can synthesize attributes at lookup time. Any module with a
   module-level `__getattr__` (PEP 562 -- what every lazy-loading library uses)
   or any object whose type overrides `__getattr__` is marked dynamic, and the
   engine must not conclude absence from a `dir()` listing that was never
   authoritative to begin with.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

#: Runs in a child interpreter. Reads a JSON query list on stdin, writes a JSON
#: result list on stdout. Deliberately dependency-free and defensive: it is
#: executing code we did not write.
_PROBE = r"""
import inspect, json, re, sys, types, warnings

warnings.simplefilter("ignore")

MAX_MEMBERS = 500
MAX_METHODS = 40

# Everything every object inherits. Listing add_note and with_traceback on each
# exception class costs tokens and tells the reader nothing they did not know.
NOISE = set(dir(object)) | set(dir(BaseException)) | {"mro"}

# Default values reprd as <object at 0x7f...>. The address changes every run,
# which would make the output non-deterministic and uncacheable for no benefit.
ADDRESS = re.compile(r"<[^<>]*? at 0x[0-9a-fA-F]+>")


def kind_of(obj):
    if isinstance(obj, types.ModuleType):
        return "module"
    if inspect.isclass(obj):
        return "class"
    if inspect.isroutine(obj):
        return "function"
    return "value"


def signature_of(obj):
    # Builtins and C extensions often have no introspectable signature. That is
    # a missing detail, not an error -- the name and kind still carry most of
    # what a caller needs.
    try:
        text = str(inspect.signature(obj))
    except BaseException:
        return None
    return ADDRESS.sub("...", text)


def summary_of(obj):
    # First line of the docstring only. The rest is prose the caller can go
    # read; the first line is the part that disambiguates two similar names.
    try:
        doc = inspect.getdoc(obj)
    except BaseException:
        return None
    if not doc:
        return None
    line = doc.strip().split("\n")[0].strip()
    return line[:140] or None


def describe(obj, name, deep):
    entry = {"name": name, "kind": kind_of(obj)}
    signature = signature_of(obj)
    if signature:
        entry["signature"] = signature
    summary = summary_of(obj)
    if summary:
        entry["summary"] = summary

    if deep and inspect.isclass(obj):
        methods = []
        for child in public_names(obj)[:MAX_METHODS]:
            if child in NOISE:
                continue
            try:
                value = getattr(obj, child)
            except BaseException:
                continue
            if not inspect.isroutine(value) and not isinstance(value, property):
                continue
            methods.append(describe(value, child, False))
        if methods:
            entry["members"] = methods
    return entry


def surface(module, path):
    out = {"module": module, "path": path, "ok": False, "error": None, "members": []}
    try:
        obj = __import__(module, fromlist=["__name__"])
        for part in path:
            obj = getattr(obj, part)
    except BaseException as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out

    out["ok"] = True
    out["kind"] = kind_of(obj)
    out["summary"] = summary_of(obj)

    is_module = out["kind"] == "module"
    for name in public_names(obj)[:MAX_MEMBERS]:
        if not is_module and name in NOISE:
            continue
        try:
            value = getattr(obj, name)
        except BaseException:
            continue
        out["members"].append(describe(value, name, is_module))
    return out


def is_dynamic(obj):
    # Can this object synthesize attributes that dir() will not list?
    #
    # Careful here: ModuleType and type both override __getattribute__ as a
    # matter of course, so comparing against object's slot marks literally
    # every module and every class as dynamic -- and a checker that abstains
    # on everything is indistinguishable from one that does nothing.
    #
    # Attribute lookup on obj is governed by type(obj), so that is what we
    # inspect, skipping the two builtin bases whose overrides are structural
    # rather than a deliberate interception.
    if isinstance(obj, types.ModuleType):
        # PEP 562: a module is dynamic only if it defines __getattr__ itself.
        try:
            return "__getattr__" in vars(obj)
        except TypeError:
            return True

    for klass in getattr(type(obj), "__mro__", ()):
        if klass is object or klass is type:
            continue
        try:
            namespace = vars(klass)
        except TypeError:
            return True
        if "__getattr__" in namespace or "__getattribute__" in namespace:
            return True
    return False


def public_names(obj):
    try:
        names = list(getattr(obj, "__all__", None) or dir(obj))
    except Exception:
        return []
    return [n for n in names if isinstance(n, str) and not n.startswith("_")][:4000]


def probe(module, path):
    out = {
        "module": module,
        "path": path,
        "module_ok": False,
        "module_error": None,
        "error_type": None,
        "missing_module": None,
        "resolved": False,
        "missing_at": None,
        "container": module,
        "candidates": [],
        "dynamic": False,
    }
    try:
        obj = __import__(module, fromlist=["__name__"])
    except BaseException as exc:
        out["module_error"] = "%s: %s" % (type(exc).__name__, exc)
        out["error_type"] = type(exc).__name__
        # ModuleNotFoundError.name says *which* module was missing. A package
        # that fails to import because one of its own dependencies is absent
        # tells us nothing about the name we were asked about, and must not be
        # mistaken for evidence against it.
        out["missing_module"] = getattr(exc, "name", None)
        return out

    out["module_ok"] = True
    container = obj
    walked = []
    for part in path:
        if is_dynamic(container):
            out["dynamic"] = True
            out["resolved"] = True  # unknowable, so treat as present
            return out
        try:
            nxt = getattr(container, part)
        except BaseException:
            # A submodule is not an attribute of its package until something
            # imports it. `from os import path` works; `getattr(os, "path")`
            # happening to work is luck, not a rule. Try the import before
            # concluding anything.
            nxt = try_submodule(module, walked, part)
            if nxt is None:
                out["missing_at"] = part
                out["container"] = ".".join([module] + walked)
                out["candidates"] = public_names(container)
                return out
        container = nxt
        walked.append(part)

    out["resolved"] = True
    out["dynamic"] = is_dynamic(container)
    return out


def try_submodule(module, walked, part):
    dotted = ".".join([module] + walked + [part])
    try:
        __import__(dotted)
    except BaseException:
        return None
    return sys.modules.get(dotted)


def main():
    try:
        queries = json.load(sys.stdin)
    except Exception:
        sys.stdout.write("[]")
        return
    results = []
    for q in queries:
        try:
            if q.get("op") == "surface":
                results.append(surface(q["module"], q.get("path") or []))
                continue
            results.append(probe(q["module"], q.get("path") or []))
        except BaseException as exc:
            results.append({
                "module": q.get("module"),
                "path": q.get("path") or [],
                "module_ok": False,
                "module_error": "probe failed: %s" % exc,
                "resolved": False,
                "missing_at": None,
                "container": q.get("module"),
                "candidates": [],
                "dynamic": False,
            })
    sys.stdout.write(json.dumps(results))


main()
"""


@dataclass(frozen=True)
class Probe:
    """What the child interpreter found."""

    module_ok: bool
    module_error: str | None
    resolved: bool
    missing_at: str | None
    container: str
    candidates: tuple[str, ...]
    dynamic: bool
    error_type: str | None = None
    missing_module: str | None = None

    @property
    def proves_absence(self) -> bool:
        """Only true when we loaded the container and the name was not in it."""
        return self.module_ok and not self.resolved and not self.dynamic

    def proves_module_absent(self, module: str) -> bool:
        """Did the import fail *because this module does not exist*?

        Anything else -- a missing transitive dependency, a package that raises
        on import, a compiled extension built for the wrong platform -- is a
        broken environment, not a hallucination, and gets no verdict from us.
        """
        if self.module_ok or self.error_type != "ModuleNotFoundError":
            return False
        if not self.missing_module:
            return False
        return module == self.missing_module or module.startswith(
            self.missing_module + "."
        )


@dataclass(frozen=True)
class Member:
    """One public name on a module or class."""

    name: str
    kind: str
    signature: str | None = None
    summary: str | None = None
    members: tuple["Member", ...] = ()


@dataclass(frozen=True)
class Surface:
    """The public API of a module or class, as it actually is right now.

    This is the retrieval half of the tool. The same subprocess that proves a
    name is absent can enumerate the names that are present, which means the
    index that blocks a hallucination is also the index that prevents the next
    one -- without the agent reading a single line of the library.
    """

    module: str
    path: tuple[str, ...] = ()
    ok: bool = False
    error: str | None = None
    kind: str = "module"
    summary: str | None = None
    members: tuple[Member, ...] = ()

    @property
    def dotted(self) -> str:
        return ".".join((self.module, *self.path))


def _member_from(raw: dict) -> Member:
    return Member(
        name=raw.get("name") or "",
        kind=raw.get("kind") or "value",
        signature=raw.get("signature"),
        summary=raw.get("summary"),
        members=tuple(_member_from(m) for m in raw.get("members") or ()),
    )


class Introspector:
    """Batched, sandboxed attribute resolution."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout: float = 20.0,
        python: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.timeout = timeout
        self.python = python or sys.executable
        self._cache: dict[tuple[str, tuple[str, ...]], Probe] = {}

    def resolve(
        self, queries: list[tuple[str, tuple[str, ...]]]
    ) -> dict[tuple[str, tuple[str, ...]], Probe]:
        """Resolve many (module, path) pairs in one child process.

        One subprocess per check run, not per reference -- a file importing
        numpy, pandas and torch should pay the import cost once.
        """
        if not self.enabled:
            return {}

        pending = [q for q in dict.fromkeys(queries) if q not in self._cache]
        if pending:
            for key, probe in self._run(pending).items():
                self._cache[key] = probe

        return {q: self._cache[q] for q in queries if q in self._cache}

    def surface(self, module: str, path: tuple[str, ...] = ()) -> Surface:
        """Enumerate what a module or class actually contains."""
        return self.surfaces([(module, path)]).get(
            (module, path),
            Surface(module=module, path=path, error="probe failed"),
        )

    def surfaces(
        self, targets: list[tuple[str, tuple[str, ...]]]
    ) -> dict[tuple[str, tuple[str, ...]], Surface]:
        """Project several surfaces in one child process.

        Batched for the same reason probes are: the expensive part is importing
        the libraries, and doing that once for the whole request beats doing it
        once per question.
        """
        if not self.enabled or not targets:
            return {}

        unique = list(dict.fromkeys(targets))
        raw = self._exec(
            [{"op": "surface", "module": m, "path": list(p)} for m, p in unique]
        )

        out: dict[tuple[str, tuple[str, ...]], Surface] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = (item.get("module") or "", tuple(item.get("path") or ()))
            out[key] = Surface(
                module=key[0],
                path=key[1],
                ok=bool(item.get("ok")),
                error=item.get("error"),
                kind=item.get("kind") or "module",
                summary=item.get("summary"),
                members=tuple(_member_from(m) for m in item.get("members") or ()),
            )
        return out

    def _exec(self, queries: list[dict]) -> list[dict]:
        """Run the probe script once and hand back whatever it managed to say."""
        try:
            proc = subprocess.run(
                [self.python, "-I", "-c", _PROBE],
                input=json.dumps(queries),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            # A hung or unlaunchable probe is a failure to gather evidence.
            # Returning nothing makes the engine abstain, which is right.
            return []

        try:
            results = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            # A package printed to stdout on import and corrupted our channel.
            return []
        return results if isinstance(results, list) else []

    def _run(
        self, queries: list[tuple[str, tuple[str, ...]]]
    ) -> dict[tuple[str, tuple[str, ...]], Probe]:
        results = self._exec([{"module": m, "path": list(p)} for m, p in queries])

        out: dict[tuple[str, tuple[str, ...]], Probe] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            key = (item.get("module") or "", tuple(item.get("path") or ()))
            out[key] = Probe(
                module_ok=bool(item.get("module_ok")),
                module_error=item.get("module_error"),
                resolved=bool(item.get("resolved")),
                missing_at=item.get("missing_at"),
                container=item.get("container") or key[0],
                candidates=tuple(item.get("candidates") or ()),
                dynamic=bool(item.get("dynamic")),
                error_type=item.get("error_type"),
                missing_module=item.get("missing_module"),
            )
        return out
