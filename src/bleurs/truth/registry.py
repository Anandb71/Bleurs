"""Tier 4: does a package by this name exist on PyPI at all?

This is the tier that catches slopsquatting -- the model invents a plausible
package, the developer pip-installs it, and whoever registered that name owns
the machine. A 2026 study across five frontier models found package
hallucination rates of 4.6%-6.1%, with 127 names invented *identically* by all
five. Those names are predictable, which is exactly what makes them
registerable by an attacker, and exactly what makes them checkable by us.

Cache semantics are chosen with that attack in mind. A cached "absent" that
later becomes present errs toward blocking, which is the safe direction, so
negative results are cached for a long time without risk. Network failure is
never treated as absence.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .env import normalize_project_name

_SIMPLE_INDEX = "https://pypi.org/simple/{name}/"
_USER_AGENT = "bleurs/0.1 (+https://github.com/anandbiju/bleurs)"

_TTL_PRESENT = 30 * 24 * 3600
_TTL_ABSENT = 7 * 24 * 3600


def cache_path(name: str = "registry.json") -> Path:
    base = os.environ.get("BLEURS_CACHE_DIR")
    if base:
        return Path(base) / name
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "bleurs" / name
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "bleurs" / name


class ExistenceCache:
    """Does a name exist in some package registry? Cached on disk.

    Shared by PyPI and npm because the question, the caching policy and the
    consequences of getting it wrong are identical in both ecosystems. Only
    the URL and the name normalization differ.
    """

    cache_file = "registry.json"
    url_template = ""
    accept = "*/*"

    def __init__(self, *, enabled: bool = True, timeout: float = 4.0) -> None:
        self.enabled = enabled
        self.timeout = timeout
        self._path = cache_path(self.cache_file)
        self._cache: dict[str, dict] = self._load()
        self._dirty = False
        #: Set when any lookup failed for network reasons, so the engine can
        #: report "unverified" honestly instead of silently allowing.
        self.network_failed = False

    # -- subclass hooks --------------------------------------------------

    def normalize(self, name: str) -> str:
        return name

    def url(self, normalized: str) -> str:
        return self.url_template.format(name=normalized)

    # -- public ----------------------------------------------------------

    def exists(self, project: str) -> bool | None:
        """True / False / None, where None means "we could not find out"."""
        if not self.enabled:
            return None

        key = self.normalize(project)
        if not key:
            return None

        entry = self._cache.get(key)
        if entry is not None:
            age = time.time() - entry.get("at", 0)
            ttl = _TTL_PRESENT if entry.get("exists") else _TTL_ABSENT
            if age < ttl:
                return bool(entry["exists"])

        found = self._fetch(key)
        if found is None:
            self.network_failed = True
            return None

        self._cache[key] = {"exists": found, "at": time.time()}
        self._dirty = True
        return found

    def flush(self) -> None:
        if not self._dirty:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache), encoding="utf-8")
            tmp.replace(self._path)
            self._dirty = False
        except OSError:  # pragma: no cover - cache is a nicety, never a blocker
            pass

    # -- internals -------------------------------------------------------

    def _fetch(self, normalized: str) -> bool | None:
        request = urllib.request.Request(
            self.url(normalized),
            method="HEAD",
            headers={"User-Agent": _USER_AGENT, "Accept": self.accept},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}


class Registry(ExistenceCache):
    """PyPI name existence."""

    cache_file = "registry.json"
    url_template = _SIMPLE_INDEX
    accept = "application/vnd.pypi.simple.v1+json"

    def normalize(self, name: str) -> str:
        return normalize_project_name(name)


class NpmRegistry(ExistenceCache):
    """npm name existence.

    npm has the harder version of this problem. Its namespace is flat, vastly
    larger, and has no PEP 503 normalization, so a hallucinated name is both
    more likely and cheaper for an attacker to claim. Package names are already
    lowercase by rule, so normalization is a no-op beyond trimming.
    """

    cache_file = "npm.json"
    url_template = "https://registry.npmjs.org/{name}"
    accept = "application/json"

    def normalize(self, name: str) -> str:
        name = name.strip().strip("/")
        # Scoped names carry a slash the registry accepts unencoded, but a
        # leading or doubled slash would silently query the wrong path.
        return name if "//" not in name else ""
