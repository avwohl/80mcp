"""Integration tests: the seven phase-1 tools, wired end to end.

Three layers, deliberately separated so the first two run anywhere:

1. **Pure**, no backend and no filesystem: the assert evaluator, the pass
   verdict, the reference-argv substitution, registration and annotations.
2. **Sandbox only**: the ``x80_files`` routes against a tree this test makes,
   and the tools whose whole job is reporting (``x80_profiles``,
   ``x80_images``) -- which must work with every backend absent, because
   SPEC.md 4.7's rule is that a missing backend is a structured answer and
   never a crash.
3. **Live**, skipped without the binaries: SPEC.md 9's phase-1 acceptance
   numbers, driven through ``tools/call`` rather than by calling the handler,
   because the acceptance claim is about the server and not about a function.

The live layer finds its fixtures the same way ``tests/test_normalize.py``
does: ``X80_UN80_SRC`` / ``X80_CPMEMU`` first, then the usual checkouts.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

from eightymcp import __version__
from eightymcp.jsonrpc import Connection, JsonRpcError, ProtocolRevision
from eightymcp.sandbox import Sandbox
from eightymcp.schemas import ANNOTATIONS, PHASE1_TOOLS
from eightymcp.tools import (
    HANDLERS,
    SANDBOX_MARKER,
    _assertions,
    _Manifest,
    _nearest_line,
    _reference_argv,
    _verdict,
    build_server,
)
from eightymcp.types import Assertion, AssertionKind, ExitReason, FileOut

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


# ==========================================================================
# Fixtures on disk
# ==========================================================================

def _find_un80() -> pathlib.Path | None:
    candidates = []
    if os.environ.get("X80_UN80_SRC"):
        candidates.append(pathlib.Path(os.environ["X80_UN80_SRC"]))
    candidates += [
        pathlib.Path.home() / "src" / "80un",
        pathlib.Path(__file__).resolve().parent.parent.parent / "80un",
    ]
    scratch = os.environ.get("CLAUDE_SCRATCHPAD")
    if scratch:
        candidates.append(pathlib.Path(scratch) / "80un")
    for candidate in candidates:
        if (candidate / "80un.com").is_file():
            return candidate
    return None


def _find_cpmemu() -> str | None:
    explicit = os.environ.get("X80_CPMEMU")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("cpmemu")
    if found:
        return found
    for candidate in (pathlib.Path.home() / "src" / "cpmemu" / "src" / "cpmemu",):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


UN80 = _find_un80()
CPMEMU = _find_cpmemu()
ARC = (UN80 / "tests" / "samples" / "arc" / "method9.arc") if UN80 else None
LIVE = UN80 is not None and CPMEMU is not None and ARC is not None and ARC.is_file()
needs_live = pytest.mark.skipif(
    not LIVE, reason="needs a 80un checkout (X80_UN80_SRC) and a cpmemu binary (X80_CPMEMU)"
)
needs_cpmemu = pytest.mark.skipif(
    CPMEMU is None, reason="needs a cpmemu binary (X80_CPMEMU)"
)

DOSIZ = os.environ.get("EIGHTYMCP_DOSIZ") or shutil.which("dosiz")
needs_dosiz = pytest.mark.skipif(
    not (DOSIZ and os.access(DOSIZ, os.X_OK)), reason="needs a dosiz binary (EIGHTYMCP_DOSIZ)"
)


def _client_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    if CPMEMU:
        env["EIGHTYMCP_CPMEMU"] = CPMEMU
    return env


class Stdio:
    """One server subprocess, driven the way a client drives it."""

    def __init__(self, revision: str = "2026-07-28"):
        self.revision = revision
        self.p = subprocess.Popen(
            [sys.executable, "-m", "eightymcp.cli"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_client_env(), bufsize=0,
        )
        self.n = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}
        msg["params"].setdefault("_meta", {"protocolVersion": self.revision})
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()
        raw = self.p.stdout.readline()
        if not raw:
            raise AssertionError(f"server closed stdout; stderr:\n{self.p.stderr.read().decode()}")
        return json.loads(raw)

    def tool(self, name, arguments):
        return self.call("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        self.stderr = self.p.stderr.read().decode()
        self.p.wait(timeout=60)
        for pipe in (self.p.stdout, self.p.stderr):
            try:
                pipe.close()
            except OSError:
                pass


# ==========================================================================
# 1. Registration and annotations
# ==========================================================================

def test_all_seven_phase_one_tools_register():
    server = build_server()
    assert [t.name for t in server.registry.ordered()] == list(PHASE1_TOOLS)


def test_handlers_are_the_phase_one_set():
    assert tuple(HANDLERS) == tuple(PHASE1_TOOLS)


@pytest.mark.parametrize("name", PHASE1_TOOLS)
def test_annotations_come_from_the_spec_table(name):
    """SPEC.md 6.2: the defaults bite, so every one is written out."""
    tool = build_server().registry.get(name)
    assert tool.annotations == ANNOTATIONS[name]
    assert tool.annotations.openWorldHint is (name == "x80_images")


def test_only_x80_images_is_open_world():
    server = build_server()
    open_world = [t.name for t in server.registry.ordered() if t.annotations.openWorldHint]
    assert open_world == ["x80_images"]


def test_x80_files_is_the_destructive_one():
    server = build_server()
    destructive = [t.name for t in server.registry.ordered() if t.annotations.destructiveHint]
    assert destructive == ["x80_files"]


# ==========================================================================
# 2. The assert evaluator
# ==========================================================================

def _manifest(*names: str) -> _Manifest:
    return _Manifest([
        FileOut(guest_name=n, bytes=1, sha256="a" * 64, host_name=n.lower())
        for n in names
    ])


def test_stdout_contains_passes_and_fails():
    got = _assertions(
        {"stdout_contains": ["23 file(s) extracted"]},
        stdout="\r\n23 file(s) extracted\r\n",
        manifest=_manifest(),
        unimplemented_key="no_unimplemented_bdos",
        unimplemented=[],
    )
    assert [a.to_json() for a in got] == [
        {"kind": AssertionKind.STDOUT_CONTAINS, "value": "23 file(s) extracted", "ok": True}
    ]


def test_a_failed_stdout_contains_reports_the_line_the_guest_printed_instead():
    """SPEC.md 7.1's negative case, verbatim: value 23, actual 1."""
    got = _assertions(
        {"stdout_contains": ["23 file(s) extracted"]},
        stdout="\r\n80UN - CP/M Archive Unpacker v2.3\r\n\r\n  B5-TIME.INF\r\n  Error\r\n\r\n1 file(s) extracted\r\n",
        manifest=_manifest(),
        unimplemented_key="no_unimplemented_bdos",
        unimplemented=[],
    )
    assert got[0].to_json() == {
        "kind": AssertionKind.STDOUT_CONTAINS,
        "value": "23 file(s) extracted",
        "ok": False,
        "actual": "1 file(s) extracted",
    }


