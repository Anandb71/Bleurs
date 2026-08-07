"""The suite that matters.

Every test here asserts that bleurs stays *quiet*. They are written first and
kept first on purpose: the tool's only real failure mode is blocking correct
code, and every one of these is a pattern that a naive checker gets wrong.

If a change makes one of these fail, the change is wrong, however many extra
hallucinations it catches.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def check(engine, source: str, name: str = "sample.py"):
    return engine.check_source(source, Path(name))


def test_plain_stdlib_usage_is_silent(make_engine):
    report = check(
        make_engine(),
        """
import json
import os.path

def go(raw, a, b):
    return json.dumps(os.path.join(a, b)) + json.loads(raw)["x"]
""",
    )
    assert report.blocks == []
    assert report.checked > 0


def test_submodule_reached_through_parent_binding(make_engine):
    # `os.path` is a submodule, not an attribute of `os` in any guaranteed
    # sense. Checkers that assume dir() covers submodules fire here.
    report = check(
        make_engine(),
        """
import os

def go(p):
    return os.path.dirname(p)
""",
    )
    assert report.blocks == []


def test_submodule_import_not_preloaded_by_parent(make_engine):
    report = check(
        make_engine(),
        """
import concurrent.futures

def go():
    return concurrent.futures.ThreadPoolExecutor()
""",
    )
    assert report.blocks == []


def test_shadowed_module_name_is_abstained_not_blocked(make_engine):
    # `json` is rebound, so `json.whatever` is unknowable. Silence is correct;
    # blocking would be a false positive on completely valid code.
    report = check(
        make_engine(),
        """
import json

def render(template):
    json = template.get("renderer")
    return json.render_to_string()
""",
    )
    assert report.blocks == []


def test_wildcard_import_disables_name_claims(make_engine):
    report = check(
        make_engine(known={"os"}),
        """
from os.path import *

def go(a, b):
    return join(a, b)
""",
    )
    assert report.blocks == []


def test_optional_import_of_missing_package_never_blocks(make_engine):
    # A guarded import is a deliberate statement that the dependency is
    # optional. Every real codebase does this.
    report = check(
        make_engine(known=set()),
        """
try:
    import ujson
except ImportError:
    ujson = None
""",
    )
    assert report.blocks == []


def test_offline_registry_is_not_evidence(make_engine):
    # The single most dangerous shortcut available: treating a failed lookup
    # as proof the package does not exist.
    report = check(
        make_engine(offline=True),
        "import some_package_that_may_or_may_not_exist\n",
    )
    assert report.blocks == []


def test_known_alias_survives_an_empty_registry(make_engine, nothing_installed):
    # `yaml` ships from PyYAML, so there is no PyPI project called `yaml`.
    # Judging the import name against the registry directly would block a
    # completely ordinary import. The alias table exists to stop that.
    report = check(make_engine(known=set()), "import yaml\n")
    assert report.blocks == []
    assert report.warnings[0].suggestion == "pip install PyYAML"


def test_namespace_package_root_is_never_blocked(make_engine):
    report = check(make_engine(known=set()), "import azure.storage.blob\n")
    assert report.blocks == []


def test_attribute_on_a_local_variable_is_not_a_claim(make_engine):
    report = check(
        make_engine(),
        """
def go(client):
    return client.totally_made_up_method()
""",
    )
    assert report.blocks == []


def test_attribute_on_a_call_result_is_not_a_claim(make_engine):
    report = check(
        make_engine(),
        """
import json

def go(raw):
    return json.loads(raw).some_invented_helper()
""",
    )
    assert report.blocks == []


def test_from_import_binding_is_not_followed(make_engine):
    # `loads` is a function, not a module. We must not pretend to know what
    # attributes it has.
    report = check(
        make_engine(),
        """
from json import loads

def go(raw):
    return loads(raw).anything_at_all
""",
    )
    assert report.blocks == []


def test_syntax_error_is_skipped_not_blocked(make_engine):
    report = check(make_engine(), "def broken(:\n    pass\n")
    assert report.parse_error is not None
    assert report.blocks == []


def test_dynamic_module_attributes_are_abstained(make_engine, tmp_path):
    # A module with PEP 562 __getattr__ can produce any name on demand, so
    # dir() proves nothing about what is absent.
    package = tmp_path / "lazyish"
    package.mkdir()
    (package / "__init__.py").write_text(
        "def __getattr__(name):\n    return 42\n", encoding="utf-8"
    )

    engine = make_engine(project_root=tmp_path)
    report = engine.check_source(
        "import lazyish\n\nx = lazyish.anything_you_like\n",
        tmp_path / "user.py",
    )
    assert report.blocks == []


def test_attribute_guarded_by_try_except_is_not_blocked(make_engine):
    # Found by running bleurs against its own source on Linux CI. `ctypes.windll`
    # exists only on Windows, and this is exactly how every cross-platform
    # codebase reaches for it.
    report = check(
        make_engine(),
        """
import ctypes

def enable():
    try:
        return ctypes.windll.kernel32.GetStdHandle(-11)
    except Exception:
        return None
""",
    )
    assert report.blocks == []


def test_the_same_attribute_unguarded_is_still_blocked(make_engine):
    # The guard has to be doing real work, not just switching checking off.
    report = check(
        make_engine(),
        """
import base64

def go(x):
    return base64.encode_string(x)
""",
    )
    assert len(report.blocks) == 1


@pytest.mark.parametrize(
    "test",
    [
        'sys.platform == "win32"',
        'os.name == "nt"',
        'platform.system() == "Windows"',
        "sys.version_info >= (3, 12)",
    ],
)
def test_platform_gated_references_are_not_blocked(make_engine, test):
    report = check(
        make_engine(known=set()),
        f"""
import os
import sys
import platform

if {test}:
    import invented_platform_shim
    x = os.invented_platform_call()
""",
    )
    assert report.blocks == []


def test_platform_gated_else_branch_is_also_covered(make_engine):
    # Which branch is live depends on the machine running the code, not the
    # machine running the checker.
    report = check(
        make_engine(known=set()),
        """
import sys
import base64

if sys.platform == "win32":
    result = 1
else:
    result = base64.posix_only_helper()
""",
    )
    assert report.blocks == []


@pytest.mark.parametrize(
    "source",
    [
        "import os\nfor os in range(3):\n    pass\nx = os.invented\n",
        "import os\nwith open('f') as os:\n    x = os.invented\n",
        "import os\ntry:\n    pass\nexcept ValueError as os:\n    x = os.invented\n",
        "import os\nos, y = 1, 2\nx = os.invented\n",
        "import os\ndef f(os):\n    return os.invented\n",
        "import os\n[os for os in range(3)]\nx = os.invented\n",
    ],
)
def test_every_rebinding_form_suppresses_claims(make_engine, source):
    assert check(make_engine(), source).blocks == []
