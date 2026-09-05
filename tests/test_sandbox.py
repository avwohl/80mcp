"""Sandbox tests: the four launch invariants, the deadline, the group kill.

The two that matter most are :func:`test_deadline_kills_a_spinning_child` and
:func:`test_group_kill_reaches_a_forked_grandchild`, and they are run against
``/bin/sh`` rather than a mock. SPEC.md 5.5: "Nothing in the family has a
timeout ... Every launch is externally deadlined and killed by process group."
A mock cannot demonstrate that a grandchild died, and the grandchild is the
whole reason the kill is by group.

The rest cover SPEC.md 5.4 Invariant 1 (hermetic launch is four things, and
Appendix B item 1 says every design in the round got it wrong), SPEC.md 5.5's
LST:/PUN: rule (Appendix B item 2), and the before/after manifest that answers
"what did this run make?".
"""

from __future__ import annotations

import base64
import os
import signal
import stat
import time
from pathlib import Path

import pytest

from eightymcp.mbp import SandboxProtocol
from eightymcp.sandbox import (
    DEFAULT_PATH,
    KILL_GRACE_MS,
    PROTECTED_ENV,
    Collect,
    Sandbox,
    sha256_bytes,
)
from eightymcp.types import (
    FileIn,
    FileMode,
    ReturnContent,
    ToolExecutionError,
)

SH = "/bin/sh"


@pytest.fixture()
def sb():
    box = Sandbox()
    try:
        yield box
    finally:
        box.keep = False
        box.cleanup()


def _reaped(pid: int, within_s: float = 3.0) -> bool:
    """Poll until ``pid`` no longer exists, or give up."""
    end = time.monotonic() + within_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover
            return False
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# The deadline and the process-group kill
# ---------------------------------------------------------------------------

def test_deadline_kills_a_spinning_child(sb):
    """SPEC.md 5.5: the supervisor's clock is the only clock."""
    r = sb.exec([SH, "-c", "while :; do :; done"], timeout_ms=400)
    assert r.timed_out is True
    assert r.rc is None, "SPEC.md ExecResult: rc is None when the deadline fired"
    assert r.signal in (int(signal.SIGTERM), int(signal.SIGKILL))
    assert r.ok is False
    assert r.deadline_ms == 400
    assert 400 <= r.wall_ms < 3000, r.wall_ms


def test_sigterm_ignored_still_dies_by_sigkill(sb):
    """A backend mid-instruction has nothing to clean up and may not honour
    SIGTERM at all. The escalation after KILL_GRACE_MS is what guarantees the
    tool call returns."""
    r = sb.exec(
        [SH, "-c", 'trap "" TERM; while :; do :; done'],
        timeout_ms=300,
    )
    assert r.timed_out is True
    assert r.signal == int(signal.SIGKILL)
    assert r.wall_ms >= 300


def test_group_kill_reaches_a_forked_grandchild(sb):
    """The point of ``start_new_session=True`` plus ``killpg``.

    The direct child exits immediately. Its background grandchild spins and
    still holds the stdout and stderr pipes, so the run does not end on its
    own -- the deadline has to end it, and killing the direct pid would not
    have been enough because the direct pid is already gone.
    """
    script = 'sh -c "while :; do :; done" & echo $! > child.pid'
    r = sb.exec([SH, "-c", script], timeout_ms=500)

    pid_file = sb.guest_dir / "child.pid"
    assert pid_file.exists(), "the direct child never got as far as forking"
    grandchild = int(pid_file.read_text().strip())

    assert r.timed_out is True, (
        "the direct child exited but the grandchild held the pipes open; "
        "the run must end on the deadline, not on the child's exit"
    )
    assert _reaped(grandchild), (
        f"grandchild {grandchild} survived the deadline: the kill went to the "
        f"pid, not to the process group"
    )


def test_a_fast_command_is_not_killed(sb):
    r = sb.exec([SH, "-c", "exit 3"], timeout_ms=5000)
    assert r.timed_out is False
    assert r.rc == 3
    assert r.signal is None
    assert r.ok is False
    r2 = sb.exec([SH, "-c", "true"], timeout_ms=5000)
    assert r2.ok is True


