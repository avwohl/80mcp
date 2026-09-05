"""Regressions from the hostile review of phase 1.

Every test here reproduces something that was measured to be broken and is now
fixed. They are grouped by the failure they pin down, and each one names the
observed behaviour rather than the intent, because the intent was already
correct in all five cases -- the code did not implement it.

The two that killed the server process outright are first: they are the
failure class SPEC.md 1.4 calls out by name ("Never let a subprocess write to
the JSON-RPC channel"), reached from the other end. A frame that cannot be
written and a frame that is not JSON cost the same thing -- a strict client
that can never resynchronise -- and one malformed request was enough for both.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from eightymcp.jsonrpc import JsonRpcServer, ServerIdentity
from eightymcp.sandbox import Sandbox
from eightymcp.tools import SANDBOX_MARKER, build_server
from eightymcp.types import ToolExecutionError

SRC = str(Path(__file__).resolve().parent.parent / "src")


# ---------------------------------------------------------------------------
# A live server on a real pipe pair, because the bugs below were in the framing
# ---------------------------------------------------------------------------

class Client:
    """Strict stdio client: a line that is not RFC 8259 JSON is a failure."""

    def __init__(self) -> None:
        env = dict(os.environ, PYTHONPATH=SRC)
        self.p = subprocess.Popen(
            [sys.executable, "-m", "eightymcp.cli"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )

    def send_raw(self, data: bytes) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def read(self) -> dict:
        assert self.p.stdout is not None
        line = self.p.stdout.readline()
        if not line:
            raise AssertionError("the server closed stdout instead of answering")

        def reject(token: str):
            raise AssertionError(f"non-standard JSON token on the wire: {token}")

        return json.loads(line, parse_constant=reject)

    def call(self, method: str, params: dict, ident) -> dict:
        self.send_raw(
            json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}).encode()
            + b"\n"
        )
        return self.read()

    def close(self) -> int:
        for stream in (self.p.stdin, self.p.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return self.p.wait(timeout=20)


@pytest.fixture()
def client():
    c = Client()
    try:
        yield c
    finally:
        try:
            c.close()
        except Exception:
            c.p.kill()
            c.p.wait(timeout=10)


# ---------------------------------------------------------------------------
# 1. A lone surrogate used to kill the process
# ---------------------------------------------------------------------------

def test_lone_surrogate_in_an_argument_does_not_kill_the_server(client):
    r"""Measured before the fix: one request, no reply, exit 1.

    ``{"profile":"\ud800"}`` is legal JSON text -- RFC 8259 permits any
    ``\uXXXX`` escape and Python's parser accepts it -- so the string reached
    the schema violation's ``got`` field, and
    ``json.dumps(..., ensure_ascii=False).encode("utf-8")`` raised
    ``UnicodeEncodeError`` inside ``serve``. Traceback on stderr, dead process,
    nothing on stdout. The same string arrives from a filesystem name decoded
    with ``surrogateescape``.
    """
    client.send_raw(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        b'{"name":"x80_cpm_run","arguments":{"profile":"\\ud800","program":"x"}}}\n'
    )
    reply = client.read()
    assert reply["id"] == 1
    assert reply["error"]["code"] == -32602
    # and the connection survives, which is the whole point
    assert "tools" in client.call("tools/list", {}, 2)["result"]


def test_encode_never_raises_on_a_surrogate():
    rpc = JsonRpcServer(ServerIdentity(), {})
    frame = rpc.encode({"jsonrpc": "2.0", "id": 1, "result": {"x": "\ud800\udfff"}})
    assert frame.endswith(b"\n")
    assert b"\\ud800" in frame          # emitted as the escape the client sent
    json.loads(frame)                    # and it parses back


# ---------------------------------------------------------------------------
# 2. NaN / Infinity used to be emitted verbatim
# ---------------------------------------------------------------------------

def test_nan_id_does_not_put_a_non_json_token_on_the_wire(client):
    """Measured before the fix: ``{"jsonrpc":"2.0","id":NaN,"result":...}``.

    Python's ``json`` accepts and emits ``NaN``/``Infinity``; RFC 8259 defines
    neither, so Go, Rust, Jackson and ``JSON.parse`` all reject the line. The
    id is lost and the client cannot resynchronise. Now the request is refused
    at the door with a parse error.
    """
    client.send_raw(b'{"jsonrpc":"2.0","id":NaN,"method":"tools/list","params":{}}\n')
    reply = client.read()               # Client.read() rejects the tokens itself
    assert reply["error"]["code"] == -32700
    assert reply["id"] is None
    assert "tools" in client.call("tools/list", {}, 2)["result"]


def test_encode_refuses_a_non_finite_number():
    rpc = JsonRpcServer(ServerIdentity(), {})
    with pytest.raises(ValueError):
        rpc.encode({"jsonrpc": "2.0", "id": 1, "result": {"x": float("inf")}})


# ---------------------------------------------------------------------------
# 3. stdout carries JSON-RPC and nothing else, from every channel
# ---------------------------------------------------------------------------

_POLLUTER = '''
import os, subprocess, sys
sys.path.insert(0, {src!r})
import eightymcp.tools as T
def poisoned(ctx):
    print("STRAY-print")
    sys.stdout.write("STRAY-stdout-write\\n"); sys.stdout.flush()
    sys.__stdout__.write("STRAY-dunder\\n"); sys.__stdout__.flush()
    os.write(1, b"STRAY-fd1\\n")
    subprocess.run(["/bin/echo", "STRAY-subprocess"])
    raise RuntimeError("STRAY-traceback")
T.HANDLERS["x80_profiles"] = poisoned
from eightymcp.cli import serve
sys.exit(serve())
'''


def test_no_channel_can_reach_stdout_but_the_protocol(tmp_path):
    """print, sys.stdout, sys.__stdout__, fd 1, an inherited fd 1, a traceback.

    SPEC.md 1.4: "Never let a subprocess write to the JSON-RPC channel." All
    six land on stderr because ``claim_stdout`` dups fd 1 away and points the
    old fd 1 at fd 2, and the client stays in sync across the failure.
    """
    script = tmp_path / "polluter.py"
    script.write_text(_POLLUTER.format(src=SRC))
    p = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, PYTHONPATH=SRC),
    )
    try:
        p.stdin.write(
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"x80_profiles","arguments":{}}}\n'
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        )
        p.stdin.flush()
        first = json.loads(p.stdout.readline())
        second = json.loads(p.stdout.readline())
    finally:
        out_rest, err = p.communicate(timeout=20)
    assert first["id"] == 1 and first["result"]["isError"] is True
    assert second["id"] == 2 and "tools" in second["result"]
    assert out_rest == b""
    text = err.decode("utf-8", "replace")
    for stray in ("STRAY-print", "STRAY-stdout-write", "STRAY-dunder",
                  "STRAY-fd1", "STRAY-subprocess", "STRAY-traceback"):
        assert stray in text, stray


# ---------------------------------------------------------------------------
# 4. The deadline kills what the guest forked, not just the guest
# ---------------------------------------------------------------------------

def _alive(tag: str) -> int:
    out = subprocess.run(["ps", "-Ao", "command"], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if tag in line and "ps -Ao" not in line)


def test_deadline_kills_a_forking_guest_and_its_children():
    """SPEC.md 5.5: "Every launch is externally deadlined and killed by process
    group." Four spinning descendants, one killpg, no survivors."""
    tag = "80mcp-fork-victim"
    with Sandbox() as sb:
        script = sb.guest_dir / f"{tag}.sh"
        script.write_text(
            "#!/bin/sh\n"
            "spin() { while :; do :; done; }\n"
            "for i in 1 2 3; do ( spin ) & done\n"
            "( sh -c 'while :; do :; done' ) &\n"
            "spin\n"
        )
        os.chmod(script, 0o700)
        result = sb.exec(["/bin/sh", f"./{tag}.sh"], timeout_ms=800)
    assert result.timed_out is True
    assert result.rc is None
    assert 700 <= result.wall_ms <= 3000
    time.sleep(0.4)
    assert _alive(tag) == 0


