"""The cross-platform stdlib union.

Introspection sees one operating system. These tests pin the rule that lets a
verdict be reached anyway, and — more importantly — the rule that keeps the
table from ever causing harm: it may only permit.

Every assertion here holds on Linux, macOS and Windows. A name real on Unix
resolves directly when the suite runs there and via the table when it does not,
and either way the answer is the same.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bleurs import Config, Engine
from bleurs.truth.platform_surface import (
    covered_versions,
    exists_somewhere,
    running_version_is_covered,
)


@pytest.fixture
def check():
    engine = Engine(Config(network=False))

    def _check(source):
        return engine.check_source(source, Path("sample.py"))

    return _check


# -- the table itself ----------------------------------------------------


def test_table_covers_three_platforms_and_the_supported_versions():
    assert covered_versions() >= {"3.10", "3.11", "3.12", "3.13"}


def test_names_real_on_another_platform_are_known():
    # None of these exist on Windows; all exist on Unix.
    assert exists_somewhere("signal", "SIGQUIT") is True
    assert exists_somewhere("socket", "AF_UNIX") is True
    assert exists_somewhere("os", "fork") is True
    # And the reverse: a Windows-only name, from a Unix runner's perspective.
    assert exists_somewhere("sys", "getwindowsversion") is True


def test_invented_names_are_absent_everywhere():
    assert exists_somewhere("signal", "register_all") is False
    assert exists_somewhere("socket", "AF_QUANTUM") is False
    assert exists_somewhere("os", "fork_all") is False


def test_uncovered_containers_return_no_opinion():
    # The table only lists platform-varying modules. Anything else must fall
    # through to the normal introspection path rather than being judged here.
    assert exists_somewhere("json", "loads_safe") is None
    assert exists_somewhere("not_a_module", "whatever") is None


def test_an_empty_name_is_never_answered():
    assert exists_somewhere("signal", "") is None


def test_the_table_declines_outside_its_python_range(monkeypatch):
    # A table built for 3.10-3.13 says nothing useful about 3.15.
    monkeypatch.setattr(
        "bleurs.truth.platform_surface.running_version_is_covered", lambda: False
    )
    from bleurs.truth import platform_surface

    assert platform_surface.exists_somewhere("signal", "SIGQUIT") is None


def test_running_interpreter_is_covered():
    assert running_version_is_covered()


# -- end to end ----------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import signal\nsignal.signal(signal.SIGQUIT, None)\n",
        "import socket\nx = socket.AF_UNIX\n",
        "import os\nos.fork()\n",
        "import signal\nsignal.siginterrupt(signal.SIGTERM, False)\n",
        "import os\nx = os.O_NONBLOCK\n",
    ],
)
def test_platform_specific_names_are_never_blocked(check, source):
    assert check(source).blocks == []


@pytest.mark.parametrize(
    "source",
    [
        "import signal\nsignal.register_all()\n",
        "import socket\nx = socket.AF_QUANTUM\n",
        "import os\nos.fork_all()\n",
    ],
)
def test_names_absent_on_every_platform_are_blocked(check, source):
    # Before the table these abstained alongside the real ones, because
    # introspecting a single machine could not tell them apart.
    assert len(check(source).blocks) == 1