def test_partial_output_survives_the_kill(sb):
    r = sb.exec(
        [SH, "-c", 'echo before-the-spin; echo diag >&2; while :; do :; done'],
        timeout_ms=400,
    )
    assert r.timed_out is True
    assert b"before-the-spin\n" in r.stdout
    assert b"diag\n" in r.stderr


def test_cleanup_is_safe_after_a_killed_run(sb):
    r = sb.exec([SH, "-c", "sh -c 'while :; do :; done' & echo x > f.txt"], timeout_ms=300)
    assert r.timed_out is True
    root = sb.root
    sb.cleanup()
    assert not root.exists()
    sb.cleanup()  # idempotent


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------

def test_stdout_and_stderr_are_separate_pipes(sb):
    """SPEC.md 5.2: all three backends separate guest stdout from diagnostics
    stderr, and every ``exit_reason`` is parsed from a stderr sentinel. Merging
    them would destroy that."""
    r = sb.exec([SH, "-c", "echo to-stdout; echo to-stderr >&2"], timeout_ms=5000)
    assert r.stdout == b"to-stdout\n"
    assert r.stderr == b"to-stderr\n"


def test_stdin_from_a_pipe(sb):
    r = sb.exec([SH, "-c", "cat"], stdin=b"hello\nworld\n", timeout_ms=5000)
    assert r.rc == 0
    assert r.stdout == b"hello\nworld\n"


def test_empty_stdin_is_devnull_not_a_hanging_pipe(sb):
    """SPEC.md 7.1 launches cpmemu with stdin from /dev/null. An open, idle
    pipe is exactly the case cpmemu's instruction watchdog does not fire on."""
    r = sb.exec([SH, "-c", "cat"], timeout_ms=3000)
    assert r.timed_out is False
    assert r.rc == 0
    assert r.stdout == b""


def test_large_stdin_does_not_deadlock(sb):
    payload = b"x" * (4 * 1024 * 1024)
    r = sb.exec([SH, "-c", "wc -c"], stdin=payload, timeout_ms=20000)
    assert r.timed_out is False
    assert int(r.stdout.split()[0]) == len(payload)


def test_a_child_that_closes_its_pipes_and_spins_still_times_out(sb):
    r = sb.exec(
        [SH, "-c", "exec >/dev/null 2>&1; while :; do :; done"],
        timeout_ms=400,
    )
    assert r.timed_out is True


# ---------------------------------------------------------------------------
# Invariant 1: hermetic launch
# ---------------------------------------------------------------------------

def test_home_and_xdg_config_home_point_into_the_sandbox(sb):
    r = sb.exec(
        [SH, "-c", 'echo "$HOME"; echo "$XDG_CONFIG_HOME"'], timeout_ms=5000
    )
    lines = r.stdout.decode().split()
    assert lines == [str(sb.home_dir), str(sb.xdg_dir)]
    assert sb.home_dir.is_dir() and sb.xdg_dir.is_dir()


def test_the_host_environment_is_scrubbed(monkeypatch, sb):
    monkeypatch.setenv("SOME_DEVELOPER_SETTING", "leaked")
    r = sb.exec([SH, "-c", 'echo "[${SOME_DEVELOPER_SETTING-unset}]"'], timeout_ms=5000)
    assert r.stdout == b"[unset]\n"


def test_path_and_dyld_survive_the_scrub(monkeypatch, sb):
    """SPEC.md 6.4 records mpm2_emu needing DYLD_LIBRARY_PATH for
    libqkz80.4.dylib. Which binary runs and how it links is an installation
    fact, not guest configuration."""
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/opt/qkz80/lib")
    env = sb.base_env()
    assert env["DYLD_LIBRARY_PATH"] == "/opt/qkz80/lib"
    assert env["PATH"] == os.environ.get("PATH", DEFAULT_PATH)


def test_env_adds_but_cannot_drop_home_or_xdg(sb):
    """The contract in eightymcp.mbp.SandboxProtocol.exec: ``env`` is added to
    the hermetic base, and a backend must not be able to reintroduce the
    measured NVRAM leak by handing back the developer's real $HOME."""
    r = sb.exec(
        [SH, "-c", 'echo "$HOME"; echo "$XDG_CONFIG_HOME"; echo "$EXTRA"'],
        env={"HOME": "/Users/wohl", "XDG_CONFIG_HOME": "/Users/wohl/.config", "EXTRA": "ok"},
        timeout_ms=5000,
    )
    out = r.stdout.decode().split()
    assert out == [str(sb.home_dir), str(sb.xdg_dir), "ok"]
    assert set(PROTECTED_ENV) == {"HOME", "XDG_CONFIG_HOME"}


