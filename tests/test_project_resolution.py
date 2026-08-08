"""Attributes on project objects.

This is where hallucinations in a real repository actually live. An agent
rarely invents a stdlib function; it invents a method on the class you just
showed it, and every tool that only checks imports sails straight past that.

The abstain cases below matter more than the detection ones. Resolving types
without running the program means guessing, and the moment this starts guessing
it starts blaming correct code.
"""

from __future__ import annotations

import textwrap

import pytest

from bleurs import Config, Engine


@pytest.fixture
def project(tmp_path):
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text(
        textwrap.dedent(
            '''
            class Base:
                def save(self): ...

            class User(Base):
                """A user."""
                role = "member"

                def __init__(self, email):
                    self.email = email
                    self._token = None

                def rename(self, name): ...

            class Wild(SomeThirdPartyBase):
                def go(self): ...

            class Magic:
                def __getattr__(self, name): ...

            @mystery_decorator
            class Decorated:
                def known(self): ...
            '''
        ),
        encoding="utf-8",
    )
    (pkg / "utils.py").write_text(
        "def helper():\n    ...\n\n\nCONST = 1\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def check(project):
    engine = Engine(Config(project_root=project, network=False))

    def _check(source):
        return engine.check_source(textwrap.dedent(source), project / "main.py")

    return _check


# -- detection -----------------------------------------------------------


def test_invented_attribute_on_a_local_class_is_blocked(check):
    report = check(
        """
        from app.models import User
        u = User("a@b.c")
        print(u.emial)
        """
    )
    assert len(report.blocks) == 1
    assert report.blocks[0].resolver == "project"
    assert report.blocks[0].suggestion == "email"


def test_attributes_set_in_init_are_found(check):
    report = check(
        """
        from app.models import User
        u = User("a@b.c")
        print(u.email, u._token)
        """
    )
    assert report.blocks == []


def test_class_level_attributes_are_found(check):
    report = check(
        """
        from app.models import User
        u = User("a@b.c")
        print(u.role)
        """
    )
    assert report.blocks == []


def test_inherited_attributes_are_found(check):
    report = check(
        """
        from app.models import User
        u = User("a@b.c")
        u.save()
        """
    )
    assert report.blocks == []


def test_self_attribute_is_checked_against_the_enclosing_class(check):
    report = check(
        """
        from app.models import User

        class Admin(User):
            def go(self):
                return self.notathing
        """
    )
    assert len(report.blocks) == 1
    assert "Admin" in report.blocks[0].message


def test_a_class_defined_in_the_edit_itself_resolves(check):
    # The file may not exist on disk yet. Its proposed content is the authority
    # on its own classes.
    report = check(
        """
        class Thing:
            def __init__(self):
                self.cache = {}

            def go(self):
                return self.cahce
        """
    )
    assert len(report.blocks) == 1
    assert "cahce" in report.blocks[0].message


def test_missing_member_of_a_local_module_is_blocked(check):
    report = check(
        """
        from app import utils
        print(utils.no_such_helper)
        """
    )
    assert len(report.blocks) == 1
    assert "app.utils" in report.blocks[0].message


def test_present_members_of_a_local_module_pass(check):
    report = check(
        """
        from app import utils
        print(utils.helper, utils.CONST)
        """
    )
    assert report.blocks == []


# -- abstaining ----------------------------------------------------------


def test_reassigned_variable_abstains(check):
    # After a second assignment we no longer know what the name holds, and a
    # wrong type is worse than no type.
    report = check(
        """
        from app.models import User
        u = User("a@b.c")
        u = something_else()
        print(u.emial)
        """
    )
    assert report.blocks == []


def test_factory_function_result_abstains(check):
    report = check(
        """
        from app.models import User

        def make():
            return User("a@b.c")

        u = make()
        print(u.emial)
        """
    )
    assert report.blocks == []


def test_unresolvable_base_class_opens_the_surface(check):
    # `Wild` inherits from something outside the project. Anything could be on
    # it, so absence proves nothing.
    report = check(
        """
        from app.models import Wild
        w = Wild()
        print(w.anything_at_all)
        """
    )
    assert report.blocks == []


def test_getattr_on_a_local_class_opens_the_surface(check):
    report = check(
        """
        from app.models import Magic
        m = Magic()
        print(m.conjured)
        """
    )
    assert report.blocks == []


def test_unknown_decorator_opens_the_surface(check):
    # A decorator can return a completely different object.
    report = check(
        """
        from app.models import Decorated
        d = Decorated()
        print(d.who_knows)
        """
    )
    assert report.blocks == []


def test_dataclass_decorator_keeps_the_surface_closed(tmp_path):
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "dto.py").write_text(
        "from dataclasses import dataclass\n\n\n"
        "@dataclass\nclass Point:\n    x: int\n    y: int\n",
        encoding="utf-8",
    )
    engine = Engine(Config(project_root=tmp_path, network=False))
    report = engine.check_source(
        "from app.dto import Point\np = Point(1, 2)\nprint(p.z)\n",
        tmp_path / "main.py",
    )
    assert len(report.blocks) == 1


def test_assigning_a_new_attribute_is_not_a_claim(check):
    # `u.brand_new = 1` creates the attribute. It does not assert one exists.
    report = check(
        """
        from app.models import User
        u = User("a@b.c")
        u.brand_new = 1
        """
    )
    assert report.blocks == []


def test_attribute_on_a_function_abstains(check):
    # Decorators and functools.wraps attach arbitrary attributes to functions.
    report = check(
        """
        from app.utils import helper
        print(helper.cache_clear)
        """
    )
    assert report.blocks == []


def test_loop_variable_abstains(check):
    report = check(
        """
        from app.models import User
        for u in load():
            print(u.emial)
        """
    )
    assert report.blocks == []


def test_parameter_abstains(check):
    report = check(
        """
        def handle(u):
            return u.emial
        """
    )
    assert report.blocks == []


def test_deep_chains_only_claim_the_first_step(check):
    # `u.email` is checkable; what `email` then holds is not.
    report = check(
        """
        from app.models import User
        u = User("a@b.c")
        print(u.email.anything.at.all)
        """
    )
    assert report.blocks == []


def test_wildcard_import_in_the_target_module_abstains(tmp_path):
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "wide.py").write_text("from os.path import *\n", encoding="utf-8")
    engine = Engine(Config(project_root=tmp_path, network=False))
    report = engine.check_source(
        "from app import wide\nprint(wide.anything)\n", tmp_path / "main.py"
    )
    assert report.blocks == []