def test_nearest_line_falls_back_to_a_bounded_tail():
    assert _nearest_line("", "anything") == ""
    out = _nearest_line("x" * 500, "nothing like it")
    assert out.startswith("...") and len(out) <= 203


def test_files_created_is_case_insensitive_across_both_names():
    """cpmemu lowercases on create; an assertion must not fail on case."""
    got = _assertions(
        {"files_created": ["TIME2.ASM", "time2.asm", "MISSING.TXT"]},
        stdout="",
        manifest=_manifest("TIME2.ASM"),
        unimplemented_key="no_unimplemented_bdos",
        unimplemented=[],
    )
    assert [a.ok for a in got] == [True, True, False]
    assert got[2].actual == ["TIME2.ASM"]


def test_file_sha256_reports_the_actual_hash():
    got = _assertions(
        {"file_sha256": {"TIME2.ASM": "b" * 64}},
        stdout="",
        manifest=_manifest("TIME2.ASM"),
        unimplemented_key="no_unimplemented_bdos",
        unimplemented=[],
    )
    assert got[0].ok is False
    assert got[0].actual == "a" * 64


def test_a_false_boolean_assertion_produces_no_entry():
    """The schema fills ``no_unimplemented_bdos: false`` whenever an assert
    block exists at all; asserting nothing is not a passing assertion."""
    got = _assertions(
        {"no_unimplemented_bdos": False},
        stdout="",
        manifest=_manifest(),
        unimplemented_key="no_unimplemented_bdos",
        unimplemented=[100],
    )
    assert got == []