def test_build_env_reasserts_the_protected_keys(sb):
    env = sb.build_env({"HOME": "/etc"})
    assert env["HOME"] == str(sb.home_dir)


def test_cwd_defaults_to_the_guest_directory(sb):
    r = sb.exec([SH, "-c", "pwd"], timeout_ms=5000)
    assert Path(r.stdout.decode().strip()).resolve() == sb.guest_dir
    assert r.cwd == str(sb.guest_dir)


def test_tmpdir_is_inside_the_sandbox(sb):
    r = sb.exec([SH, "-c", 'echo "$TMPDIR"'], timeout_ms=5000)
    assert r.stdout.decode().strip() == str(sb.tmp_dir)


# ---------------------------------------------------------------------------
# Launch failures are structured, not tracebacks
# ---------------------------------------------------------------------------

def test_a_missing_binary_is_backend_missing_not_a_crash(sb):
    with pytest.raises(ToolExecutionError) as e:
        sb.exec(["/nonexistent/cpmemu"], timeout_ms=1000)
    body = e.value.to_json()
    assert body["error"] == "backend_missing"
    assert body["backend"] == "cpmemu"
    assert "x80_profiles" in body["hint"]


def test_a_non_executable_binary_is_backend_missing(sb):
    p = sb.root / "notexec"
    p.write_text("#!/bin/sh\n")
    os.chmod(p, 0o600)
    with pytest.raises(ToolExecutionError) as e:
        sb.exec([str(p)], timeout_ms=1000)
    assert e.value.to_json()["error"] == "backend_missing"


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def test_stage_from_host_path_is_byte_exact(sb, tmp_path):
    src = tmp_path / "method9.arc"
    blob = bytes(range(256)) * 4
    src.write_bytes(blob)
    p = sb.stage(FileIn(guest_name="M9.ARC", host_path=str(src)))
    assert p == sb.guest_dir / "M9.ARC"
    assert p.read_bytes() == blob


def test_stage_from_content_b64(sb):
    p = sb.stage(FileIn(guest_name="IN.TXT", content_b64=base64.b64encode(b"\x00\xff").decode()))
    assert p.read_bytes() == b"\x00\xff"


def test_stage_text_mode_writes_crlf_and_is_idempotent(sb):
    """SPEC.md 7.1's synthesized cfg carries ``eol_convert = false``, so
    nothing downstream converts and staging is the only place it can happen."""
    lf = sb.stage(FileIn(guest_name="A.TXT", content_b64=base64.b64encode(b"a\nb\n").decode(), mode=FileMode.TEXT))
    assert lf.read_bytes() == b"a\r\nb\r\n"
    crlf = sb.stage(FileIn(guest_name="B.TXT", content_b64=base64.b64encode(b"a\r\nb\r\n").decode(), mode=FileMode.TEXT))
    assert crlf.read_bytes() == b"a\r\nb\r\n"
    binary = sb.stage(FileIn(guest_name="C.BIN", content_b64=base64.b64encode(b"a\nb\n").decode()))
    assert binary.read_bytes() == b"a\nb\n"


def test_stage_refuses_to_escape_the_sandbox(sb):
    for bad in ("../escape.txt", "/etc/passwd", "a/../../escape.txt"):
        with pytest.raises(ToolExecutionError) as e:
            sb.stage(FileIn(guest_name=bad, content_b64="AA=="))
        assert e.value.to_json()["error"] == "bad_argument"


def test_stage_missing_host_path_is_actionable(sb):
    with pytest.raises(ToolExecutionError) as e:
        sb.stage(FileIn(guest_name="X.TXT", host_path="/no/such/file.txt"))
    body = e.value.to_json()
    assert body["error"] == "bad_argument"
    assert body["argument"] == "files_in[].host_path"
    assert body["guest_name"] == "X.TXT"