# ---------------------------------------------------------------------------
# 5. A missing cwd is a cwd problem, not a missing binary
# ---------------------------------------------------------------------------

def test_missing_cwd_is_not_reported_as_a_missing_executable(tmp_path):
    """Measured before the fix: ``backend_missing: no such executable: /bin/sh``
    for a ``reference.cwd`` that did not exist. Popen raises FileNotFoundError
    for both, and the agent was told to fix a binary that was fine."""
    with Sandbox() as sb:
        with pytest.raises(ToolExecutionError) as excinfo:
            sb.exec(["/bin/sh", "-c", "true"], cwd=tmp_path / "nope", timeout_ms=2000)
    body = excinfo.value.err.to_json()
    assert body["error"] == "bad_argument"
    assert body["argument"] == "cwd"
    assert "not a directory" in body["reason"]


# ---------------------------------------------------------------------------
# 6. to_guest was a host-filesystem existence oracle
# ---------------------------------------------------------------------------

def _marker_sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "sbx"
    (root / "guest").mkdir(parents=True)
    (root / SANDBOX_MARKER).write_text(
        json.dumps({"schema": 1, "created_ns": time.time_ns(), "created": [], "state": {}})
    )
    return root


def _files_call(root: Path, guest_name: str) -> dict:
    server = build_server()
    tool = server.registry.get("x80_files")
    from eightymcp.server import CallContext, validate_and_fill
    args = validate_and_fill(tool.input_schema, {
        "op": "to_guest", "sandbox": str(root),
        "files": [{"guest_name": guest_name, "content_b64": "QQ=="}],
    })
    ctx = CallContext(tool="x80_files", arguments=args, connection=server.rpc.connection)
    try:
        return tool.handler(ctx).structured
    except ToolExecutionError as exc:
        return exc.err.to_json()


