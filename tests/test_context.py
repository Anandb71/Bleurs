"""Working sets.

The properties that matter are the boring ones: the closure is complete, the
budget is respected, and the ordering puts the things you cannot do without
ahead of the things you can ask for by name.
"""

from __future__ import annotations

import textwrap

import pytest

from bleurs.context import DEFAULT_BUDGET, build


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
    (pkg / "store.py").write_text(
        "from app.models import User\n\n\ndef save(user: User): ...\n",
        encoding="utf-8",
    )
    (pkg / "service.py").write_text(
        textwrap.dedent(
            """
            import json

            from .store import save
            from app.models import User


            def register(email):
                user = User(email)
                save(user)
                return json.dumps({"email": user.email})


            def _internal(): ...
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


def keys(working):
    return [s.key for s in working.sections]


def test_seed_surface_comes_first(project):
    working = build([project / "app" / "service.py"], project_root=project)
    assert working.sections[0].key == "service.py"


def test_seed_includes_private_names(project):
    # You are editing this file, so its internals are exactly what you need.
    working = build([project / "app" / "service.py"], project_root=project)
    assert "_internal" in working.sections[0].body


def test_direct_project_imports_are_gathered(project):
    working = build([project / "app" / "service.py"], project_root=project)
    assert "app.models" in keys(working)
    assert "app.store" in keys(working)


def test_relative_imports_are_anchored(project):
    # `from .store import save` names app.store. Dropping relative imports would
    # throw away most of a real Python project.
    working = build([project / "app" / "service.py"], project_root=project)
    assert "app.store" in keys(working)


def test_imported_class_surface_is_present(project):
    working = build([project / "app" / "service.py"], project_root=project)
    models = next(s for s in working.sections if s.key == "app.models")
    assert "rename" in models.body
    assert "email" in models.body


def test_third_party_and_stdlib_are_included(project):
    working = build([project / "app" / "service.py"], project_root=project)
    assert "json" in keys(working)


def test_introspection_can_be_disabled(project):
    working = build(
        [project / "app" / "service.py"], project_root=project, introspect=False
    )
    assert "json" not in keys(working)
    assert "app.models" in keys(working)


def test_depth_zero_stops_at_direct_imports(project):
    shallow = build([project / "app" / "service.py"], project_root=project, depth=0)
    # store.py imports models, but at depth 0 we never open store.py itself.
    assert "app.store" in keys(shallow)


def test_budget_is_respected(project):
    working = build([project / "app" / "service.py"], project_root=project, budget=60)
    assert working.used <= 60


def test_omissions_are_reported_not_hidden(project):
    working = build([project / "app" / "service.py"], project_root=project, budget=60)
    assert working.omitted
    assert "omitted for budget" in working.render()


def test_render_states_the_cost(project):
    working = build([project / "app" / "service.py"], project_root=project)
    header = working.render().splitlines()[1]
    assert "tokens" in header and str(working.used) in header


def test_a_working_set_is_cheaper_than_the_files_it_replaces(project):
    from bleurs.surface import estimate_tokens

    seed = project / "app" / "service.py"
    working = build([seed], project_root=project, introspect=False)
    raw = sum(
        estimate_tokens(p.read_text(encoding="utf-8"))
        for p in (seed, project / "app" / "models.py", project / "app" / "store.py")
    )
    assert working.used < raw


def test_multiple_seeds_are_merged_without_duplicates(project):
    working = build(
        [project / "app" / "service.py", project / "app" / "store.py"],
        project_root=project,
    )
    assert len(keys(working)) == len(set(keys(working)))
    assert "app.models" in keys(working)


def test_unreadable_seed_does_not_crash(project):
    working = build([project / "app" / "nope.py"], project_root=project)
    assert working.sections == [] or working.used >= 0


def test_unparseable_seed_yields_no_imports(project):
    broken = project / "app" / "broken.py"
    broken.write_text("def broken( {{{\n", encoding="utf-8")
    working = build([broken], project_root=project)
    assert "app.models" not in keys(working)


def test_works_without_a_project_root(project):
    # Degrades to the seed plus whatever is installed, rather than failing.
    working = build([project / "app" / "service.py"])
    assert working.sections
    assert working.budget == DEFAULT_BUDGET
