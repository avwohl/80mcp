"""Protocol tests: dual-revision negotiation, the two error channels, framing.

The dual-revision requirement is not in SPEC.md and is the reason this file
exists. SPEC.md section 4 targets MCP 2026-07-28, which deleted the
``initialize`` handshake in favour of ``server/discover`` plus a
``_meta.protocolVersion`` on every request, and made ``resultType`` and
``ttlMs``/``cacheScope`` required. Every client shipping today still opens with
``initialize``. Both have to work, and the difference must be confined to the
envelope: the tool list, the schemas and the result bodies are identical on
either.

Also asserted here, from SPEC.md 9 phase 1's acceptance list and Appendix B:

* every ``inputSchema`` in the repo ``json.loads``;
* the string ``"tool_result"`` and the string ``"server"`` appear nowhere as a
  ``resultType`` or a ``cacheScope``;
* ``x80_cpm_run``'s result carries no ``exit_code`` field anywhere.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from eightymcp import __version__
from eightymcp.jsonrpc import (
    LATEST_REVISION,
    SUPPORTED_REVISIONS,
    Connection,
    ErrorCode,
    JsonRpcError,
    negotiate,
)
from eightymcp.schemas import CANONICAL_TOOL_ORDER, INPUT_SCHEMAS, PHASE1_TOOLS
from eightymcp.server import Server, Tool, ToolResult, validate_and_fill
from eightymcp.types import (
    CacheScope,
    ExitReason,
    ProtocolRevision,
    ResultType,
    RunResult,
    ToolError,
    ToolExecutionError,
)

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# Fixtures: a server with two stub tools, one that works and one that fails
# ---------------------------------------------------------------------------

def _ok_handler(ctx):
    return ToolResult.ok({"profiles": [], "server_version": __version__,
                          "protocol_version": ctx.connection.revision.value,
                          "external_servers_recommended": []})


def _failing_handler(ctx):
    # SPEC.md 4.7: an unsupported op is a structured, actionable body inside a
    # normal result -- not a JSON-RPC error.
    raise ToolExecutionError(
        ToolError.unsupported(
            "step", "cpmemu",
            "cpmemu has no debugger; CPMEmulator is declared inside a "
            "3429-line cpmemu.cc with no header, so nothing can reach it from "
            "outside the process",
            alternative_profile="cpm22",
        )
    )


def _crashing_handler(ctx):
    raise RuntimeError("a bug in a handler")


def make_server() -> Server:
    server = Server()
    server.register_spec_tool("x80_profiles", _ok_handler)
    server.register_spec_tool("x80_cpm_run", _failing_handler)
    server.register_spec_tool("x80_images", _crashing_handler)
    return server


def call(server: Server, method: str, params=None, *, id=1, meta=None):
    message = {"jsonrpc": "2.0", "id": id, "method": method}
    params = dict(params or {})
    if meta is not None:
        params["_meta"] = meta
    if params:
        message["params"] = params
    return server.rpc.dispatch(message)


def initialize(server: Server, version: str):
    return call(server, "initialize", {
        "protocolVersion": version,
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0"},
    })


# ---------------------------------------------------------------------------
# negotiate()
# ---------------------------------------------------------------------------

def test_negotiate_exact_matches():
    for rev in SUPPORTED_REVISIONS:
        assert negotiate(rev.value) is rev


def test_negotiate_absent_means_latest():
    assert negotiate(None) is LATEST_REVISION


def test_negotiate_future_revision_clamps_to_latest():
    # A client that asks for a revision newer than anything we know is told
    # what we actually speak, rather than being refused.
    assert negotiate("2099-01-01") is LATEST_REVISION


def test_negotiate_between_known_revisions_rounds_down():
    assert negotiate("2026-01-01") is ProtocolRevision.V2025_11_25


def test_negotiate_prehistoric_revision_is_refused():
    # 2024-11-05 is what altairsim hardcodes (SPEC.md 1.4). It predates every
    # convention in SPEC.md section 4, so it is refused with the list of what
    # would work, not silently upgraded.
    with pytest.raises(JsonRpcError) as excinfo:
        negotiate("2024-11-05")
    assert excinfo.value.code == ErrorCode.INVALID_PARAMS
    assert excinfo.value.data["supported"] == [r.value for r in SUPPORTED_REVISIONS]


# ---------------------------------------------------------------------------
# The legacy initialize handshake
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25"])
def test_initialize_echoes_a_supported_revision(version):
    server = make_server()
    response = initialize(server, version)
    result = response["result"]
    assert result["protocolVersion"] == version
    assert result["serverInfo"]["name"] == "80mcp"
    assert result["serverInfo"]["version"] == __version__
    assert server.rpc.connection.revision.value == version
    assert server.rpc.connection.initialize_seen is True


def test_initialize_result_never_carries_result_type():
    # A client that sends `initialize` is by definition not a 2026-07-28
    # client, since that revision has no such request.
    server = make_server()
    for version in ("2025-06-18", "2025-11-25", "2026-07-28"):
        result = initialize(make_server(), version)["result"]
        assert "resultType" not in result
    del server


def test_initialize_with_2026_still_answers():
    # Not a shape any real client sends, but a server that refuses it is
    # refusing a superset of what it supports.
    server = make_server()
    result = initialize(server, "2026-07-28")["result"]
    assert result["protocolVersion"] == "2026-07-28"
    assert server.rpc.connection.emits_result_metadata is True


def test_notifications_initialized_produces_no_response():
    server = make_server()
    initialize(server, "2025-06-18")
    response = server.rpc.dispatch(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert response is None
    assert server.rpc.connection.initialized_notified is True


# ---------------------------------------------------------------------------
# The 2026-07-28 path
# ---------------------------------------------------------------------------

def test_meta_protocol_version_negotiates_without_a_handshake():
    server = make_server()
    assert server.rpc.connection.negotiated is False
    call(server, "tools/list", meta={"protocolVersion": "2026-07-28",
                                     "clientCapabilities": {}})
    assert server.rpc.connection.negotiated is True
    assert server.rpc.connection.revision is ProtocolRevision.V2026_07_28


def test_no_handshake_at_all_defaults_to_latest():
    # A 2026-07-28 client need send nothing special, so an unannounced
    # connection is assumed modern. A legacy client always sends initialize.
    server = make_server()
    result = call(server, "tools/list")["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 86400000
    assert result["cacheScope"] == "public"


def test_initialize_is_authoritative_over_a_later_meta():
    # A client that handshook has a parser fixed to the revision it agreed to;
    # a stray _meta must not silently start adding fields it cannot read.
    server = make_server()
    initialize(server, "2025-06-18")
    result = call(server, "tools/list", meta={"protocolVersion": "2026-07-28"})["result"]
    assert server.rpc.connection.revision is ProtocolRevision.V2025_06_18
    assert "resultType" not in result
    assert "ttlMs" not in result


def test_server_discover_reports_every_supported_revision():
    server = make_server()
    result = call(server, "server/discover")["result"]
    assert result["protocolVersion"] == LATEST_REVISION.value
    assert result["protocolVersions"] == [r.value for r in SUPPORTED_REVISIONS]
    assert result["serverInfo"]["name"] == "80mcp"
    assert result["resultType"] == "complete"


def test_server_discover_omits_result_type_on_a_legacy_connection():
    server = make_server()
    initialize(server, "2025-06-18")
    result = call(server, "server/discover")["result"]
    assert "resultType" not in result


# ---------------------------------------------------------------------------
# What the revision changes, and what it must not change
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,modern", [
    ("2025-06-18", False),
    ("2025-11-25", False),
    ("2026-07-28", True),
])
def test_result_metadata_is_emitted_only_from_2026_07_28(version, modern):
    server = make_server()
    initialize(server, version)
    listed = call(server, "tools/list")["result"]
    called = call(server, "tools/call", {"name": "x80_profiles", "arguments": {}})["result"]
    assert ("ttlMs" in listed) is modern
    assert ("cacheScope" in listed) is modern
    assert ("resultType" in listed) is modern
    assert ("resultType" in called) is modern


@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25", "2026-07-28"])
def test_the_tool_list_itself_does_not_vary_by_revision(version):
    # SPEC.md 4.1: tools/list MUST NOT vary per-connection. Only the envelope
    # moves; the tools are the same bytes.
    baseline = make_server()
    listed_modern = call(baseline, "tools/list")["result"]["tools"]
    server = make_server()
    initialize(server, version)
    assert call(server, "tools/list")["result"]["tools"] == listed_modern


def test_tool_list_order_is_spec_6_1_order():
    server = make_server()
    names = [t["name"] for t in call(server, "tools/list")["result"]["tools"]]
    # Registered above in a deliberately scrambled order.
    assert names == ["x80_profiles", "x80_cpm_run", "x80_images"]
    index = {n: i for i, n in enumerate(CANONICAL_TOOL_ORDER)}
    assert names == sorted(names, key=lambda n: index[n])


def test_tool_list_carries_the_spec_6_2_annotations():
    server = make_server()
    tools = {t["name"]: t for t in call(server, "tools/list")["result"]["tools"]}
    # SPEC.md 6.2: every tool is openWorldHint:false except x80_images, the
    # only tool that touches the network. The MCP defaults are the other way
    # round, which is why every row is explicit.
    assert tools["x80_profiles"]["annotations"] == {
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": False,
    }
    assert tools["x80_images"]["annotations"]["openWorldHint"] is True
    assert tools["x80_cpm_run"]["annotations"]["openWorldHint"] is False


def test_tools_list_rejects_a_cursor():
    server = make_server()
    error = call(server, "tools/list", {"cursor": "abc"})["error"]
    assert error["code"] == ErrorCode.INVALID_PARAMS


# ---------------------------------------------------------------------------
# The two error channels
# ---------------------------------------------------------------------------

def test_unknown_tool_is_a_jsonrpc_error():
    server = make_server()
    response = call(server, "tools/call", {"name": "x80_nonesuch", "arguments": {}})
    assert "result" not in response
    assert response["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert response["error"]["data"]["available_tools"] == [
        "x80_profiles", "x80_cpm_run", "x80_images"
    ]


def test_unknown_method_is_a_jsonrpc_error():
    server = make_server()
    response = call(server, "resources/read", {"uri": "80mcp://nope"})
    assert response["error"]["code"] == ErrorCode.METHOD_NOT_FOUND


def test_malformed_arguments_are_a_jsonrpc_error():
    # "You called it wrong" -- the agent must change the call.
    server = make_server()
    response = call(server, "tools/call",
                    {"name": "x80_cpm_run", "arguments": {"program": "/x"}})
    assert response["error"]["code"] == ErrorCode.INVALID_PARAMS
    violations = response["error"]["data"]["violations"]
    assert violations == [{"path": "profile", "message": "required property is missing"}]


def test_an_actionable_failure_is_an_iserror_result():
    # "It did not work" -- SPEC.md 4.2 and 4.7. A normal result, isError true,
    # resultType complete, and a body an agent can act on.
    server = make_server()
    result = call(server, "tools/call", {
        "name": "x80_cpm_run",
        "arguments": {"profile": "cpm-hosted", "program": "/x/80un.com"},
    })["result"]
    assert result["isError"] is True
    assert result["resultType"] == "complete"
    body = result["structuredContent"]
    assert body["error"] == "unsupported"
    assert body["op"] == "step"
    assert body["backend"] == "cpmemu"
    assert body["alternative_profile"] == "cpm22"
    assert "no header" in body["reason"]


def test_a_handler_bug_is_an_iserror_result_not_a_jsonrpc_error():
    # From the agent's side a crashed handler is still "it did not work".
    # The traceback goes to stderr, never into the protocol.
    server = make_server()
    response = call(server, "tools/call", {"name": "x80_images", "arguments": {}})
    assert "error" not in response
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "internal_error"
    assert result["structuredContent"]["exception"] == "RuntimeError"


def test_a_successful_call_is_not_an_error():
    server = make_server()
    result = call(server, "tools/call", {"name": "x80_profiles", "arguments": {}})["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["server_version"] == __version__
    # The text fallback is filled in, so a client that ignores
    # structuredContent still sees the answer.
    assert result["content"][0]["type"] == "text"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def test_parse_error_is_reported_with_a_null_id():
    server = make_server()
    out = _serve(server, b"{not json\n")
    assert out[0]["error"]["code"] == ErrorCode.PARSE_ERROR
    assert out[0]["id"] is None


def test_blank_lines_are_skipped():
    server = make_server()
    out = _serve(server, b"\n\n" + _line({"jsonrpc": "2.0", "id": 3, "method": "server/discover"}))
    assert len(out) == 1 and out[0]["id"] == 3


def test_a_notification_produces_no_line():
    server = make_server()
    out = _serve(server, _line({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert out == []


def test_batches_are_rejected():
    # JSON-RPC batching was removed in MCP 2025-06-18 and 80mcp speaks nothing
    # older, so an array is malformed rather than a batch to run.
    server = make_server()
    out = _serve(server, b'[{"jsonrpc":"2.0","id":1,"method":"server/discover"}]\n')
    assert out[0]["error"]["code"] == ErrorCode.INVALID_REQUEST


def test_a_missing_jsonrpc_member_is_rejected():
    server = make_server()
    out = _serve(server, _line({"id": 1, "method": "server/discover"}))
    assert out[0]["error"]["code"] == ErrorCode.INVALID_REQUEST


def test_every_response_is_exactly_one_line():
    server = make_server()
    payload = b"".join(
        _line({"jsonrpc": "2.0", "id": i, "method": "tools/list"}) for i in range(5)
    )
    raw = _serve_raw(server, payload)
    assert raw.count(b"\n") == 5
    assert all(json.loads(line) for line in raw.splitlines())


def _line(obj) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def _serve_raw(server: Server, payload: bytes) -> bytes:
    import io
    out = io.BytesIO()
    server.rpc.serve(io.BytesIO(payload), out)
    return out.getvalue()


def _serve(server: Server, payload: bytes) -> list[dict]:
    return [json.loads(line) for line in _serve_raw(server, payload).splitlines()]


# ---------------------------------------------------------------------------
# stdout discipline (SPEC.md 1.4)
# ---------------------------------------------------------------------------

_STDOUT_PROBE = r'''
import json, os, sys
sys.path.insert(0, {src!r})
from eightymcp.jsonrpc import claim_stdout
from eightymcp.server import Server, ToolResult

def handler(ctx):
    # Three ways a subprocess-heavy server leaks into the JSON-RPC channel.
    # SPEC.md 1.4: altairsim's monitor {{!...}} escape does exactly this and
    # desyncs strict clients.
    print("a stray print()")
    sys.stdout.write("a stray sys.stdout.write\n")
    os.write(1, b"a stray write to fd 1\n")
    os.system("echo a stray subprocess on inherited fd 1")
    return ToolResult.ok({{"ok": True}})

channel = claim_stdout()
server = Server()
server.register_spec_tool("x80_profiles", handler)
server.rpc.serve(sys.stdin.buffer, channel)
'''


def test_nothing_but_jsonrpc_reaches_stdout(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text(_STDOUT_PROBE.format(src=str(SRC)))
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "x80_profiles", "arguments": {}},
    }) + "\n"
    proc = subprocess.run(
        [sys.executable, str(script)], input=request.encode(),
        capture_output=True, timeout=60,
    )
    lines = proc.stdout.splitlines()
    assert lines, proc.stderr.decode()
    for line in lines:
        json.loads(line)          # every stdout line is JSON-RPC, or this raises
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["structuredContent"] == {"ok": True}
    # All four leaks landed on stderr instead.
    err = proc.stderr.decode()
    for leak in ("a stray print()", "a stray sys.stdout.write",
                 "a stray write to fd 1", "a stray subprocess on inherited fd 1"):
        assert leak in err, f"{leak!r} did not reach stderr"


# ---------------------------------------------------------------------------
# SPEC.md 9 phase 1 acceptance assertions that live at this layer
# ---------------------------------------------------------------------------

def test_every_input_schema_json_loads():
    from eightymcp.schemas import INPUT_SCHEMA_JSON
    for name, text in INPUT_SCHEMA_JSON.items():
        assert json.loads(text) == INPUT_SCHEMAS[name]
    assert tuple(INPUT_SCHEMAS) == PHASE1_TOOLS
    assert len(PHASE1_TOOLS) == 7


def test_every_input_schema_validates_against_the_2020_12_metaschema():
    # The other half of the SPEC.md 6.0 CI gate. jsonschema is a dev-only
    # dependency; the server never imports it.
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.validators.validator_for(
        {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    )
    for name, schema in INPUT_SCHEMAS.items():
        validator.check_schema(schema)


def test_no_illegal_result_type_or_cache_scope_anywhere_in_the_source():
    # SPEC.md Appendix B item 6, and the third leg of the 6.0 conformance
    # test: resultType is "complete"/"input_required" and cacheScope is
    # "public"/"private". "tool_result" and "server" are not values.
    assert [m.value for m in ResultType] == ["complete", "input_required"]
    assert [m.value for m in CacheScope] == ["public", "private"]
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        for pattern in ('"tool_result"', "'tool_result'",
                        '"resultType": "server"', '"cacheScope": "server"',
                        '"cacheScope":"server"'):
            if pattern in text:
                offenders.append((str(path), pattern))
    assert offenders == []


def test_a_cpm_result_has_no_exit_code_field_anywhere():
    # SPEC.md 5.4 Invariant 4 and the phase-1 acceptance list: "no exit_code
    # field anywhere in the result". Measured basis: a 23-member ARC under
    # default_mode auto extracted 1 file, truncated it, printed "Error", and
    # exited 0.
    emitted = RunResult(
        pass_=False, exit_reason=ExitReason.JMP_0, wall_ms=630,
        stdout="1 file(s) extracted\r\n", stderr="Program exit via JMP 0\n",
    ).to_json()
    assert "exit_code" not in json.dumps(emitted)
    assert "exit_code" not in INPUT_SCHEMAS["x80_cpm_run"]["properties"]
    # And the DOS twin does carry one, including when it is null.
    assert "expect_exit_code" in INPUT_SCHEMAS["x80_dos_run"]["properties"]


def test_validate_and_fill_supplies_the_binary_default_mode():
    # SPEC.md 5.4 Invariant 3 is enforced by the schema's default, so a
    # handler that reads args["default_mode"] cannot get "auto" by omission.
    filled = validate_and_fill(INPUT_SCHEMAS["x80_cpm_run"],
                               {"profile": "cpm-hosted", "program": "/x/80un.com"})
    assert filled["default_mode"] == "binary"
    assert filled["timeout_ms"] == 10000
    assert filled["cpu"] == "z80"


def test_registering_an_unspecified_tool_fails():
    server = Server()
    with pytest.raises(KeyError):
        server.register_spec_tool("x80_teleport", _ok_handler)
    with pytest.raises(ValueError):
        server.register(Tool(
            name="x80_teleport", description="", input_schema={},
            annotations=next(iter(__import__(
                "eightymcp.schemas", fromlist=["ANNOTATIONS"]).ANNOTATIONS.values())),
            handler=_ok_handler,
        ))


def test_connection_defaults_to_the_latest_revision():
    conn = Connection()
    assert conn.revision is LATEST_REVISION
    assert conn.emits_result_metadata is True
    assert conn.negotiated is False