def test_to_guest_gives_the_same_answer_for_present_and_absent_host_paths(tmp_path):
    """Measured before the fix: ``../../..../etc/passwd`` came back as
    ``skipped: a file already exists``, and a path that did not exist came back
    as the containment error. Two answers, one bit of the host filesystem
    leaked per call. The collision check now runs on the resolved path."""
    root = _marker_sandbox(tmp_path)
    up = "../" * (str(root).count("/") + 2)
    present = _files_call(root, up + "etc/passwd")
    absent = _files_call(root, up + "etc/definitely-not-there")
    assert present["error"] == "bad_argument"
    assert absent["error"] == "bad_argument"
    assert present["reason"].split(":")[0] == absent["reason"].split(":")[0]
    assert not (Path("/etc") / "passwd").with_name("passwd").is_symlink()  # nothing written


# ---------------------------------------------------------------------------
# 7. A corrupt marker is the caller's problem, not a traceback
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"created": 5, "state": {}},
    {"created": [], "state": "not a dict"},
    {"created": [], "state": {}, "last_call_ns": "soon"},
    {"created": [], "state": {"a": "b"}},
    {},
])
def test_a_corrupt_sandbox_marker_does_not_raise(tmp_path, body):
    """``x80_files`` is pointed at a directory the caller names, so every field
    of the marker is hostile input. Measured before the fix: three of these
    five came back as ``internal_error`` with a traceback on stderr."""
    root = tmp_path / "sbx"
    (root / "guest").mkdir(parents=True)
    (root / "guest" / "A.TXT").write_bytes(b"x")
    (root / SANDBOX_MARKER).write_text(json.dumps({"schema": 1, **body}))

    server = build_server()
    tool = server.registry.get("x80_files")
    from eightymcp.server import CallContext, validate_and_fill
    args = validate_and_fill(tool.input_schema, {"op": "list", "sandbox": str(root), "since": "start"})
    ctx = CallContext(tool="x80_files", arguments=args, connection=server.rpc.connection)
    out = tool.handler(ctx)
    assert out.is_error is False
    assert [f["guest_name"] for f in out.structured["files"]] == ["A.TXT"]


