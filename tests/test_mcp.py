"""MCP wire protocol.

Hand-rolled JSON-RPC means the protocol details are ours to get wrong, so the
ones that break clients hard are pinned here: notifications must draw no reply,
every response must echo its request id, and a tool that raises must come back
as an error *result* rather than killing the connection.
"""

from __future__ import annotations

import io
import json

import pytest

from bleurs import mcp


def call(method, params=None, request_id=1, root=None):
    server = mcp.Server(root)
    message = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return server.handle(message)


def tool(name, arguments, root=None):
    response = call("tools/call", {"name": name, "arguments": arguments}, root=root)
    return response["result"]["content"][0]["text"]


# -- handshake -----------------------------------------------------------


def test_initialize_reports_identity_and_capabilities():
    result = call("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert result["serverInfo"]["name"] == "bleurs"
    assert "tools" in result["capabilities"]


def test_initialize_echoes_the_clients_protocol_version():
    # Answering with our own version when the client asked for a different one
    # is how you get silently disconnected.
    result = call("initialize", {"protocolVersion": "2024-11-05"})["result"]
    assert result["protocolVersion"] == "2024-11-05"


def test_initialize_falls_back_when_no_version_offered():
    result = call("initialize", {})["result"]
    assert result["protocolVersion"] == mcp.DEFAULT_PROTOCOL


def test_notifications_get_no_reply():
    assert call("notifications/initialized", request_id=None) is None


def test_every_response_echoes_its_id():
    assert call("tools/list", request_id="abc")["id"] == "abc"


def test_unknown_method_is_an_error_not_a_crash():
    assert call("nonsense/method")["error"]["code"] == -32601


def test_tools_are_advertised_with_schemas():
    tools = call("tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == {"surface", "verify"}
    for entry in tools:
        assert entry["inputSchema"]["type"] == "object"
        assert entry["description"].strip()


# -- surface tool --------------------------------------------------------


def test_surface_projects_an_installed_module():
    text = tool("surface", {"target": "json"})
    assert "loads(" in text
    assert "tokens)" in text


def test_surface_projects_a_project_file(tmp_path):
    (tmp_path / "mod.py").write_text("def helper(a, b=1):\n    pass\n", encoding="utf-8")
    text = tool("surface", {"target": "mod.py"}, root=tmp_path)
    assert "helper(a, b=1)" in text


def test_surface_rejects_a_missing_file(tmp_path):
    assert "no such file" in tool("surface", {"target": "nope.py"}, root=tmp_path)


def test_surface_requires_a_target():
    assert "required" in tool("surface", {})


# -- verify tool ---------------------------------------------------------


def test_verify_passes_clean_code():
    text = tool("verify", {"code": "import json\nx = json.dumps({})\n"})
    assert text.startswith("OK")


def test_verify_blocks_and_returns_the_real_surface():
    text = tool("verify", {"code": "import json\nx = json.loads_safe('')\n"})
    assert text.startswith("BLOCKED")
    assert "loads_safe" in text
    # The point of the whole exercise: the rejection carries the answer.
    assert "What those containers actually provide" in text
    assert "loads(" in text


def test_verify_reports_a_syntax_error_without_blocking():
    text = tool("verify", {"code": "def broken(:\n"})
    assert "could not parse" in text


def test_verify_requires_code():
    assert "required" in tool("verify", {"code": "   "})


# -- robustness ----------------------------------------------------------


def test_unknown_tool_is_rejected():
    assert call("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602


def test_a_crashing_tool_returns_an_error_result(monkeypatch):
    def explode(args, root):
        raise RuntimeError("boom")

    monkeypatch.setitem(mcp.HANDLERS, "surface", explode)
    result = call("tools/call", {"name": "surface", "arguments": {"target": "json"}})
    assert result["result"]["isError"] is True
    assert "boom" in result["result"]["content"][0]["text"]


@pytest.mark.parametrize("line", ["", "   ", "not json", "[1,2,3]", "null"])
def test_serve_survives_junk_on_the_wire(line):
    out = io.StringIO()
    mcp.serve(stdin=io.StringIO(line + "\n"), stdout=out)
    assert out.getvalue() == ""


def test_serve_round_trips_a_session():
    session = "\n".join(
        json.dumps(m)
        for m in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
    )
    out = io.StringIO()
    mcp.serve(stdin=io.StringIO(session + "\n"), stdout=out)

    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2]  # the notification drew no reply
