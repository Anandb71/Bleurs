"""One test per bug found by the brutal-testing campaign.

Every case below is a false positive that reached real, working, installed code
before it was caught. They are kept together, with the source that produced
them named, because this list is the honest record of what this design gets
wrong when nobody is looking.
"""

from __future__ import annotations

import textwrap

import pytest

from bleurs import Config, Engine


@pytest.fixture
def make(tmp_path):
    def _make(files: dict[str, str], target: str = "main.py"):
        for name, body in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(body), encoding="utf-8")
        engine = Engine(Config(project_root=tmp_path, network=False))

        def check(source, name=target):
            return engine.check_source(textwrap.dedent(source), tmp_path / name)

        return check

    return _make


def test_first_parameter_of_a_staticmethod_is_not_self(make):
    """_pytest/cacheprovider.py -- `Cache has no attribute 'getini'`.

    The first parameter of *any* method was being treated as the instance
    receiver, so every argument to every @staticmethod was claimed to be an
    instance of the enclosing class.
    """
    check = make({})
    report = check(
        """
        class Cache:
            @staticmethod
            def dir_from_config(config, *, ispytest: bool = False):
                return config.getini("cache_dir")
        """
    )
    assert report.blocks == []


def test_classmethod_receiver_still_resolves(make):
    # The staticmethod fix must not disable classmethods, whose first
    # parameter really is the class.
    check = make({})
    report = check(
        """
        class Thing:
            known = 1

            @classmethod
            def go(cls):
                return cls.definitely_not_there
        """
    )
    assert len(report.blocks) == 1


def test_project_module_shadowing_a_stdlib_name_abstains(make):
    """aiohttp, pydantic, setuptools and _pytest all ship a `warnings.py`.

    Which module `import warnings` resolves to depends on sys.path order at
    runtime, so there is no answer to give.
    """
    check = make({"warnings.py": "def local_only(): ...\n"})
    report = check(
        """
        import warnings
        warnings.warn("careful")
        """
    )
    assert report.blocks == []


def test_object_protocol_dunders_exist_on_every_class(make):
    """_pytest/reports.py -- `self.__dict__.update(...)`."""
    check = make({})
    report = check(
        """
        class Report:
            def load(self, data):
                self.__dict__.update(data)
                return self.__class__.__name__
        """
    )
    assert report.blocks == []


def test_names_defined_inside_conditionals_are_found(make):
    """aiohttp/_websocket/helpers.py -- `websocket_mask` assigned under a try.

    Only top-level statements were being read, so anything defined inside
    `if`/`try` at module level looked undefined.
    """
    check = make(
        {
            "helpers.py": """
                import sys

                if sys.version_info >= (3, 8):
                    def websocket_mask(a, b): ...
                else:
                    try:
                        from .fast import websocket_mask
                    except ImportError:
                        def websocket_mask(a, b): ...
            """
        }
    )
    assert check("from helpers import websocket_mask\n").blocks == []


def test_methods_defined_inside_conditionals_are_found(make):
    """aiohttp/client.py -- `def request` under `if ... and TYPE_CHECKING:`."""
    check = make({})
    report = check(
        """
        import sys

        class Session:
            if sys.version_info >= (3, 11):
                def request(self, url): ...

            def go(self):
                return self.request("/")
        """
    )
    assert report.blocks == []


def test_a_class_defined_twice_abstains(make):
    # One definition per branch of a version test. Which one exists depends on
    # the interpreter, so neither surface can be claimed.
    check = make({})
    report = check(
        """
        import sys

        if sys.version_info >= (3, 11):
            class Impl:
                def new_way(self): ...
        else:
            class Impl:
                def old_way(self): ...

        i = Impl()
        i.new_way()
        i.old_way()
        """
    )
    assert report.blocks == []


def test_mixin_calling_an_attribute_it_does_not_define(make):
    """aiohttp/streams.py -- AsyncStreamReaderMixin.readline.

    `self` is an instance of whatever subclass was constructed, not of the
    class the method is written in, so a mixin referring to attributes it does
    not define is correct code.
    """
    check = make(
        {
            "impl.py": """
                from base import ReaderMixin

                class Reader(ReaderMixin):
                    def readline(self): ...
            """,
            "base.py": """
                class ReaderMixin:
                    def iter_lines(self):
                        return self.readline()
            """,
        }
    )
    assert check("from base import ReaderMixin\n").blocks == []


def test_a_leaf_class_typo_is_still_caught(make):
    # The mixin rule must not switch self-checking off wholesale. Nothing
    # inherits from this class, so the typo remains provable.
    check = make({})
    report = check(
        """
        class Service:
            def __init__(self):
                self.repository = None

            def go(self):
                return self.reposiory
        """
    )
    assert len(report.blocks) == 1
    assert report.blocks[0].suggestion == "repository"


def test_nested_class_is_an_attribute_of_the_outer_one(make):
    check = make({})
    report = check(
        """
        class Outer:
            class Inner:
                def go(self): ...

        o = Outer()
        print(o.Inner)
        """
    )
    assert report.blocks == []


def test_attribute_attached_to_an_imported_class_opens_it(make):
    check = make({"models.py": "class User:\n    def __init__(self):\n        self.email = None\n"})
    report = check(
        """
        from models import User
        User.extra = 1
        u = User()
        print(u.extra)
        """
    )
    assert report.blocks == []


def test_an_object_passed_elsewhere_may_be_mutated(make):
    check = make({"models.py": "class User:\n    def __init__(self):\n        self.email = None\n"})
    report = check(
        """
        from models import User

        def decorate(user):
            user.decorated = True

        u = User()
        decorate(u)
        print(u.decorated)
        """
    )
    assert report.blocks == []


def test_an_object_that_never_escapes_is_still_checked(make):
    # The escape rule must not switch instance checking off wholesale.
    check = make({"models.py": "class User:\n    def __init__(self):\n        self.email = None\n"})
    report = check(
        """
        from models import User
        u = User()
        print(u.emial)
        """
    )
    assert len(report.blocks) == 1
