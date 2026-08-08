"""Surface projection.

The property under test is not "it produces output" but "it produces output a
caller can act on": real signatures, real defaults, and nothing invented. A
projection that quietly drops a keyword-only argument would be worse than no
projection at all, because the agent would trust it.
"""

from __future__ import annotations

from pathlib import Path

from bleurs.surface import (
    estimate_tokens,
    installed_surface,
    local_surface,
    render,
)

SAMPLE = '''
"""Module docstring."""

CONSTANT = 3
TYPED: int = 4
_PRIVATE = 5


def public(a, b=2, *args, key=None, **kwargs) -> str:
    """Does a thing."""


async def fetches(url: str) -> bytes:
    """Gets bytes."""


def _helper():
    pass


class Thing(Base):
    """A thing."""

    def method(self, x: int = 1) -> None:
        """Method doc."""

    def _internal(self):
        pass


class _Hidden:
    pass
'''


def project(tmp_path, source=SAMPLE, **kwargs):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return local_surface(path, **kwargs)


def names(surface):
    return [m.name for m in surface.members]


# -- local ---------------------------------------------------------------


def test_public_names_are_projected(tmp_path):
    surface = project(tmp_path)
    assert surface.ok
    assert "public" in names(surface)
    assert "Thing" in names(surface)
    assert "CONSTANT" in names(surface)


def test_private_names_are_omitted_by_default(tmp_path):
    surface = project(tmp_path)
    assert "_helper" not in names(surface)
    assert "_PRIVATE" not in names(surface)
    assert "_Hidden" not in names(surface)


def test_private_names_are_available_on_request(tmp_path):
    # An agent editing the module needs the internals it is about to touch.
    surface = project(tmp_path, private=True)
    assert "_helper" in names(surface)
    assert "_Hidden" in names(surface)


def test_signature_is_reproduced_exactly(tmp_path):
    surface = project(tmp_path)
    member = next(m for m in surface.members if m.name == "public")
    assert member.signature == "(a, b=2, *args, key=None, **kwargs) -> str"


def test_async_functions_are_marked(tmp_path):
    surface = project(tmp_path)
    member = next(m for m in surface.members if m.name == "fetches")
    assert member.kind == "async function"
    assert member.signature == "(url: str) -> bytes"


def test_methods_are_nested_under_their_class(tmp_path):
    surface = project(tmp_path)
    thing = next(m for m in surface.members if m.name == "Thing")
    assert [c.name for c in thing.members] == ["method"]
    assert thing.members[0].signature == "(self, x: int=1) -> None"


def test_docstring_first_line_only(tmp_path):
    surface = project(tmp_path)
    member = next(m for m in surface.members if m.name == "public")
    assert member.summary == "Does a thing."


def test_annotations_are_preserved_on_values(tmp_path):
    surface = project(tmp_path)
    member = next(m for m in surface.members if m.name == "TYPED")
    assert member.signature == ": int"


def test_unparseable_file_reports_rather_than_raises(tmp_path):
    surface = project(tmp_path, source="def broken(:\n")
    assert not surface.ok
    assert surface.error


def test_module_name_can_be_overridden(tmp_path):
    surface = project(tmp_path, module_name="pkg.sample")
    assert surface.dotted == "pkg.sample"


# -- installed -----------------------------------------------------------


def test_installed_module_is_projected():
    surface = installed_surface("json")
    assert surface.ok and surface.kind == "module"
    assert "loads" in names(surface)
    assert "loads_safe" not in names(surface)


def test_nested_class_is_resolved():
    # `datetime.datetime` is a class inside a module -- the split point between
    # importable prefix and attribute path is not knowable from the string.
    surface = installed_surface("datetime.datetime")
    assert surface.ok and surface.kind == "class"
    assert "fromisoformat" in names(surface)


def test_submodule_is_resolved():
    surface = installed_surface("os.path")
    assert surface.ok
    assert "join" in names(surface)


def test_inherited_builtin_noise_is_filtered():
    # Every exception would otherwise list add_note and with_traceback.
    surface = installed_surface("json.JSONDecodeError")
    assert surface.ok
    assert "with_traceback" not in names(surface)
    assert "add_note" not in names(surface)


def test_missing_target_reports_an_error():
    surface = installed_surface("definitely_not_a_real_module_xyz")
    assert not surface.ok
    assert surface.error


def test_signatures_carry_no_memory_addresses():
    # Default reprs like <object at 0x7f...> change every run, which would make
    # the projection non-deterministic and uncacheable for no benefit.
    surface = installed_surface("json")
    rendered = render(surface)
    assert " at 0x" not in rendered


# -- rendering -----------------------------------------------------------


def test_render_is_smaller_than_the_source(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text(SAMPLE, encoding="utf-8")
    rendered = render(local_surface(path))
    assert estimate_tokens(rendered) < estimate_tokens(SAMPLE)


def test_limit_truncates_and_says_so():
    surface = installed_surface("os.path")
    rendered = render(surface, limit=3)
    assert "more (bleurs surface os.path)" in rendered


def test_summaries_can_be_suppressed(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text(SAMPLE, encoding="utf-8")
    rendered = render(local_surface(path), summaries=False)
    assert "Does a thing." not in rendered
    assert "public(" in rendered


def test_unavailable_surface_renders_a_reason():
    rendered = render(installed_surface("definitely_not_a_real_module_xyz"))
    assert "unavailable" in rendered
