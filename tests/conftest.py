from __future__ import annotations

import pytest

from bleurs.engine import Config, Engine


class StubRegistry:
    """A PyPI stand-in, so the suite never depends on the network.

    `known` is the set of names that exist. Anything else is absent. `offline`
    makes every lookup fail the way a dropped connection does, which is the
    case the engine must treat as "no evidence" rather than "does not exist".
    """

    def __init__(self, known: set[str] | None = None, offline: bool = False) -> None:
        self.known = known or set()
        self.offline = offline
        self.network_failed = False
        self.queries: list[str] = []

    def exists(self, project: str):
        self.queries.append(project)
        if self.offline:
            self.network_failed = True
            return None
        return project.lower() in {k.lower() for k in self.known}

    def flush(self) -> None:
        pass


@pytest.fixture
def nothing_installed(monkeypatch):
    """Pretend the environment is empty.

    Several rules only fire for packages that are absent locally, and whether
    `requests` or `yaml` happens to be installed on the machine running the
    suite is not something a test should depend on.
    """
    monkeypatch.setattr("bleurs.engine.top_level_module_exists", lambda name: False)


@pytest.fixture
def make_engine(tmp_path):
    """Build an engine with a stubbed registry and a real project root."""

    def _make(
        known: set[str] | None = None,
        offline: bool = False,
        npm: set[str] | None = None,
        **config,
    ):
        config.setdefault("project_root", tmp_path)
        engine = Engine(Config(**config))
        engine.registry = StubRegistry(known, offline)
        engine.npm = StubRegistry(npm, offline)
        return engine

    return _make