def test_no_unimplemented_bdos_reports_the_numbers():
    got = _assertions(
        {"no_unimplemented_bdos": True},
        stdout="",
        manifest=_manifest(),
        unimplemented_key="no_unimplemented_bdos",
        unimplemented=[100, 107],
    )
    assert got[0].ok is False and got[0].actual == [100, 107]


# ==========================================================================
# 3. The pass verdict
# ==========================================================================

def test_pass_is_the_and_of_the_assertions():
    ok = [Assertion.passed(AssertionKind.STDOUT_CONTAINS, "a")]
    bad = ok + [Assertion.failed(AssertionKind.STDOUT_CONTAINS, "b", "c")]
    assert _verdict(ok, exit_reason=ExitReason.JMP_0) is True
    assert _verdict(bad, exit_reason=ExitReason.JMP_0) is False


def test_an_empty_assert_block_passes_a_run_that_happened():
    assert _verdict([], exit_reason=ExitReason.JMP_0) is True


def test_a_timeout_vetoes_pass_and_says_so():
    warnings: list[str] = []
    assert _verdict([], exit_reason=ExitReason.TIMEOUT, warnings=warnings) is False
    assert warnings and "did not complete" in warnings[0]


def test_a_guest_that_reported_its_own_failure_can_still_pass():
    """SPEC.md 5.4 Invariant 4: success is asserted from stdout, so asserting
    that the guest printed its error is a passing run."""
    got = _assertions(
        {"stdout_contains": ["Error"]},
        stdout="  B5-TIME.INF\r\n  Error\r\n",
        manifest=_manifest(),
        unimplemented_key="no_unimplemented_bdos",
        unimplemented=[],
    )
    assert _verdict(got, exit_reason=ExitReason.JMP_0) is True


# ==========================================================================
# 4. x80_diff_run's reference substitution
# ==========================================================================

def test_reference_argv_substitutes_input_and_outdir():
    warnings: list[str] = []
    out = _reference_argv(
        ["python3", "-m", "un80.cli", "{input}", "-o", "{outdir}"],
        [pathlib.Path("/tmp/a/M9.ARC")], pathlib.Path("/tmp/out"), warnings,
    )
    assert out == ["python3", "-m", "un80.cli", "/tmp/a/M9.ARC", "-o", "/tmp/out"]
    assert warnings == []


def test_a_bare_input_token_expands_to_every_input():
    warnings: list[str] = []
    out = _reference_argv(
        ["tool", "{input}", "{outdir}"],
        [pathlib.Path("/tmp/a"), pathlib.Path("/tmp/b")], pathlib.Path("/o"), warnings,
    )
    assert out == ["tool", "/tmp/a", "/tmp/b", "/o"]


def test_an_embedded_input_token_takes_the_first_and_warns():
    warnings: list[str] = []
    out = _reference_argv(
        ["tool", "--in={input}", "{outdir}"],
        [pathlib.Path("/tmp/a"), pathlib.Path("/tmp/b")], pathlib.Path("/o"), warnings,
    )
    assert out == ["tool", "--in=/tmp/a", "/o"]
    assert warnings and "only the first" in warnings[0]


