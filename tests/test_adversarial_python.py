"""Hostile Python. Written to break the tool, not to demonstrate it.

Every test here asserts bleurs stays silent on code that is unusual but
correct. The bar is not "does this look like a hallucination" -- it is
"can we prove it is one". Anything we cannot prove must pass through.
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
            """
            class User:
                def __init__(self, email):
                    self.email = email

                def rename(self, name): ...
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def check(project):
    engine = Engine(Config(project_root=project, network=False))

    def _check(source, name="main.py"):
        return engine.check_source(textwrap.dedent(source), project / name)

    return _check


def silent(report):
    return [f"{f.reference.display}: {f.message}" for f in report.blocks] == []


# -- attributes attached after construction ------------------------------


def test_attribute_assigned_on_the_instance_then_read(check):
    # Extremely common, and fatal if we get it wrong.
    assert silent(
        check(
            """
            from app.models import User
            u = User("a")
            u.cache = {}
            print(u.cache)
            """
        )
    )


def test_attribute_monkeypatched_onto_the_class(check):
    assert silent(
        check(
            """
            from app.models import User
            User.extra = 1
            u = User("a")
            print(u.extra)
            """
        )
    )


def test_setattr_on_the_instance(check):
    assert silent(
        check(
            """
            from app.models import User
            u = User("a")
            setattr(u, "dynamic", 1)
            print(u.dynamic)
            """
        )
    )


def test_attribute_set_in_a_helper_function(check):
    assert silent(
        check(
            """
            from app.models import User

            def decorate(user):
                user.decorated = True

            u = User("a")
            decorate(u)
            print(u.decorated)
            """
        )
    )


def test_class_reassigned_after_definition(check):
    assert silent(
        check(
            """
            class Thing:
                def go(self): ...

            Thing = SomethingElse
            t = Thing()
            print(t.whatever)
            """
        )
    )


def test_attribute_set_via_class_method_on_cls(check):
    assert silent(
        check(
            """
            class Thing:
                @classmethod
                def prepare(cls):
                    cls.prepared = True

            t = Thing()
            print(t.prepared)
            """
        )
    )


# -- class construction torture ------------------------------------------


def test_conditional_class_definition(check):
    assert silent(
        check(
            """
            import sys

            if sys.version_info >= (3, 11):
                class Impl:
                    def new_way(self): ...
            else:
                class Impl:
                    def old_way(self): ...

            i = Impl()
            i.old_way()
            i.new_way()
            """
        )
    )


def test_class_defined_inside_a_function(check):
    assert silent(
        check(
            """
            def make():
                class Local:
                    def go(self): ...
                return Local()

            x = make()
            print(x.anything)
            """
        )
    )


def test_nested_class(check):
    assert silent(
        check(
            """
            class Outer:
                class Inner:
                    def go(self): ...

            o = Outer()
            print(o.Inner)
            """
        )
    )


def test_diamond_inheritance(check):
    assert silent(
        check(
            """
            class A:
                def a(self): ...

            class B(A):
                def b(self): ...

            class C(A):
                def c(self): ...

            class D(B, C):
                pass

            d = D()
            d.a(); d.b(); d.c()
            """
        )
    )


def test_self_referential_base_via_alias(check):
    # Must terminate, not recurse forever.
    assert silent(
        check(
            """
            class A:
                def go(self): ...

            Alias = A

            class B(Alias):
                pass

            b = B()
            b.go()
            """
        )
    )


def test_metaclass_opens_the_surface(check):
    assert silent(
        check(
            """
            class Meta(type):
                def __getattr__(cls, name): ...

            class Thing(metaclass=Meta):
                def known(self): ...

            t = Thing()
            print(t.conjured)
            """
        )
    )


def test_slots_class(check):
    assert silent(
        check(
            """
            class Point:
                __slots__ = ("x", "y")

                def __init__(self, x, y):
                    self.x = x
                    self.y = y

            p = Point(1, 2)
            print(p.x, p.y)
            """
        )
    )


def test_property_assigned_by_call(check):
    assert silent(
        check(
            """
            class Thing:
                def _get(self): ...
                value = property(_get)

            t = Thing()
            print(t.value)
            """
        )
    )


def test_namedtuple_and_typeddict(check):
    assert silent(
        check(
            """
            from typing import NamedTuple, TypedDict

            class Point(NamedTuple):
                x: int
                y: int

            class Cfg(TypedDict):
                name: str

            p = Point(1, 2)
            print(p.x, p.y, p._replace(x=3))
            """
        )
    )


def test_enum_members(check):
    assert silent(
        check(
            """
            from enum import Enum

            class Color(Enum):
                RED = 1
                BLUE = 2

            c = Color.RED
            print(c.value, c.name)
            """
        )
    )