def test_stage_bad_base64_is_actionable(sb):
    with pytest.raises(ToolExecutionError) as e:
        sb.stage(FileIn(guest_name="X.TXT", content_b64="not base64!!"))
    assert e.value.to_json()["argument"] == "files_in[].content_b64"


def test_stage_program_keeps_the_executable_bit(sb, tmp_path):
    src = tmp_path / "SORT.EXE"
    src.write_bytes(b"MZ\x00\x00")
    p = sb.stage_program(str(src))
    assert p == sb.guest_dir / "SORT.EXE"
    assert os.stat(p).st_mode & stat.S_IXUSR
    r = sb.exec([SH, "-c", "ls SORT.EXE"], timeout_ms=5000)
    assert r.stdout == b"SORT.EXE\n", "the program must be nameable relatively (SPEC.md 5.5)"


def test_write_text_lands_in_the_root_not_the_guest_dir(sb):
    cfg = sb.write_text(
        "run.cfg",
        f"program = /x/80un.com\ncd = {sb.guest_dir}\ndefault_mode = binary\n",
    )
    assert cfg == sb.root / "run.cfg"
    sb.snapshot()
    assert sb.collect() == []


# ---------------------------------------------------------------------------
# The before/after manifest
# ---------------------------------------------------------------------------

def test_snapshot_excludes_staged_inputs_and_reports_created_files(sb):
    sb.stage(FileIn(guest_name="M9.ARC", content_b64=base64.b64encode(b"input").decode()))
    sb.snapshot()
    r = sb.exec([SH, "-c", "printf out > TIME2.ASM; printf yy > ZTIM.CPM"], timeout_ms=5000)
    assert r.rc == 0
    names = [p.name for p in sb.created_since_snapshot()]
    assert names == ["TIME2.ASM", "ZTIM.CPM"]


def test_manifest_carries_size_and_sha256(sb):
    sb.snapshot()
    sb.exec([SH, "-c", "printf 'hello' > OUT.TXT"], timeout_ms=5000)
    out = sb.collect()
    assert len(out) == 1
    f = out[0]
    assert f.guest_name == "OUT.TXT"
    assert f.bytes == 5
    assert f.sha256 == sha256_bytes(b"hello")
    assert base64.b64decode(f.content_b64) == b"hello"
    assert f.to_json()["bytes"] == 5


def test_manifest_reports_a_modified_input_too(sb):
    sb.stage(FileIn(guest_name="IN.TXT", content_b64=base64.b64encode(b"one").decode()))
    sb.snapshot()
    time.sleep(0.01)
    sb.exec([SH, "-c", "printf 'two-longer' > IN.TXT"], timeout_ms=5000)
    assert [f.guest_name for f in sb.collect()] == ["IN.TXT"]


def test_guest_name_mapping_reports_host_name_separately(sb):
    """SPEC.md 6.4: cpmemu lowercases on create, ``B5-TIME.INF`` lands as
    ``b5-time.inf``, and a caller asserting against host names will be wrong."""
    sb.snapshot()
    sb.exec([SH, "-c", "printf x > b5-time.inf"], timeout_ms=5000)
    out = sb.collect(guest_name=str.upper)
    assert out[0].guest_name == "B5-TIME.INF"
    assert out[0].host_name == "b5-time.inf"
    assert out[0].to_json()["host_name"] == "b5-time.inf"


def test_host_name_is_omitted_when_it_matches(sb):
    sb.snapshot()
    sb.exec([SH, "-c", "printf x > SAME.TXT"], timeout_ms=5000)
    j = sb.collect(guest_name=str.upper)[0].to_json()
    assert "host_name" not in j


def test_collect_exclude_is_case_insensitive(sb):
    sb.snapshot()
    sb.exec([SH, "-c", "printf a > keep.txt; printf b > drop.tmp"], timeout_ms=5000)
    out = sb.collect(Collect(exclude=("*.TMP",)))
    assert [f.guest_name for f in out] == ["keep.txt"]