def test_a_reference_with_no_outdir_is_refused():
    server = build_server()
    conn = Connection()
    with pytest.raises(JsonRpcError):
        # An outright schema violation would be caught earlier; this one is
        # schema-legal and semantically empty, so it is the handler's refusal.
        server.handle_tools_call({"name": "x80_diff_run", "arguments": {}}, conn)
    out = server.handle_tools_call({"name": "x80_diff_run", "arguments": {
        "guest": {"profile": "cpm-hosted", "program": "/nonexistent"},
        "reference": {"argv": ["true"]},
        "inputs": [{"guest_name": "A", "content_b64": ""}],
    }}, conn)
    assert out["isError"] is True
    assert out["structuredContent"]["argument"] == "reference.argv"


# ==========================================================================
# 5. The reporting tools work with every backend absent
# ==========================================================================

def test_x80_profiles_answers_without_any_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("EIGHTYMCP_NO_SIBLING_SEARCH", "1")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("EIGHTYMCP_CONFIG", str(tmp_path / "absent.json"))
    out = build_server().handle_tools_call(
        {"name": "x80_profiles", "arguments": {}}, Connection()
    )
    body = out["structuredContent"]
    assert out["isError"] is False
    assert len(body["profiles"]) == 11
    assert all(p["blocked_by"] for p in body["profiles"] if not p["ready"])
    assert body["server_version"] == __version__


def test_x80_profiles_reports_the_negotiated_revision_not_the_latest():
    conn = Connection(revision=ProtocolRevision.V2025_06_18)
    out = build_server().handle_tools_call(
        {"name": "x80_profiles", "arguments": {"probe": False}}, conn
    )
    assert out["structuredContent"]["protocol_version"] == "2025-06-18"
    assert "resultType" not in out


def test_x80_images_never_fetches_without_both_flags(tmp_path, monkeypatch):
    # Empty image roots, so the result does not depend on this machine's cache.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("EIGHTYMCP_IMAGE_DIR", str(tmp_path / "empty"))
    out = build_server().handle_tools_call({"name": "x80_images", "arguments": {
        "image_ids": ["hd1k_combo"], "dry_run": False,
    }}, Connection())
    assert out["isError"] is True
    assert out["structuredContent"]["error"] == "fetch_not_allowed"


def test_x80_images_unknown_id_is_actionable():
    out = build_server().handle_tools_call({"name": "x80_images", "arguments": {
        "image_ids": ["not_an_image"],
    }}, Connection())
    assert out["isError"] is True
    assert out["structuredContent"]["unknown"] == ["not_an_image"]


# ==========================================================================
# 6. x80_files, sandbox routes
# ==========================================================================

@pytest.fixture()
def kept_sandbox(tmp_path):
    """A sandbox tree shaped exactly like one a batch verb left behind."""
    sb = Sandbox(base_dir=tmp_path, keep=True)
    (sb.guest_dir / "time2.asm").write_bytes(b"guest output\r\n")
    (sb.guest_dir / "M9.ARC").write_bytes(b"staged input")
    state = {
        p.name: [p.stat().st_size, p.stat().st_mtime_ns]
        for p in sb.guest_dir.iterdir()
    }
    sb.write_text(SANDBOX_MARKER, json.dumps({
        "schema": 1, "tool": "x80_cpm_run", "profile": "cpm-hosted",
        "backend": "cpmemu", "family": "z80", "created_ns": 1, "last_call_ns": 1,
        "staged": ["M9.ARC"], "created": ["time2.asm"], "state": state,
    }))
    yield sb
    sb.keep = False
    sb.cleanup()


def _files(arguments):
    return build_server().handle_tools_call(
        {"name": "x80_files", "arguments": arguments}, Connection()
    )


def test_files_list_reports_guest_and_host_names(kept_sandbox):
    out = _files({"op": "list", "sandbox": str(kept_sandbox.root), "hash": True})
    body = out["structuredContent"]
    names = {f["guest_name"]: f for f in body["files"]}
    assert set(names) == {"TIME2.ASM", "M9.ARC"}
    assert names["TIME2.ASM"]["host_name"] == "time2.asm"
    assert names["TIME2.ASM"]["created"] is True
    assert names["M9.ARC"]["created"] is False
    assert len(names["TIME2.ASM"]["sha256"]) == 64
    assert body["via"] == "sandbox"
    assert body["drives"] == [
        {"letter": "A", "backing": str(kept_sandbox.guest_dir), "writable": True}
    ]