def test_dataclass_with_field_factory(check):
    assert silent(
        check(
            """
            from dataclasses import dataclass, field

            @dataclass
            class Cfg:
                items: list = field(default_factory=list)

            c = Cfg()
            print(c.items)
            """
        )
    )


def test_generic_class(check):
    assert silent(
        check(
            """
            from typing import Generic, TypeVar

            T = TypeVar("T")

            class Box(Generic[T]):
                def __init__(self, item: T):
                    self.item = item

            b = Box(1)
            print(b.item)
            """
        )
    )


# -- modern syntax -------------------------------------------------------


def test_match_statement_with_class_patterns(check):
    assert silent(
        check(
            """
            import json

            def handle(value):
                match value:
                    case {"kind": k, **rest}:
                        return json.dumps(rest), k
                    case [first, *others]:
                        return first, others
                    case str() as text:
                        return json.loads(text)
                    case _:
                        return None
            """
        )
    )


def test_walrus_in_comprehension(check):
    assert silent(
        check(
            """
            import json

            def go(rows):
                return [parsed for row in rows if (parsed := json.loads(row))]
            """
        )
    )


def test_pep604_unions_and_positional_only(check):
    assert silent(
        check(
            """
            import json

            def go(raw: str | None, /, *, strict: bool = False) -> dict | None:
                return json.loads(raw) if raw else None
            """
        )
    )


def test_except_star(check):
    assert silent(
        check(
            """
            import json

            def go(raw):
                try:
                    return json.loads(raw)
                except* ValueError:
                    return None
            """
        )
    )


def test_async_everything(check):
    assert silent(
        check(
            """
            import asyncio
            import json

            async def go(items):
                async with asyncio.Lock():
                    async for item in items:
                        yield json.dumps(item)
            """
        )
    )


def test_decorator_expressions(check):
    assert silent(
        check(
            """
            import functools

            registry = {}

            @functools.wraps(print)
            @registry.setdefault("k", lambda f: f)
            def go(): ...
            """
        )
    )


def test_unicode_identifiers(check):
    assert silent(
        check(
            """
            import json

            переменная = json.dumps({})
            def función(año):
                return año
            """
        )
    )


def test_nested_fstrings(check):
    assert silent(
        check(
            """
            import json

            def go(d):
                return f"{json.dumps({k: f'{v!r}' for k, v in d.items()})}"
            """
        )
    )


# -- structural robustness -----------------------------------------------


def test_deeply_nested_expression_does_not_blow_the_stack(check):
    depth = 90
    source = "import json\nx = " + "[" * depth + "1" + "]" * depth + "\n"
    report = check(source)
    assert report.blocks == []


def test_very_long_attribute_chain_terminates(check):
    # `json.a` genuinely does not exist, so one block is the correct answer.
    # What is under test is that a 60-deep chain resolves at all rather than
    # recursing, and reports once rather than sixty times.
    report = check("import json\nx = json." + "a.".join([""] * 60) + "b\n")
    assert len(report.blocks) == 1


def test_many_imports(check):
    body = "\n".join(f"import json as j{i}" for i in range(300))
    assert silent(check(body))


def test_file_with_crlf_line_endings(check):
    report = check("import json\r\nx = json.dumps({})\r\n")
    assert report.parse_error is None
    assert report.blocks == []


def test_file_with_bom(check):
    report = check("﻿import json\nx = json.dumps({})\n")
    assert report.blocks == []


def test_empty_file(check):
    assert check("").blocks == []


def test_only_comments(check):
    assert check("# nothing here\n").blocks == []


def test_null_bytes_do_not_crash(check):
    report = check("import json\nx = 1\x00\n")
    assert report.blocks == []


# -- import torture ------------------------------------------------------


def test_dotted_alias_attribute(check):
    assert silent(
        check(
            """
            import os.path as osp
            x = osp.join("a", "b")
            """
        )
    )


def test_import_inside_function(check):
    assert silent(
        check(
            """
            def go():
                import json
                return json.dumps({})
            """
        )
    )


def test_relative_import_beyond_top_level_abstains(check):
    assert silent(check("from ......nowhere import thing\n", name="app/models.py"))


def test_future_annotations_defers_everything(check):
    assert silent(
        check(
            """
            from __future__ import annotations

            import warnings

            def go(x: warnings.NotARealType) -> warnings.AlsoNotReal:
                return x
            """
        )
    )


def test_conditional_import_of_the_same_name(check):
    assert silent(
        check(
            """
            try:
                import ujson as jsonlib
            except ImportError:
                import json as jsonlib

            x = jsonlib.dumps({})
            """
        )
    )


def test_module_reexported_under_a_new_name(check):
    assert silent(
        check(
            """
            import json
            codec = json
            print(codec.anything_at_all)
            """
        )
    )


def test_del_then_reimport(check):
    assert silent(
        check(
            """
            import json
            del json
            import json
            print(json.dumps({}))
            """
        )
    )