# ---------------------------------------------------------------------------
# 8. Invariant 1 covers the probe path too
# ---------------------------------------------------------------------------

def test_probe_subprocesses_do_not_see_the_developers_home():
    """SPEC.md 5.4 Invariant 1 / Appendix B item 1 are about every launch.

    ``x80_profiles`` is ``readOnlyHint:true`` and shells out to
    ``<backend> --version``; before the fix that child got the developer's real
    ``$HOME``, which is exactly what ``romwbw_emu``'s legacy-NVRAM migration
    (``romwbw_emu.cc:79-82``) reads and writes.
    """
    from eightymcp import profiles

    _rc, out, _err = profiles._run(["/bin/sh", "-c", "echo $HOME; echo $XDG_CONFIG_HOME"])
    real = os.path.expanduser("~")
    assert real not in out
    assert "80mcp-probe-" in out


# ---------------------------------------------------------------------------
# 9. A FIFO or device as host_path used to wedge the whole connection
# ---------------------------------------------------------------------------

def test_staging_a_fifo_is_refused_instead_of_blocking(tmp_path):
    """Measured before the fix: no reply, ever, and no later request read.

    ``Path.read_bytes`` blocks in ``open(2)`` on a FIFO with no writer. Staging
    runs before ``Sandbox.exec``, so ``timeout_ms`` never applies to it, and
    the request loop is serial, so the connection dies with the call. Worse
    than a desync: the client is given nothing at all to react to.
    """
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    from eightymcp.types import FileIn

    with Sandbox() as sb:
        with pytest.raises(ToolExecutionError) as excinfo:
            sb.stage(FileIn(guest_name="A.TXT", host_path=str(fifo)))
    body = excinfo.value.err.to_json()
    assert body["error"] == "bad_argument"
    assert body["argument"] == "files_in[].host_path"
    assert "FIFO" in body["reason"]


@pytest.mark.parametrize("device", ["/dev/zero", "/dev/random"])
def test_staging_a_character_device_is_refused(device):
    """``/dev/zero`` is the same hazard with allocation instead of blocking."""
    from eightymcp.types import FileIn

    if not Path(device).exists():
        pytest.skip(f"{device} is not present on this host")
    with Sandbox() as sb:
        with pytest.raises(ToolExecutionError) as excinfo:
            sb.stage(FileIn(guest_name="A.TXT", host_path=device))
    assert "character device" in excinfo.value.err.to_json()["reason"]


def test_staging_a_program_that_is_a_fifo_is_refused(tmp_path):
    fifo = tmp_path / "prog.com"
    os.mkfifo(fifo)
    with Sandbox() as sb:
        with pytest.raises(ToolExecutionError) as excinfo:
            sb.stage_program(fifo)
    assert excinfo.value.err.to_json()["argument"] == "program"


def test_staging_an_oversized_file_is_refused_by_size(tmp_path, monkeypatch):
    from eightymcp import sandbox as sandbox_mod
    from eightymcp.types import FileIn

    monkeypatch.setattr(sandbox_mod, "MAX_STAGE_BYTES", 8)
    big = tmp_path / "big.bin"
    big.write_bytes(b"0123456789")
    with Sandbox() as sb:
        with pytest.raises(ToolExecutionError) as excinfo:
            sb.stage(FileIn(guest_name="A.BIN", host_path=str(big)))
    assert "staging ceiling" in excinfo.value.err.to_json()["reason"]