def test_files_list_since_start_drops_the_staged_input(kept_sandbox):
    out = _files({"op": "list", "sandbox": str(kept_sandbox.root), "since": "start"})
    assert [f["guest_name"] for f in out["structuredContent"]["files"]] == ["TIME2.ASM"]


def test_files_to_guest_refuses_to_clobber_without_overwrite(kept_sandbox):
    payload = base64.b64encode(b"new").decode()
    out = _files({"op": "to_guest", "sandbox": str(kept_sandbox.root), "files": [
        {"guest_name": "time2.asm", "content_b64": payload},
    ]})
    body = out["structuredContent"]
    assert body["placed"] == []
    assert body["skipped"][0]["guest_name"] == "time2.asm"
    assert (kept_sandbox.guest_dir / "time2.asm").read_bytes() == b"guest output\r\n"

    out = _files({"op": "to_guest", "sandbox": str(kept_sandbox.root), "overwrite": True,
                  "files": [{"guest_name": "time2.asm", "content_b64": payload}]})
    assert out["structuredContent"]["placed"][0]["bytes"] == 3
    assert (kept_sandbox.guest_dir / "time2.asm").read_bytes() == b"new"


def test_files_to_guest_dedupes_on_an_idempotency_key(kept_sandbox):
    payload = base64.b64encode(b"once").decode()
    args = {"op": "to_guest", "sandbox": str(kept_sandbox.root), "overwrite": True,
            "idempotency_key": "nonce-1",
            "files": [{"guest_name": "ONCE.TXT", "content_b64": payload}]}
    first = _files(args)["structuredContent"]
    (kept_sandbox.guest_dir / "ONCE.TXT").unlink()
    second = _files(args)["structuredContent"]
    assert first == second
    # The retry was served from the cache, so the file was not re-created.
    assert not (kept_sandbox.guest_dir / "ONCE.TXT").exists()


def test_files_from_guest_inlines_and_exports(kept_sandbox, tmp_path):
    export = tmp_path / "export"
    out = _files({"op": "from_guest", "sandbox": str(kept_sandbox.root),
                  "names": ["TIME2.ASM"], "export_to": str(export)})
    body = out["structuredContent"]
    row = body["files"][0]
    assert base64.b64decode(row["content_b64"]) == b"guest output\r\n"
    assert row["resource_uri"].startswith("80mcp://sandbox/")
    assert pathlib.Path(row["host_path"]).read_bytes() == b"guest output\r\n"
    assert body["truncated"] == []


def test_files_from_guest_records_what_it_did_not_inline(kept_sandbox):
    out = _files({"op": "from_guest", "sandbox": str(kept_sandbox.root),
                  "names": ["TIME2.ASM"], "return_content": "never"})
    body = out["structuredContent"]
    assert "content_b64" not in body["files"][0]
    assert body["truncated"] == ["TIME2.ASM"]


def test_files_refuses_a_directory_that_is_not_a_sandbox(tmp_path):
    out = _files({"op": "list", "sandbox": str(tmp_path)})
    assert out["isError"] is True
    assert out["structuredContent"]["argument"] == "sandbox"


def test_files_refuses_a_handle_and_points_at_phase_two(kept_sandbox):
    out = _files({"op": "list", "handle": "m-1"})
    assert out["isError"] is True
    assert out["structuredContent"]["error"] == "unsupported"
    assert out["structuredContent"]["escalation"]["tool"] == "x80_cpm_run"


def test_files_handles_and_resolve_are_phase_five(kept_sandbox):
    for op in ("handles", "resolve"):
        out = _files({"op": op, "sandbox": str(kept_sandbox.root)})
        assert out["isError"] is True, op
        assert out["structuredContent"]["error"] == "unsupported"


