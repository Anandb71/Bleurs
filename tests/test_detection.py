"""What bleurs is supposed to catch, and the receipts it must produce."""

from __future__ import annotations

from pathlib import Path

from bleurs.refs import Confidence, Verdict


def check(engine, source: str, name: str = "sample.py"):
    return engine.check_source(source, Path(name))


def test_invented_package_is_blocked(make_engine, nothing_installed):
    report = check(make_engine(known={"requests"}), "import totally_invented_pkg\n")
    assert len(report.blocks) == 1
    finding = report.blocks[0]
    assert finding.confidence is Confidence.ABSENT
    assert finding.resolver == "registry"
    assert "totally_invented_pkg" in finding.message


def test_real_but_uninstalled_package_only_warns(make_engine, nothing_installed):
    report = check(make_engine(known={"requests"}), "import requests\n")
    assert report.blocks == []
    assert len(report.warnings) == 1
    assert "pip install requests" in report.warnings[0].suggestion


def test_invented_stdlib_attribute_is_blocked(make_engine):
    report = check(
        make_engine(),
        """
import json

def go(raw):
    return json.loads_safe(raw)
""",
    )
    assert len(report.blocks) == 1
    finding = report.blocks[0]
    assert finding.resolver == "introspect"
    assert "loads_safe" in finding.message


def test_deep_attribute_path_reports_the_failing_step(make_engine):
    report = check(
        make_engine(),
        """
import datetime

def go():
    return datetime.datetime.now_utc()
""",
    )
    assert len(report.blocks) == 1
    assert "datetime.datetime" in report.blocks[0].message
    assert "now_utc" in report.blocks[0].message


def test_invented_member_of_a_real_module_is_blocked(make_engine):
    report = check(make_engine(), "from json import loads_safely\n")
    assert len(report.blocks) == 1
    assert report.blocks[0].resolver == "introspect"


def test_near_miss_gets_a_suggestion(make_engine):
    report = check(
        make_engine(),
        """
import base64

def go(x):
    return base64.b64encodee(x)
""",
    )
    assert len(report.blocks) == 1
    assert report.blocks[0].suggestion == "base64.b64encode"


def test_distant_miss_gets_no_suggestion(make_engine):
    # A confident wrong suggestion is worse than none: the agent will take it.
    report = check(
        make_engine(),
        """
import base64

def go(x):
    return base64.encrypt_everything_now(x)
""",
    )
    assert len(report.blocks) == 1
    assert report.blocks[0].suggestion is None


def test_attribute_through_an_alias_is_checked(make_engine):
    report = check(
        make_engine(),
        """
import json as j

def go(raw):
    return j.parse_document(raw)
""",
    )
    assert len(report.blocks) == 1
    assert "parse_document" in report.blocks[0].message


def test_missing_submodule_of_an_installed_package_is_blocked(make_engine):
    report = check(make_engine(), "import json.encoder_deluxe\n")
    assert len(report.blocks) == 1


def test_dynamic_import_with_a_literal_name_is_checked(make_engine):
    report = check(
        make_engine(known=set()),
        """
import importlib

mod = importlib.import_module("invented_runtime_plugin")
""",
    )
    assert len(report.blocks) == 1


def test_dynamic_import_with_a_computed_name_is_not(make_engine):
    report = check(
        make_engine(known=set()),
        """
import importlib

def load(name):
    return importlib.import_module(name)
""",
    )
    assert report.blocks == []


def test_strict_imports_off_downgrades_to_warning(make_engine):
    report = check(
        make_engine(known=set(), strict_imports=False),
        "import definitely_not_a_real_package\n",
    )
    assert report.blocks == []
    assert len(report.warnings) == 1


def test_no_introspect_still_catches_fake_packages(make_engine):
    engine = make_engine(known=set(), introspect=False)
    report = check(engine, "import definitely_not_a_real_package\n")
    assert len(report.blocks) == 1

    # ...but says nothing about APIs, and admits it.
    quiet = check(engine, "import json\nx = json.loads_safe('')\n")
    assert quiet.blocks == []
    assert quiet.abstentions


def test_json_output_only_reports_actionable_findings(make_engine):
    from bleurs.cli import _as_json

    report = check(make_engine(), "import json\nx = json.loads_safe('')\n")
    payload = _as_json([report])
    assert payload["blocked"] is True
    assert all(f["verdict"] != Verdict.ALLOW.value for f in payload["files"][0]["findings"])
