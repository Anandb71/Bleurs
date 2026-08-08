"""TypeScript and JavaScript.

npm is the harder ecosystem to get right. Its namespace is flat and enormous,
path aliases look exactly like package specifiers, and the module resolution
algorithm has more fallbacks than Python's. Most of the tests below are
therefore about *not* firing.

The registry is stubbed throughout, so nothing here touches the network.
"""

from __future__ import annotations

import json

import pytest

from bleurs.analyze.typescript import available

pytestmark = pytest.mark.skipif(
    not available(), reason="needs bleurs[typescript]"
)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18", "left-pad": "^1"}}),
        encoding="utf-8",
    )
    (tmp_path / "node_modules" / "react").mkdir(parents=True)
    (tmp_path / "node_modules" / "@scope" / "real").mkdir(parents=True)

    src = tmp_path / "src"
    src.mkdir()
    (src / "utils.ts").write_text(
        "export function helper() {}\n"
        "export const CONST = 1;\n"
        "export class Widget {}\n"
        "export interface Opts {}\n"
        "export type Id = string;\n",
        encoding="utf-8",
    )
    (src / "barrel.ts").write_text('export * from "./utils";\n', encoding="utf-8")
    (src / "nested").mkdir()
    (src / "nested" / "index.ts").write_text("export const deep = 1;\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def check(project, make_engine):
    engine = make_engine(npm={"react", "zod", "left-pad"}, project_root=project)

    def _check(code, name="src/main.ts"):
        return engine.check_source(code, project / name)

    return _check


# -- packages ------------------------------------------------------------


def test_installed_package_passes(check):
    assert check('import React from "react";').blocks == []


def test_scoped_installed_package_passes(check):
    assert check('import x from "@scope/real";').blocks == []


@pytest.mark.parametrize(
    "code",
    [
        'import fs from "node:fs";',
        'import path from "path";',
        'const fs = require("fs");',
        'import test from "node:test";',
    ],
)
def test_node_builtins_pass(check, code):
    assert check(code).blocks == []


def test_declared_but_uninstalled_only_warns(check):
    report = check('import lp from "left-pad";')
    assert report.blocks == []
    assert "package.json" in report.warnings[0].message


def test_real_but_uninstalled_only_warns(check):
    report = check('import { z } from "zod";')
    assert report.blocks == []
    assert "npm install zod" in report.warnings[0].suggestion


def test_invented_package_is_blocked(check):
    report = check('import z from "react-hooks-utils-toolkit";')
    assert len(report.blocks) == 1
    assert report.blocks[0].resolver == "npm"


def test_invented_scoped_package_is_blocked(check):
    report = check('import x from "@acme/intl-format-helpers";')
    assert len(report.blocks) == 1


def test_dynamic_import_of_an_invented_package_is_blocked(check):
    assert len(check('const m = await import("totally-fake-xyzzy");').blocks) == 1


def test_subpath_is_judged_by_its_package_root(check):
    # `lodash/fp` is a subpath of a package. Evaluating subpaths means
    # evaluating exports maps, which is tsc's job.
    assert check('import fp from "react/jsx-runtime";').blocks == []


# -- project files -------------------------------------------------------


def test_relative_import_resolves_through_extensions(check):
    assert check('import { helper } from "./utils";').blocks == []


def test_relative_import_resolves_through_index(check):
    assert check('import { deep } from "./nested";').blocks == []


def test_js_specifier_resolves_to_the_ts_source(check):
    # ESM-style TypeScript imports its own source with a .js extension.
    assert check('import { helper } from "./utils.js";').blocks == []


def test_missing_relative_file_is_blocked(check):
    report = check('import { x } from "./nope";')
    assert len(report.blocks) == 1
    assert report.blocks[0].resolver == "node"


def test_missing_file_reports_once_not_per_binding(check):
    # One problem, one line. Repeating it per named binding turns a single
    # mistake into a wall of noise.
    assert len(check('import { a, b, c } from "./nope";').blocks) == 1


def test_invented_named_export_is_blocked(check):
    report = check('import { helperr } from "./utils";')
    assert len(report.blocks) == 1
    assert report.blocks[0].suggestion == "helper"


@pytest.mark.parametrize("name", ["helper", "CONST", "Widget", "Opts", "Id"])
def test_every_export_form_is_recognized(check, name):
    assert check(f'import {{ {name} }} from "./utils";').blocks == []


def test_namespace_member_is_checked(check):
    report = check('import * as u from "./utils";\nu.nope();')
    assert len(report.blocks) == 1


def test_valid_namespace_member_passes(check):
    assert check('import * as u from "./utils";\nu.helper();').blocks == []


def test_shadowed_namespace_abstains(check):
    assert check(
        'import * as u from "./utils";\nfunction f(u: any) { return u.anything; }'
    ).blocks == []


# -- abstaining ----------------------------------------------------------


def test_star_reexport_opens_the_surface(check):
    assert check('import { anything } from "./barrel";').blocks == []


def test_type_only_import_members_abstain(check):
    # Types resolve against declarations this front-end does not read.
    assert check('import type { Nope } from "./utils";').blocks == []


def test_inline_type_specifier_abstains(check):
    assert check('import { type Nope } from "./utils";').blocks == []


def test_package_members_abstain(check):
    # Answering this needs .d.ts resolution, which is tsc's job.
    assert check('import { nonexistentExport } from "react";').blocks == []


def test_tsconfig_path_alias_abstains(project, make_engine):
    (project / "tsconfig.json").write_text(
        '{\n  // aliases\n  "compilerOptions": { "paths": { "@/*": ["./src/*"] } },\n}',
        encoding="utf-8",
    )
    engine = make_engine(npm=set(), project_root=project)
    report = engine.check_source(
        'import { Button } from "@/components/Button";', project / "src" / "main.ts"
    )
    assert report.blocks == []


def test_base_url_makes_bare_specifiers_ambiguous(project, make_engine):
    (project / "tsconfig.json").write_text(
        '{"compilerOptions": {"baseUrl": "./src"}}', encoding="utf-8"
    )
    engine = make_engine(npm=set(), project_root=project)
    report = engine.check_source(
        'import { helper } from "utils";', project / "src" / "main.ts"
    )
    assert report.blocks == []


def test_offline_npm_is_not_evidence(project, make_engine):
    engine = make_engine(npm=set(), offline=True, project_root=project)
    report = engine.check_source(
        'import x from "maybe-real-maybe-not";', project / "src" / "main.ts"
    )
    assert report.blocks == []


def test_unparseable_file_is_skipped(check):
    report = check("function broken( { { {")
    assert report.parse_error is not None
    assert report.blocks == []


def test_tsx_is_parsed_with_the_jsx_grammar(check):
    report = check(
        'import React from "react";\n'
        "export const A = () => <div className=\"x\">hi</div>;\n",
        name="src/App.tsx",
    )
    assert report.parse_error is None
    assert report.blocks == []