def test_files_out_of_range_drive_names_the_profiles_set(kept_sandbox):
    out = _files({"op": "list", "sandbox": str(kept_sandbox.root), "drive": "Z"})
    assert out["isError"] is True
    assert out["structuredContent"]["drives"][0] == "A:"
    assert "P:" in out["structuredContent"]["drives"]


def test_files_hostfile_and_image_routes_are_later_phases(kept_sandbox):
    for via in ("hostfile", "image"):
        out = _files({"op": "list", "sandbox": str(kept_sandbox.root), "via": via})
        assert out["isError"] is True, via
        assert out["structuredContent"]["error"] == "unsupported"


# ==========================================================================
# 7. Live: SPEC.md 9's phase-1 acceptance numbers, over real stdio
# ==========================================================================

@needs_live
def test_acceptance_method9_arc_under_cpm_hosted():
    """SPEC.md 9: 23 file(s) extracted, exit_reason jmp_0, B5-TIME.INF 1664."""
    with Stdio() as s:
        r = s.tool("x80_cpm_run", {
            "profile": "cpm-hosted", "cpu": "z80",
            "program": str(UN80 / "80un.com"), "args": ["M9.ARC"],
            "files_in": [{"guest_name": "M9.ARC", "host_path": str(ARC), "mode": "binary"}],
            "default_mode": "binary", "eol_convert": False, "timeout_ms": 10000,
            "assert": {
                "stdout_contains": ["23 file(s) extracted"],
                "stdout_not_contains": ["Error", "Cannot open", "Invalid"],
                "files_created": ["TIME2.ASM", "ZTIM-S3.CPM"],
                "no_unimplemented_bdos": True,
            },
        })["result"]
    b = r["structuredContent"]
    assert b["pass"] is True
    assert b["exit_reason"] == "jmp_0"
    assert "23 file(s) extracted" in b["stdout"]
    assert len(b["files_out"]) == 23
    b5 = [f for f in b["files_out"] if f["guest_name"] == "B5-TIME.INF"]
    assert b5 and b5[0]["bytes"] == 1664
    assert b5[0]["host_name"] == "b5-time.inf"
    assert all(a["ok"] for a in b["assertions"])
    assert b["list_output"] == {"bytes": 0}
    assert b["sandbox"] is None


@needs_live
def test_no_exit_code_key_anywhere_in_a_cpm_result():
    """SPEC.md 5.4 Invariant 4, asserted on the wire and not on the dataclass."""
    with Stdio() as s:
        b = s.tool("x80_cpm_run", {
            "profile": "cpm-hosted", "program": str(UN80 / "80un.com"),
        })["result"]["structuredContent"]

    def keys(obj):
        if isinstance(obj, dict):
            return set(obj) | {k for v in obj.values() for k in keys(v)}
        if isinstance(obj, list):
            return {k for v in obj for k in keys(v)}
        return set()

    assert "exit_code" not in keys(b)


@needs_live
def test_acceptance_diff_run_needs_exactly_two_normalizations():
    """SPEC.md 7.2 / 9: 23/0 with both, 2/21 with lowercase_names alone."""
    def run(normalize):
        with Stdio() as s:
            return s.tool("x80_diff_run", {
                "guest": {"profile": "cpm-hosted", "program": str(UN80 / "80un.com"),
                          "args": ["M9.ARC"], "default_mode": "binary"},
                "reference": {"argv": ["python3", "-m", "un80.cli", "{input}", "-o", "{outdir}"],
                              "env": {"PYTHONPATH": "src"}, "cwd": str(UN80)},
                "inputs": [{"guest_name": "M9.ARC", "host_path": str(ARC)}],
                "normalize": normalize, "compare": "bytes", "timeout_ms": 60000,
            })["result"]["structuredContent"]

    both = run(["lowercase_names", "pad_to_record"])
    assert (both["files_compared"], both["identical"], both["differ"]) == (23, 23, 0)
    assert both["pass"] is True

    lower = run(["lowercase_names"])
    assert (lower["identical"], lower["differ"]) == (2, 21)

    none = run([])
    assert len(none["only_in_guest"]) + len(none["only_in_reference"]) == 46


