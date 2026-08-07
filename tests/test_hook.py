"""The hook contract.

Two properties are load-bearing. It must reconstruct the *post-edit* file
rather than judging a fragment, and it must fail open on anything it does not
understand -- a firewall that fails closed on its own bugs gets uninstalled.
"""

from __future__ import annotations

import io
import json

import pytest

from bleurs import hook


def run_hook(monkeypatch, payload, argv=None):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return hook.run(argv or [])


def test_clean_write_is_allowed(monkeypatch, tmp_path):
    payload = {
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(tmp_path / "ok.py"),
            "content": "import json\nx = json.dumps({})\n",
        },
    }
    assert run_hook(monkeypatch, payload) == hook.ALLOW


def test_hallucinated_write_is_denied(monkeypatch, tmp_path, capsys):
    payload = {
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(tmp_path / "bad.py"),
            "content": "import json\nx = json.loads_safe('{}')\n",
        },
    }
    assert run_hook(monkeypatch, payload) == hook.DENY
    assert "loads_safe" in capsys.readouterr().err


def test_edit_is_judged_after_application(monkeypatch, tmp_path):
    # The import lives in the existing file; the bad call arrives in the edit.
    # Neither half is a hallucination on its own.
    target = tmp_path / "app.py"
    target.write_text("import json\n\n\ndef go(raw):\n    pass\n", encoding="utf-8")

    payload = {
        "tool_name": "Edit",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(target),
            "old_string": "    pass",
            "new_string": "    return json.loads_safe(raw)",
        },
    }
    assert run_hook(monkeypatch, payload) == hook.DENY


def test_multiedit_applies_every_edit_in_order(monkeypatch, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("PLACEHOLDER_IMPORT\n\nPLACEHOLDER_BODY\n", encoding="utf-8")

    payload = {
        "tool_name": "MultiEdit",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(target),
            "edits": [
                {"old_string": "PLACEHOLDER_IMPORT", "new_string": "import base64"},
                {
                    "old_string": "PLACEHOLDER_BODY",
                    "new_string": "x = base64.encode_string('a')",
                },
            ],
        },
    }
    assert run_hook(monkeypatch, payload) == hook.DENY


def test_json_protocol_emits_a_deny_decision(monkeypatch, tmp_path, capsys):
    payload = {
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(tmp_path / "bad.py"),
            "content": "import json\nx = json.loads_safe('{}')\n",
        },
    }
    assert run_hook(monkeypatch, payload, ["--json"]) == hook.ALLOW
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "loads_safe" in decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"tool_name": "Write", "tool_input": {}},
        {"tool_name": "Write", "tool_input": {"file_path": "a.py"}},
        {"tool_name": "Write", "tool_input": {"file_path": "a.txt", "content": "hi"}},
        {"tool_name": "Edit", "tool_input": {"file_path": "/nope/missing.py",
                                             "old_string": "a", "new_string": "b"}},
        {"tool_name": "Write", "tool_input": {"file_path": "a.py", "content": 42}},
    ],
)
def test_unrecognized_payloads_fail_open(monkeypatch, payload):
    assert run_hook(monkeypatch, payload) == hook.ALLOW


def test_malformed_stdin_fails_open(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert hook.run([]) == hook.ALLOW


def test_an_edit_that_would_not_apply_is_left_alone(monkeypatch, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("import json\n", encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(target),
            "old_string": "text that is not in the file",
            "new_string": "x = json.loads_safe('{}')",
        },
    }
    assert run_hook(monkeypatch, payload) == hook.ALLOW


def test_engine_failure_fails_open(monkeypatch, tmp_path):
    def explode(*args, **kwargs):
        raise RuntimeError("engine is on fire")

    monkeypatch.setattr(hook, "Engine", explode)
    payload = {
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(tmp_path / "bad.py"),
            "content": "import json\nx = json.loads_safe('{}')\n",
        },
    }
    assert run_hook(monkeypatch, payload) == hook.ALLOW