def test_collect_return_content_modes(sb):
    sb.snapshot()
    sb.exec([SH, "-c", "dd if=/dev/zero of=BIG.BIN bs=1024 count=8 2>/dev/null"], timeout_ms=10000)
    never = sb.collect(Collect(return_content=ReturnContent.NEVER))
    assert never[0].content_b64 is None and never[0].bytes == 8192
    under = sb.collect(Collect(return_content=ReturnContent.INLINE_IF_UNDER_KB, max_kb=4))
    assert under[0].content_b64 is None, "8 KB is over a 4 KB max_kb"
    over = sb.collect(Collect(return_content=ReturnContent.INLINE_IF_UNDER_KB, max_kb=64))
    assert over[0].content_b64 is not None
    always = sb.collect(Collect(return_content=ReturnContent.ALWAYS, max_kb=1))
    assert always[0].content_b64 is not None


def test_collect_from_json_ignores_normalize(sb):
    """``normalize`` rides in x80_cpm_run's inline Collect and belongs to
    eightymcp.normalize: SPEC.md 6.4 scopes it to "the REPORTED sha256 and
    content only, never to the file on disk"."""
    c = Collect.from_json(
        {"return_content": "never", "max_kb": 8, "exclude": ["*.bak"],
         "normalize": ["lowercase_names", "pad_to_record"]}
    )
    assert c.return_content is ReturnContent.NEVER
    assert c.max_kb == 8
    assert c.exclude == ("*.bak",)


# ---------------------------------------------------------------------------
# LST: and PUN:  (SPEC.md 5.5, Appendix B item 2)
# ---------------------------------------------------------------------------

def test_lst_and_pun_are_real_files_never_devnull(sb):
    """Appendix B item 2: empty emits ``Warning: Cannot open printer file '':
    No such file or directory`` every run; /dev/null silences the warning and
    silently destroys the byte stream."""
    for p in (sb.lst_path, sb.pun_path):
        assert p.exists() and p.is_file()
        assert str(p) not in ("", os.devnull)
        assert not str(p).startswith("/dev/")


def test_untouched_streams_report_bytes_zero(sb):
    assert sb.list_output().to_json() == {"bytes": 0}
    assert sb.punch_output().to_json() == {"bytes": 0}


def test_a_written_list_device_is_surfaced(sb):
    sb.lst_path.write_bytes(b"PAGE 1\r\n")
    s = sb.list_output()
    assert s.bytes == 8
    assert s.sha256 == sha256_bytes(b"PAGE 1\r\n")
    assert base64.b64decode(s.content_b64) == b"PAGE 1\r\n"


# ---------------------------------------------------------------------------
# keep_sandbox and cleanup
# ---------------------------------------------------------------------------

def test_keep_sandbox_false_removes_the_tree_and_reports_null():
    with Sandbox() as box:
        root = box.root
        assert box.reported_path is None
    assert not root.exists()


def test_keep_sandbox_true_leaves_the_tree_and_reports_the_path():
    box = Sandbox(keep=True)
    root = box.root
    try:
        with box:
            assert box.reported_path == str(root)
        assert root.exists(), "keep_sandbox:true must survive the context manager"
    finally:
        box.keep = False
        box._cleaned = False
        box.cleanup()
    assert not root.exists()


def test_cleanup_removes_a_read_only_file(sb):
    p = sb.guest_dir / "ro.txt"
    p.write_bytes(b"x")
    os.chmod(p, 0o400)
    os.chmod(sb.guest_dir, 0o500)
    root = sb.root
    sb.cleanup()
    assert not root.exists()


def test_cleanup_kills_a_group_that_outlived_its_exec():
    """A grandchild that escaped by exiting the exec early is still killed when
    the sandbox goes away: nothing outlives the tool call."""
    box = Sandbox()
    r = box.exec([SH, "-c", "sh -c 'while :; do :; done' & echo $! > c.pid"], timeout_ms=300)
    assert r.timed_out is True
    box.cleanup()
    assert not box.root.exists()


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

def test_sandbox_satisfies_the_mbp_protocol(sb):
    assert isinstance(sb, SandboxProtocol)
    for name in ("root", "guest_dir", "home_dir", "xdg_dir", "lst_path", "pun_path"):
        assert isinstance(getattr(sb, name), Path)
    for name in ("stage", "snapshot", "created_since_snapshot", "write_text", "exec"):
        assert callable(getattr(sb, name))


def test_kill_grace_is_bounded(sb):
    assert 0 < KILL_GRACE_MS <= 1000