@needs_live
def test_keep_sandbox_then_x80_files_reads_it_back():
    with Stdio() as s:
        b = s.tool("x80_cpm_run", {
            "profile": "cpm-hosted", "program": str(UN80 / "80un.com"), "args": ["M9.ARC"],
            "files_in": [{"guest_name": "M9.ARC", "host_path": str(ARC)}],
            "keep_sandbox": True, "collect": {"return_content": "never"},
        })["result"]["structuredContent"]
        path = b["sandbox"]
        assert isinstance(path, str)
        listed = s.tool("x80_files", {"op": "list", "sandbox": path, "since": "start"})
        names = [f["guest_name"] for f in listed["result"]["structuredContent"]["files"]]
    try:
        assert len(names) == 23
        assert "B5-TIME.INF" in names
    finally:
        shutil.rmtree(path, ignore_errors=True)


@needs_live
def test_probe_says_cpm_hosted_is_sufficient_for_80un():
    with Stdio() as s:
        b = s.tool("x80_probe", {
            "program": str(UN80 / "80un.com"), "args": ["M9.ARC"],
            "files_in": [{"guest_name": "M9.ARC", "host_path": str(ARC)}],
        })["result"]["structuredContent"]
    assert b["ran_on"] == "cpm-hosted"
    assert b["verdict"] == "sufficient"
    assert b["unimplemented"] == []
    assert b["escalation"]["tool"] == "x80_cpm_run"


@needs_live
def test_collect_normalize_moves_the_report_and_never_the_file():
    """SPEC.md 6.4: "applied to the REPORTED sha256 and content only"."""
    import hashlib

    with Stdio() as s:
        b = s.tool("x80_cpm_run", {
            "profile": "cpm-hosted", "program": str(UN80 / "80un.com"), "args": ["M9.ARC"],
            "files_in": [{"guest_name": "M9.ARC", "host_path": str(ARC)}],
            "keep_sandbox": True,
            "collect": {"return_content": "never", "normalize": ["strip_cpm_eof"]},
        })["result"]["structuredContent"]
    path = b["sandbox"]
    try:
        raw = (pathlib.Path(path) / "guest" / "b5-time.inf").read_bytes()
        row = [f for f in b["files_out"] if f["guest_name"] == "B5-TIME.INF"][0]
        # On disk: the 1664 bytes the guest wrote, untouched. Reported: the
        # same bytes with the trailing 0x1A run removed, which is 1472 here --
        # not 1537, because the member's own content ends in 65 of them too.
        assert len(raw) == 1664
        assert row["bytes"] == len(raw.rstrip(b"\x1a")) == 1472
        assert row["sha256"] == hashlib.sha256(raw.rstrip(b"\x1a")).hexdigest()
        assert any("B5-TIME.INF" in w for w in b["warnings"])
    finally:
        shutil.rmtree(path, ignore_errors=True)


@needs_cpmemu
def test_a_deadline_kills_a_spinning_guest_and_is_not_a_pass(tmp_path):
    """SPEC.md 5.5: "Nothing in the family has a timeout." cpmemu's only guard
    is a 9e9-instruction watchdog, so a JMP $ at 0100h runs for minutes; the
    server's deadline is the only thing that ends this call."""
    spin = tmp_path / "SPIN.COM"
    spin.write_bytes(b"\xc3\x00\x01")            # jp 0100h
    started = time.monotonic()
    with Stdio() as s:
        b = s.tool("x80_cpm_run", {
            "profile": "cpm-hosted", "program": str(spin), "timeout_ms": 1000,
        })["result"]["structuredContent"]
    elapsed_ms = (time.monotonic() - started) * 1000
    assert b["exit_reason"] == "timeout"
    assert b["pass"] is False
    assert any("did not complete" in w for w in b["warnings"])
    assert 1000 <= elapsed_ms < 15000


