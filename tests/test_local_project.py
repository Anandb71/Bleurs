"""Project-local resolution.

The most common agent failure in a real repo is not inventing a PyPI package --
it is calling a helper that was never written. These tests cover that, plus the
relative-import anchoring it depends on.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def project(tmp_path):
    pkg = tmp_path / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "utils.py").write_text(
        "def existing_helper():\n    return 1\n\n\nCONSTANT = 2\n",
        encoding="utf-8",
    )
    services = pkg / "services"
    services.mkdir()
    (services / "__init__.py").write_text("", encoding="utf-8")
    (services / "billing.py").write_text(
        "def charge(amount):\n    return amount\n", encoding="utf-8"
    )
    return tmp_path


def test_missing_local_helper_is_blocked(make_engine, project):
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from myapp.utils import helper_that_was_never_written\n",
        project / "main.py",
    )
    assert len(report.blocks) == 1
    assert report.blocks[0].resolver == "project"


def test_existing_local_helper_passes(make_engine, project):
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from myapp.utils import existing_helper, CONSTANT\n",
        project / "main.py",
    )
    assert report.blocks == []


def test_local_near_miss_gets_a_suggestion(make_engine, project):
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from myapp.utils import existing_helpers\n", project / "main.py"
    )
    assert report.blocks[0].suggestion == "existing_helper"


def test_importing_a_local_subpackage_passes(make_engine, project):
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from myapp import services\n", project / "main.py"
    )
    assert report.blocks == []


def test_relative_import_is_anchored_and_checked(make_engine, project):
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from .billing import charge\n",
        project / "myapp" / "services" / "orders.py",
    )
    assert report.blocks == []


def test_relative_import_of_a_missing_name_is_blocked(make_engine, project):
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from .billing import refund\n",
        project / "myapp" / "services" / "orders.py",
    )
    assert len(report.blocks) == 1


def test_parent_relative_import_walks_up_one_package(make_engine, project):
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from ..utils import existing_helper\n",
        project / "myapp" / "services" / "orders.py",
    )
    assert report.blocks == []


def test_relative_import_from_a_package_init_stays_in_that_package(
    make_engine, project
):
    # `__init__.py` is its own package, so one dot means "here", not "up".
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from .billing import charge\n",
        project / "myapp" / "services" / "__init__.py",
    )
    assert report.blocks == []


def test_unanchorable_relative_import_abstains(make_engine, tmp_path):
    engine = make_engine(project_root=tmp_path)
    report = engine.check_source(
        "from .nowhere import thing\n", tmp_path.parent / "stray.py"
    )
    assert report.blocks == []
    assert report.abstentions


def test_local_module_with_a_wildcard_import_stops_answering(make_engine, project):
    (project / "myapp" / "reexport.py").write_text(
        "from myapp.utils import *\n", encoding="utf-8"
    )
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from myapp.reexport import anything\n", project / "main.py"
    )
    assert report.blocks == []


def test_reexported_name_counts_as_defined(make_engine, project):
    (project / "myapp" / "api.py").write_text(
        "from myapp.utils import existing_helper\n", encoding="utf-8"
    )
    engine = make_engine(project_root=project)
    report = engine.check_source(
        "from myapp.api import existing_helper\n", project / "main.py"
    )
    assert report.blocks == []