@needs_dosiz
def test_dos_run_filters_the_two_lines_dosiz_prints_every_run(tmp_path):
    """SPEC.md 5.5 / 7.5: removed by name, and the removal is reported."""
    prog = tmp_path / "EXIT0.COM"
    prog.write_bytes(b"\xb4\x4c\xb0\x00\xcd\x21")   # mov ah,4Ch / mov al,0 / int 21h
    with Stdio() as s:
        b = s.tool("x80_dos_run", {
            "profile": "dos-hosted", "program": str(prog), "timeout_ms": 10000,
            "expect_exit_code": 0,
        })["result"]["structuredContent"]
    assert b["exit_code"] == 0
    assert b["exit_code_meaning"] == "guest_exit"
    assert b["pass"] is True
    filtered = b["diagnostics"]["stderr_filtered"]
    assert len(filtered) == 2
    assert all(line.startswith("dosiz: ") for line in filtered)
    assert "slirp" not in b["stderr"]


# ==========================================================================
# 8. The CLI
# ==========================================================================

def test_the_reaper_removes_only_old_marked_sandboxes(tmp_path, monkeypatch):
    """SPEC.md 4.3: a kept sandbox lives on disk for 24 hours, not forever."""
    from eightymcp.cli import reap_old_sandboxes
    from eightymcp.sandbox import SANDBOX_PREFIX

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    now = time.time()
    old = tmp_path / (SANDBOX_PREFIX + "old")
    young = tmp_path / (SANDBOX_PREFIX + "young")
    unmarked = tmp_path / (SANDBOX_PREFIX + "unmarked")
    stranger = tmp_path / "someone-elses-directory"
    for d in (old, young, unmarked, stranger):
        (d / "guest").mkdir(parents=True)
    (old / SANDBOX_MARKER).write_text(json.dumps({"created_ns": int((now - 90000) * 1e9)}))
    (young / SANDBOX_MARKER).write_text(json.dumps({"created_ns": int(now * 1e9)}))
    (stranger / SANDBOX_MARKER).write_text(json.dumps({"created_ns": 0}))

    removed = reap_old_sandboxes(now=now)

    assert removed == [str(old)]
    assert not old.exists()
    assert young.exists()
    assert unmarked.exists(), "no marker, not ours, not touched"
    assert stranger.exists(), "wrong prefix, not ours, not touched"


def test_cli_version():
    out = subprocess.run(
        [sys.executable, "-m", "eightymcp.cli", "--version"],
        capture_output=True, text=True, env=_client_env(),
    )
    assert out.returncode == 0
    assert __version__ in out.stdout


def test_doctor_prints_the_table_and_exits_nonzero_on_a_problem():
    out = subprocess.run(
        [sys.executable, "-m", "eightymcp.cli", "doctor"],
        capture_output=True, text=True, env=_client_env(),
    )
    assert "backends" in out.stdout and "profiles" in out.stdout
    # 0 clean, 1 something blocked, 2 nothing runnable. Any machine that does
    # not have all five backends installed lands on 1 or 2, and this repo
    # ships none of them.
    assert out.returncode in (0, 1, 2)
    assert ("profiles ready" in out.stdout) or ("nothing" in out.stdout.lower())


def test_doctor_json_is_the_x80_profiles_body():
    out = subprocess.run(
        [sys.executable, "-m", "eightymcp.cli", "doctor", "--json"],
        capture_output=True, text=True, env=_client_env(),
    )
    body = json.loads(out.stdout)
    assert {"profiles", "server_version", "protocol_version"} <= set(body)


def test_serving_over_stdio_keeps_the_frames_clean():
    """claim_stdout(): every frame on fd 1 is one JSON object and nothing else."""
    with Stdio() as s:
        first = s.call("tools/list")
        second = s.tool("x80_profiles", {"probe": False})
    assert len(first["result"]["tools"]) == 7
    assert second["result"]["isError"] is False
