"""Tests for the dosiz one-shot adapter.

Two halves. The parser tests run everywhere and assert against transcripts
measured from this build of ``dosiz`` (each one names how it was produced). The
live tests need the binary and the fixtures from ``dosiz/tests`` and skip
cleanly without them -- an absent backend is a profile reporting
``ready:false``, never a failure (SPEC.md 5.1).

Point the live half at a build with::

    EIGHTYMCP_DOSIZ=/path/to/build/dosiz \\
    EIGHTYMCP_DOSIZ_TESTS=/path/to/dosiz/tests \\
    pytest tests/test_dosiz.py
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from eightymcp.backends import dosiz as dz
from eightymcp.mbp import (
    BackendFailure,
    BootSpec,
    IdleRequest,
    RecvRequest,
    RunRequest,
    SandboxProtocol,
    SendRequest,
    StopRequest,
    UnsupportedOp,
)
from eightymcp.types import (
    DOSIZ_STDERR_NOISE,
    AdapterShape,
    DefaultMode,
    ExecResult,
    ExitCodeMeaning,
    ExitReason,
    Family,
    FileIn,
    Tier,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A sandbox test double
# ---------------------------------------------------------------------------

class StubSandbox:
    """The slice of :mod:`eightymcp.sandbox` this backend touches.

    Written here because ``sandbox.py`` belongs to another module and the
    backend is specified against :class:`~eightymcp.mbp.SandboxProtocol`, not
    against a class. It implements the two things the protocol says ``exec``
    owns -- a wall-clock deadline and a process-group kill -- because the dosiz
    timeout test is meaningless without them: measured, a 2-byte ``EB FE``
    spin loop runs until SIGKILL.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.guest_dir = root / "guest"
        self.home_dir = root / "home"
        self.xdg_dir = root / "xdg"
        self.lst_path = root / ".lst"
        self.pun_path = root / ".pun"
        for d in (self.guest_dir, self.home_dir, self.xdg_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._snapshot: dict[Path, tuple[int, int]] = {}
        self.last_env: dict[str, str] = {}

    # -- staging -----------------------------------------------------------

    def stage(self, f: FileIn) -> Path:
        target = self.guest_dir / f.guest_name
        if f.host_path is not None:
            shutil.copy2(f.host_path, target)
        else:
            import base64
            target.write_bytes(base64.b64decode(f.content_b64 or ""))
        return target

    def _walk(self) -> dict[Path, tuple[int, int]]:
        return {
            p: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in self.guest_dir.rglob("*")
            if p.is_file()
        }

    def snapshot(self) -> None:
        self._snapshot = self._walk()

    def created_since_snapshot(self) -> list[Path]:
        now = self._walk()
        return sorted(p for p, meta in now.items() if self._snapshot.get(p) != meta)

    def write_text(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # -- exec --------------------------------------------------------------

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes = b"",
        timeout_ms: int = 10000,
    ) -> ExecResult:
        base = {
            "HOME": str(self.home_dir),
            "XDG_CONFIG_HOME": str(self.xdg_dir),
            "TMPDIR": str(self.root),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LC_ALL": "C",
        }
        full = dict(base)
        for key, value in (env or {}).items():
            # Invariant 1: `env` adds to the hermetic base; it cannot drop it.
            if key in ("HOME", "XDG_CONFIG_HOME"):
                continue
            full[key] = value
        self.last_env = full

        run_cwd = str(cwd) if cwd is not None else str(self.guest_dir)
        started = time.monotonic()
        proc = subprocess.Popen(
            list(argv),
            cwd=run_cwd,
            env=full,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,          # its own process group
        )
        timed_out = False
        try:
            out, err = proc.communicate(stdin, timeout=timeout_ms / 1000.0)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            out, err = proc.communicate()
        wall_ms = int((time.monotonic() - started) * 1000)

        rc: int | None = proc.returncode
        sig: int | None = None
        if rc is not None and rc < 0:
            sig, rc = -rc, None
        if timed_out:
            rc = None
        return ExecResult(
            argv=list(argv),
            cwd=run_cwd,
            rc=rc,
            signal=sig,
            stdout=out,
            stderr=err,
            wall_ms=wall_ms,
            timed_out=timed_out,
            deadline_ms=timeout_ms,
        )


@pytest.fixture()
def sandbox(tmp_path: Path) -> StubSandbox:
    return StubSandbox(tmp_path / "sb")


def test_stub_sandbox_satisfies_the_protocol(sandbox: StubSandbox) -> None:
    assert isinstance(sandbox, SandboxProtocol)


# ---------------------------------------------------------------------------
# Measured transcripts
# ---------------------------------------------------------------------------

#: evidence/t4/err.txt, committed in this repo: the whole of dosiz's stderr for
#: a clean SORT.EXE run.
NOISE_ONLY = (REPO_ROOT / "evidence" / "t4" / "err.txt").read_text()

#: Measured. Hand-assembled UNIMP.COM, bytes
#: `B4 7F CD 21  B4 6B CD 21  B8 03 4C CD 21` (AH=7Fh, AH=6Bh, then
#: INT 21h AH=4Ch AL=03), run through this build of dosiz.
UNIMP_STDERR = (
    "dosiz: ethernet/slirp backend unavailable; INT 0x60 packet driver will "
    "accept guest calls but RX/TX will no-op.\n"
    "dosiz: Crynwr pktdrv installed at INT 60h, stub at 0060:0000\n"
    "dosiz: unimplemented INT 21h AH=7Fh (AL=00h BX=0000h CX=0000h DX=0000h) "
    "-- returning invalid-function, program continues\n"
    "dosiz: unimplemented INT 21h AH=6Bh (AL=01h BX=0000h CX=0000h DX=0000h) "
    "-- returning invalid-function, program continues\n"
)

#: Measured. dosiz/tests/gen_le_min.py with the exit stub at page[5:7] replaced
#: by `0F 0B` (UD2), run through this build; host rc was 1.
LE_FAULT_STDERR = (
    "dosiz: LE entering PM: CS=0024:EIP=00020010 SS=001c:ESP=00021040 "
    "DS=001c ES=002c D=1\n"
    "dosiz: LE client exception 0x06 (#UD  invalid opcode) -- terminating.\n"
    "  fault at CS:EIP = 0024:00020010  EFLAGS = 00000000\n"
    "  EAX=00000000 EBX=00000000 ECX=00000000 EDX=00000000\n"
    "  ESI=00000000 EDI=00000000 EBP=00000000\n"
    "  DS=001c ES=002c FS=0000 GS=0000\n"
)

#: Measured. `dosiz NOSUCH.EXE`, host rc 1 -- the rc a guest also gets from
#: INT 21h AH=4Ch AL=01, which is the whole reason exit_code_meaning exists.
MISSING_PROGRAM_STDERR = "dosiz: cannot find 'NOSUCH.EXE' on DOSIZ_PATH\n"

#: Measured. `dosiz /abs/path/DJ_PRINTF.exe` from a different cwd: this line is
#: on the guest's STDOUT, and the host rc is 102.
ARGV0_TRAP_STDOUT = (
    "C:\\PRIVATE\\TMP\\CLAUDE-501\\SCRATCHPAD\\DZ-PROBE\\DJ_PRINTF.EXE: "
    "can't open\n"
)


# ---------------------------------------------------------------------------
# argv[0]
# ---------------------------------------------------------------------------

def test_dos_argv0_uppercases_and_backslashes_an_absolute_path() -> None:
    # dosiz/src/bridge.cc:7109-7122. This is the string the go32 stub reopens.
    assert dz.dos_argv0("/tmp/dz/DJ_PRINTF.exe") == "C:\\TMP\\DZ\\DJ_PRINTF.EXE"


def test_dos_argv0_of_a_relative_name_stays_inside_the_cwd() -> None:
    assert dz.dos_argv0("DJ_PRINTF.exe") == "C:\\DJ_PRINTF.EXE"
    assert dz.dos_argv0("./DJ_PRINTF.exe") == "C:\\DJ_PRINTF.EXE"


def test_detect_argv0_trap_needs_both_the_rc_and_the_line() -> None:
    assert dz.detect_argv0_trap(ARGV0_TRAP_STDOUT, 102) == (
        "C:\\PRIVATE\\TMP\\CLAUDE-501\\SCRATCHPAD\\DZ-PROBE\\DJ_PRINTF.EXE"
    )
    # rc 102 alone is not the trap, and the line alone is not either.
    assert dz.detect_argv0_trap("nothing here\n", 102) is None
    assert dz.detect_argv0_trap(ARGV0_TRAP_STDOUT, 0) is None


# ---------------------------------------------------------------------------
# stderr filtering
# ---------------------------------------------------------------------------

def test_filter_noise_removes_exactly_the_two_lines_and_records_them() -> None:
    kept, removed = dz.filter_noise(NOISE_ONLY)
    assert removed == list(DOSIZ_STDERR_NOISE)
    assert kept.strip() == ""


def test_filter_noise_matches_the_whole_line_not_a_prefix() -> None:
    # A future dosiz that changes the wording must surface as an unfiltered
    # line rather than be quietly absorbed.
    drifted = DOSIZ_STDERR_NOISE[1] + " (v2)\n"
    kept, removed = dz.filter_noise(drifted)
    assert removed == []
    assert kept.strip() == drifted.strip()


def test_filter_noise_keeps_real_diagnostics(  ) -> None:
    kept, removed = dz.filter_noise(UNIMP_STDERR)
    assert removed == list(DOSIZ_STDERR_NOISE)
    assert "unimplemented INT 21h AH=7Fh" in kept
    assert "slirp" not in kept


# ---------------------------------------------------------------------------
# unimplemented INT 21h
# ---------------------------------------------------------------------------

def test_parse_unimplemented_int21_reassembles_ax_from_ah_and_al() -> None:
    calls = dz.parse_unimplemented_int21(UNIMP_STDERR)
    assert [c.ah for c in calls] == [0x7F, 0x6B]
    # The line carries AL; the schema wants AX. 0x6B<<8 | 0x01 == 0x6B01.
    assert calls[0].ax == 0x7F00
    assert calls[1].ax == 0x6B01
    assert calls[1].to_json() == {
        "ah": 0x6B, "ax": 0x6B01, "bx": 0, "cx": 0, "dx": 0
    }


def test_parse_unimplemented_int21_is_empty_on_a_clean_run() -> None:
    assert dz.parse_unimplemented_int21(NOISE_ONLY) == []


# ---------------------------------------------------------------------------
# protected-mode faults
# ---------------------------------------------------------------------------

def test_parse_pm_fault_on_the_measured_le_client_exception() -> None:
    fault = dz.parse_pm_fault(LE_FAULT_STDERR)
    assert fault is not None
    assert (fault.cs, fault.eip, fault.err) == (0x0024, 0x00020010, 0)
    assert fault.regs["vec"] == 6
    assert fault.regs["eflags"] == 0
    assert fault.regs["ds"] == 0x001C
    assert fault.regs["es"] == 0x002C


def test_parse_pm_fault_reads_an_error_code_when_the_vector_has_one() -> None:
    # dosiz/src/bridge.cc:1941-1942 prints the error code only for the vectors
    # that push one. #GP (0x0D) does.
    text = (
        "dosiz: LE client exception 0x0d (#GP  general protection) -- "
        "terminating.\n"
        "  fault at CS:EIP = 00a8:0000c3f0  EFLAGS = 00010246\n"
        "  error code = 0x00000018\n"
    )
    fault = dz.parse_pm_fault(text)
    assert fault is not None
    assert (fault.cs, fault.eip, fault.err) == (0x00A8, 0x0000C3F0, 0x18)


def test_parse_pm_fault_handles_the_16_bit_gate_form() -> None:
    # dosiz/src/bridge.cc:1954-1958.
    text = (
        "dosiz: LE client exception 0x0e (#PF  page fault) -- terminating.\n"
        "  fault at CS:IP = 0170:0abc  FLAGS = 0246\n"
        "  error code = 0x0004\n"
        "  AX=0001 BX=0002 CX=0003 DX=0004 SI=0005 DI=0006 BP=0007\n"
    )
    fault = dz.parse_pm_fault(text)
    assert fault is not None
    assert (fault.cs, fault.eip, fault.err) == (0x0170, 0x0ABC, 4)
    assert fault.regs["ax"] == 1
    assert fault.regs["bp"] == 7


def test_parse_pm_fault_handles_the_dpmi_dispatcher_forms() -> None:
    # dosiz/src/bridge.cc:2204-2207 and :2188-2191.
    no_handler = (
        "dosiz: PM exception vec=13 at 00a8:0000c3f0 err=0x18 "
        "(no user handler installed, terminating)\n"
    )
    fault = dz.parse_pm_fault(no_handler)
    assert fault is not None
    assert (fault.cs, fault.eip, fault.err, fault.regs["vec"]) == (
        0x00A8, 0x0000C3F0, 0x18, 13
    )

    recursive = (
        "dosiz: PM exception dispatcher in recursive-fault loop "
        "(vec=13 cs:eip=00a8:0000c3f0 err=0x18) -- terminating\n"
    )
    fault = dz.parse_pm_fault(recursive)
    assert fault is not None
    assert (fault.cs, fault.eip, fault.err) == (0x00A8, 0x0000C3F0, 0x18)


def test_parse_pm_fault_reports_a_vector_unknown_frame_rather_than_dropping_it() -> None:
    # bridge.cc:1971-1975: no per-vector info, so no CS:EIP. An agent still
    # needs to know a fault happened.
    text = (
        "dosiz: LE client PM exception (vector unknown) -- terminating.\n"
        "  SS:ESP = 001c:00021040  stack = 00000000 00000000 00000000 "
        "00000000 00000000\n"
    )
    fault = dz.parse_pm_fault(text)
    assert fault is not None
    assert (fault.cs, fault.eip, fault.err) == (0, 0, 0)


def test_parse_pm_fault_is_none_on_a_clean_run() -> None:
    assert dz.parse_pm_fault(NOISE_ONLY) is None
    assert dz.parse_pm_fault(UNIMP_STDERR) is None


# ---------------------------------------------------------------------------
# exit-code classification -- the whole point of x80_dos_run
# ---------------------------------------------------------------------------

def test_classify_exit_guest_exit_is_the_default() -> None:
    # Measured: DJ_PRINTF.exe -> rc 7, from INT 21h AH=4Ch AL (bridge.cc:5438).
    assert dz.classify_exit(rc=7, timed_out=False, stderr_text=NOISE_ONLY) == (
        7, ExitCodeMeaning.GUEST_EXIT
    )


def test_classify_exit_disambiguates_rc_1() -> None:
    # The case SPEC.md 6.4 names: the same number, three different meanings.
    assert dz.classify_exit(
        rc=1, timed_out=False, stderr_text=NOISE_ONLY
    ) == (1, ExitCodeMeaning.GUEST_EXIT)
    assert dz.classify_exit(
        rc=1, timed_out=False, stderr_text=MISSING_PROGRAM_STDERR
    ) == (1, ExitCodeMeaning.LOADER_FAILURE)
    assert dz.classify_exit(
        rc=1, timed_out=False, stderr_text=LE_FAULT_STDERR
    ) == (1, ExitCodeMeaning.PM_FAULT)


def test_classify_exit_timeout_has_no_exit_code_at_all() -> None:
    # SPEC.md 5.5: the deadline is the supervisor's, so there is no guest
    # status to report -- not a zero, not the signal number.
    assert dz.classify_exit(rc=None, timed_out=True, stderr_text="") == (
        None, ExitCodeMeaning.TIMEOUT
    )


def test_classify_exit_calls_the_argv0_trap_a_loader_failure() -> None:
    assert dz.classify_exit(
        rc=102, timed_out=False, stderr_text=NOISE_ONLY,
        stdout_text=ARGV0_TRAP_STDOUT,
    ) == (102, ExitCodeMeaning.LOADER_FAILURE)


@pytest.mark.parametrize("line", [
    "dosiz: unknown option: --nope",
    "dosiz: cannot find 'NOSUCH.EXE' on DOSIZ_PATH",
    "dosiz: no program to run (set 'program =' in cfg or pass on CLI)",
    "dosiz: cannot open FOO.EXE: No such file or directory",
    "dosiz: read error on FOO.EXE",
    "dosiz: FOO.EXE has no MZ signature",
    "dosiz: FOO.EXE too small to be an MZ .EXE",
    "dosiz: FOO.COM too large for .COM (99999 bytes)",
    "dosiz: FOO.EXE header describes 1024 bytes, file has 512",
    "dosiz: FOO.EXE reloc 3 out of bounds",
    "dosiz: FOO.EXE is LE/LX but le_load_objects failed",
    "dosiz: LE entry_obj=9 out of range",
    "dosiz: LE descriptor install failed",
    "dosiz: LE obj 1: pm_alloc 65536 bytes failed",
    "dosiz: LE: no LDT run of 4 descriptors",
    "dosiz: LE: no data object to host the synth stack",
    "dosiz: bring-up failed: something",
    "dosiz: bring-up threw: std::bad_alloc",
])
def test_find_loader_failure_covers_every_message_that_precedes_rc_1(line: str) -> None:
    assert dz.find_loader_failure(NOISE_ONLY + line + "\n") == line


def test_find_loader_failure_ignores_the_loader_running_normally() -> None:
    chatty = (
        "dosiz: LE_MIN.EXE is LE (CPU=3, 1 pages of 4096 bytes, 2 objects, "
        "entry obj#1+0x0, stack obj#2+0x1000)\n"
        "dosiz: LE fixups applied: 2\n"
    )
    assert dz.find_loader_failure(chatty) is None


# ---------------------------------------------------------------------------
# cfg synthesis
# ---------------------------------------------------------------------------

def test_synthesize_cfg_writes_only_keys_dosiz_recognises() -> None:
    text = dz.synthesize_cfg(
        program="SORT.EXE",
        default_mode=DefaultMode.BINARY,
        eol_convert=False,
        printer_path="/sb/.lst",
        aux_output_path="/sb/.pun",
    )
    keys = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.startswith("#")
    }
    # dosiz/src/config.cc:88-100.
    recognised = {
        "program", "cd", "chdir", "default_mode", "eol_convert", "verbose",
        "printer", "aux_input", "aux_output", "machine", "cputype", "core",
        "memsize", "headless", "args",
    }
    assert keys <= recognised
    assert "program = SORT.EXE" in text
    assert "default_mode = binary" in text
    assert "eol_convert = false" in text
    assert "printer = /sb/.lst" in text


def test_synthesize_cfg_refuses_a_path_because_that_is_the_argv0_trap() -> None:
    with pytest.raises(ValueError) as exc:
        dz.synthesize_cfg(
            program="/abs/DJ_PRINTF.exe",
            default_mode=DefaultMode.BINARY,
            eol_convert=False,
        )
    assert "C:\\ABS\\DJ_PRINTF.EXE" in str(exc.value)


# ---------------------------------------------------------------------------
# caps and the ops a one-shot does not serve
# ---------------------------------------------------------------------------

def test_caps_shape_and_the_two_flags_that_matter() -> None:
    caps = dz.DosizBackend().caps()
    assert caps.backend == "dosiz"
    assert caps.shape is AdapterShape.ONE_SHOT
    assert caps.family is Family.X86
    assert caps.tier is Tier.HOSTED
    # SPEC.md 5.4 Invariant 4: dosiz is the one backend where the exit code
    # says something about the guest.
    assert caps.has_exit_code is True
    # SPEC.md 1.4: never claim an idle signal you cannot read. A one-shot has
    # no control channel to ask dos_machine::is_waiting_for_key().
    assert caps.has_idle_signal is False
    assert caps.ops == frozenset(
        {"caps", "boot", "run", "send", "recv", "idle", "stop"}
    )
    assert caps.divergences  # carried inline per SPEC.md 6.4, not buried in docs


@pytest.mark.parametrize("op", [
    "regs", "mem_read", "mem_write", "step", "bp_set", "bp_clear", "disasm",
    "screen_text", "screen_pixels", "console_list", "console_select",
    "trace_on", "trace_off", "trace_read", "syscall_bp",
])
def test_debugger_ops_refuse_with_a_structured_body(op: str) -> None:
    backend = dz.DosizBackend()
    assert backend.serves(op) is False
    with pytest.raises(UnsupportedOp) as exc:
        getattr(backend, op)({})
    body = exc.value.to_json()
    assert body["error"] == "unsupported"
    assert body["backend"] == "dosiz"
    assert body["op"] == op


def test_boot_is_a_no_op_but_refuses_freedos(sandbox: StubSandbox) -> None:
    backend = dz.DosizBackend()
    assert backend.boot(BootSpec(sandbox=sandbox, profile="dos-hosted")).ok is True
    with pytest.raises(UnsupportedOp) as exc:
        backend.boot(BootSpec(sandbox=sandbox, profile="freedos"))
    body = exc.value.to_json()
    assert body["alternative_profile"] == "dos-hosted"
    assert "emu88" in body["reason"]


def test_run_refuses_a_stop_condition_it_cannot_enforce(
    sandbox: StubSandbox, tmp_path: Path
) -> None:
    prog = tmp_path / "X.COM"
    prog.write_bytes(bytes([0xB8, 0x00, 0x4C, 0xCD, 0x21]))
    backend = dz.DosizBackend(binary="/bin/echo")   # never reached
    for kwargs in ({"until": "A>"}, {"max_steps": 1000}, {"console": 3}):
        with pytest.raises(UnsupportedOp) as exc:
            backend.run(RunRequest(
                sandbox=sandbox, program=str(prog), **kwargs
            ))
        assert exc.value.to_json()["backend"] == "dosiz"


def test_one_shot_send_recv_idle_stop(sandbox: StubSandbox) -> None:
    backend = dz.DosizBackend()
    assert backend.idle(IdleRequest()).source == "not_started"
    assert backend.send(SendRequest(data=b"hello")).bytes_sent == 5
    assert backend.send(SendRequest(data=b"hello")).paced_by == "not_waited"
    assert backend.recv(RecvRequest()).data == b""
    assert backend.stop(StopRequest()).stopped is True


def test_a_missing_binary_is_reported_not_raised_at_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(dz.BINARY_ENV_VAR, str(tmp_path / "not-here"))
    backend = dz.DosizBackend()
    assert backend.available() is False
    assert backend.blocked_by()
    # caps() still answers, so x80_profiles can render a ready:false row.
    assert backend.caps().binary_path is None


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------

def _find_fixtures() -> Path | None:
    env = os.environ.get("EIGHTYMCP_DOSIZ_TESTS")
    candidates = [Path(env)] if env else []
    binary = dz.find_binary()
    if binary:
        # The usual layout is <checkout>/build/dosiz beside <checkout>/tests.
        for parent in Path(binary).resolve().parents:
            candidates.append(parent / "tests")
    candidates.append(Path.home() / "src" / "dosiz" / "tests")
    for c in candidates:
        if (c / "DJ_PRINTF.exe").is_file():
            return c
    return None


BINARY = dz.find_binary()
FIXTURES = _find_fixtures()
needs_dosiz = pytest.mark.skipif(
    BINARY is None,
    reason=f"no dosiz binary; set ${dz.BINARY_ENV_VAR} or put it on PATH",
)
needs_fixtures = pytest.mark.skipif(
    FIXTURES is None,
    reason="no dosiz/tests fixtures; set $EIGHTYMCP_DOSIZ_TESTS",
)


@needs_dosiz
def test_live_version_is_reported() -> None:
    backend = dz.DosizBackend()
    assert backend.available() is True
    assert backend.blocked_by() == []
    assert (backend.version() or "").startswith("dosiz ")


@needs_dosiz
@needs_fixtures
def test_live_sort_round_trips_under_100ms(sandbox: StubSandbox) -> None:
    """SPEC.md 9, phase 1 acceptance: "dosiz SORT.EXE round-trips in under
    100 ms"."""
    assert FIXTURES is not None
    backend = dz.DosizBackend()
    sandbox.snapshot()
    outcome = backend.run(RunRequest(
        sandbox=sandbox,
        program=str(FIXTURES / "SORT.EXE"),
        stdin=b"pear\nbanana\napple\ncherry\n",
        timeout_ms=20000,
    ))
    assert outcome.exit_code == 0
    assert outcome.exit_code_meaning is ExitCodeMeaning.GUEST_EXIT
    assert outcome.exit_reason is ExitReason.BDOS_0
    # CR preserved: DOS convention, and the reason x80_diff_run has a
    # crlf_to_lf normalization rather than doing it silently.
    assert outcome.stdout == b"apple\r\nbanana\r\ncherry\r\npear\r\n"
    assert outcome.wall_ms < 100, f"wall_ms={outcome.wall_ms}"
    assert outcome.stderr_filtered == list(DOSIZ_STDERR_NOISE)
    assert outcome.unimplemented_int21 == []
    assert outcome.pm_fault is None
    # created_since_snapshot() answers "what did the program write?" -- the
    # adapter re-snapshots after staging, so neither the copy of SORT.EXE nor
    # the synthesized cfg shows up. SORT wrote only to stdout.
    assert sandbox.created_since_snapshot() == []
    assert (sandbox.guest_dir / "SORT.EXE").is_file()
    assert (sandbox.root / dz.CFG_NAME).is_file()


@needs_dosiz
@needs_fixtures
def test_live_files_in_reaches_the_guest(sandbox: StubSandbox) -> None:
    assert FIXTURES is not None
    src = sandbox.root / "in.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"pear\nbanana\napple\n")
    backend = dz.DosizBackend()
    outcome = backend.run(RunRequest(
        sandbox=sandbox,
        program=str(FIXTURES / "CAT.EXE"),
        args=["IN.TXT"],
        files_in=[FileIn(guest_name="IN.TXT", host_path=str(src))],
        timeout_ms=20000,
    ))
    assert outcome.exit_code == 0
    assert b"apple" in outcome.stdout


@needs_dosiz
@needs_fixtures
def test_live_dj_printf_exit_code_is_7(sandbox: StubSandbox) -> None:
    """SPEC.md 7.5 and 9: "DJ_PRINTF.exe gives exit_code:7"."""
    assert FIXTURES is not None
    outcome = dz.DosizBackend().run(RunRequest(
        sandbox=sandbox,
        program=str(FIXTURES / "DJ_PRINTF.exe"),
        timeout_ms=20000,
    ))
    assert outcome.exit_code == 7
    assert outcome.exit_code_meaning is ExitCodeMeaning.GUEST_EXIT
    assert outcome.stdout == (
        b"int=42 str=hello\r\nflt=3.142 hex=0xDEADBEEF\r\ndj-printf=ok\r\n"
    )


@needs_dosiz
@needs_fixtures
def test_live_the_argv0_trap_is_real_and_the_adapter_avoids_it(
    tmp_path: Path, sandbox: StubSandbox
) -> None:
    """Both halves, in one test, because a mitigation whose failure mode is not
    demonstrated is a comment.

    SPEC.md 5.5: "``./build/dosiz /abs/path/tests/DJ_PRINTF.exe`` -> rc 102".
    """
    assert BINARY is not None and FIXTURES is not None
    staged = tmp_path / "DJ_PRINTF.exe"
    shutil.copy2(FIXTURES / "DJ_PRINTF.exe", staged)

    # (a) The trap. An absolute path, run from a cwd that is not its directory.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = subprocess.run(
        [BINARY, str(staged)],
        cwd=elsewhere, capture_output=True, timeout=60,
    )
    assert proc.returncode == dz.ARGV0_TRAP_RC == 102
    trapped = dz.detect_argv0_trap(proc.stdout.decode("latin-1"), proc.returncode)
    assert trapped == dz.dos_argv0(str(staged))
    assert dz.classify_exit(
        rc=proc.returncode,
        timed_out=False,
        stderr_text=proc.stderr.decode("latin-1"),
        stdout_text=proc.stdout.decode("latin-1"),
    )[1] is ExitCodeMeaning.LOADER_FAILURE

    # (b) The adapter, given the same absolute host path, runs it anyway.
    outcome = dz.DosizBackend().run(RunRequest(
        sandbox=sandbox, program=str(staged), timeout_ms=20000,
    ))
    assert outcome.exit_code == 7
    assert outcome.exit_code_meaning is ExitCodeMeaning.GUEST_EXIT
    # cwd was the guest dir and the program was a bare name.
    assert outcome.raw is not None
    assert outcome.raw.cwd == str(sandbox.guest_dir)
    assert not any("/" in a for a in outcome.raw.argv[2:])


@needs_dosiz
def test_live_unimplemented_int21_is_collected(sandbox: StubSandbox) -> None:
    """Hand-assembled: INT 21h AH=7Fh, INT 21h AH=6Bh, then AH=4Ch AL=03."""
    prog = sandbox.root / "UNIMP.COM"
    prog.write_bytes(bytes([
        0xB4, 0x7F, 0xCD, 0x21,   # mov ah,7Fh ; int 21h
        0xB4, 0x6B, 0xCD, 0x21,   # mov ah,6Bh ; int 21h
        0xB8, 0x03, 0x4C, 0xCD, 0x21,   # mov ax,4C03h ; int 21h
    ]))
    outcome = dz.DosizBackend().run(RunRequest(
        sandbox=sandbox, program=str(prog), timeout_ms=20000,
    ))
    # The guest kept running past two unimplemented calls and exited with the
    # code it chose: an exit code alone says nothing about completeness.
    assert outcome.exit_code == 3
    assert outcome.exit_code_meaning is ExitCodeMeaning.GUEST_EXIT
    assert [c.ah for c in outcome.unimplemented_int21] == [0x7F, 0x6B]
    assert any("AH=7Fh" in w for w in outcome.warnings)


@needs_dosiz
def test_live_a_spin_loop_is_killed_by_the_sandbox_deadline(
    sandbox: StubSandbox,
) -> None:
    """SPEC.md 5.5: "dosiz's run loop has no instruction counter and no wall
    clock (a 2-byte EB FE spin ran until SIGKILL)"."""
    prog = sandbox.root / "SPIN.COM"
    prog.write_bytes(bytes([0xEB, 0xFE]))   # jmp $
    started = time.monotonic()
    outcome = dz.DosizBackend().run(RunRequest(
        sandbox=sandbox, program=str(prog), timeout_ms=800,
    ))
    elapsed_ms = (time.monotonic() - started) * 1000
    assert outcome.timed_out is True
    assert outcome.exit_reason is ExitReason.TIMEOUT
    assert outcome.exit_code is None
    assert outcome.exit_code_meaning is ExitCodeMeaning.TIMEOUT
    assert elapsed_ms < 5000, f"deadline did not fire: {elapsed_ms:.0f} ms"


@needs_dosiz
def test_live_a_missing_program_is_a_backend_failure_not_a_traceback(
    sandbox: StubSandbox,
) -> None:
    with pytest.raises(BackendFailure) as exc:
        dz.DosizBackend().run(RunRequest(
            sandbox=sandbox, program=str(sandbox.root / "NOPE.EXE"),
        ))
    body = exc.value.to_json()
    assert body["error"] == "backend_failure"
    assert body["backend"] == "dosiz"
    assert "NOPE.EXE" in body["program"]


@needs_dosiz
def test_live_a_non_dos_binary_is_a_loader_failure(sandbox: StubSandbox) -> None:
    prog = sandbox.root / "JUNK.EXE"
    prog.write_bytes(b"not an MZ image at all, not even close" * 4)
    outcome = dz.DosizBackend().run(RunRequest(
        sandbox=sandbox, program=str(prog), timeout_ms=20000,
    ))
    assert outcome.exit_code_meaning is ExitCodeMeaning.LOADER_FAILURE
    assert outcome.exit_reason is ExitReason.EMULATOR_ERROR
    assert any("did not load" in w for w in outcome.warnings)


@needs_dosiz
def test_live_env_that_cannot_reach_the_guest_is_named(
    sandbox: StubSandbox,
) -> None:
    prog = sandbox.root / "EXIT0.COM"
    prog.write_bytes(bytes([0xB8, 0x00, 0x4C, 0xCD, 0x21]))
    outcome = dz.DosizBackend().run(RunRequest(
        sandbox=sandbox,
        program=str(prog),
        env={"MYVAR": "1", "TZ": "UTC", "DOSIZ_PATH": "/etc", "DOSIZ_TRACE": ""},
        timeout_ms=20000,
    ))
    assert outcome.exit_code == 0
    # TZ is in the passthrough list; MYVAR is not; the two DOSIZ_ keys never
    # reach the host process at all.
    assert sandbox.last_env.get("TZ") == "UTC"
    assert sandbox.last_env.get("MYVAR") == "1"
    assert "DOSIZ_PATH" not in sandbox.last_env
    assert "DOSIZ_TRACE" not in sandbox.last_env
    joined = " ".join(outcome.warnings)
    assert "'MYVAR'" in joined
    assert "DOSIZ_PATH" in joined
    assert "DOSIZ_TRACE" in joined


def _make_faulting_le(dest_dir: Path) -> Path | None:
    """Build an LE image whose entry point is UD2, from dosiz's own generator.

    None of the 152 fixtures in ``dosiz/tests`` faults, so the only way to
    exercise the PM-fault path end to end is to make one.
    """
    if FIXTURES is None:
        return None
    gen = FIXTURES / "gen_le_min.py"
    if not gen.is_file():
        return None
    src = gen.read_text()
    anchor = 'page[5:7] = b"\\xB4\\x4C"'
    if anchor not in src or '"LE_MIN.EXE"' not in src:
        return None
    src = src.replace('"LE_MIN.EXE"', '"LE_FAULT.EXE"')
    src = src.replace(anchor, 'page[5:7] = b"\\x0F\\x0B"')   # UD2
    script = dest_dir / "gen_le_fault.py"
    script.write_text(src)
    subprocess.run(["python3", str(script)], cwd=dest_dir, check=True,
                   capture_output=True, timeout=60)
    out = dest_dir / "LE_FAULT.EXE"
    return out if out.is_file() else None


@needs_dosiz
@needs_fixtures
def test_live_a_pm_fault_lands_in_diagnostics(
    tmp_path: Path, sandbox: StubSandbox
) -> None:
    prog = _make_faulting_le(tmp_path)
    if prog is None:
        pytest.skip("dosiz/tests/gen_le_min.py is absent or has changed shape")
    outcome = dz.DosizBackend().run(RunRequest(
        sandbox=sandbox, program=str(prog), timeout_ms=20000,
    ))
    assert outcome.exit_code_meaning is ExitCodeMeaning.PM_FAULT
    assert outcome.exit_reason is ExitReason.EMULATOR_ERROR
    assert outcome.pm_fault is not None
    # UD2 is vector 6.
    assert outcome.pm_fault.regs["vec"] == 6
    assert outcome.pm_fault.to_json().keys() == {"cs", "eip", "err", "regs"}


@needs_dosiz
@needs_fixtures
def test_live_default_mode_binary_matches_dosiz_stock_auto(
    tmp_path: Path,
) -> None:
    """Invariant 3 sets ``default_mode`` to binary family-wide. dosiz does not
    have cpmemu's auto-never-resolves-on-write bug, so this asserts the change
    is a no-op on dosiz rather than assuming it."""
    assert FIXTURES is not None
    results = {}
    for mode in (DefaultMode.BINARY, DefaultMode.AUTO):
        sb = StubSandbox(tmp_path / f"sb-{mode.value}")
        outcome = dz.DosizBackend().run(RunRequest(
            sandbox=sb,
            program=str(FIXTURES / "SORT.EXE"),
            stdin=b"pear\nbanana\napple\n",
            default_mode=mode,
            eol_convert=(mode is DefaultMode.AUTO),
            timeout_ms=20000,
        ))
        results[mode] = (outcome.exit_code, outcome.stdout)
    assert results[DefaultMode.BINARY] == results[DefaultMode.AUTO]


# ---------------------------------------------------------------------------
# The real sandbox
# ---------------------------------------------------------------------------
#
# Everything above runs against StubSandbox so the backend can be tested on its
# own. These run against eightymcp.sandbox.Sandbox, which is the thing that
# actually ships: the adapter is only correct if the two agree.

def _real_sandbox_class():
    try:
        from eightymcp.sandbox import Sandbox
    except Exception:                          # pragma: no cover - not landed yet
        return None
    return Sandbox


needs_real_sandbox = pytest.mark.skipif(
    _real_sandbox_class() is None,
    reason="eightymcp.sandbox has not landed yet",
)


@needs_real_sandbox
def test_the_real_sandbox_satisfies_the_protocol_the_backend_codes_against(
    tmp_path: Path,
) -> None:
    Sandbox = _real_sandbox_class()
    with Sandbox(base_dir=tmp_path) as sb:
        assert isinstance(sb, SandboxProtocol)


@needs_real_sandbox
@needs_dosiz
@needs_fixtures
def test_live_dj_printf_on_the_real_sandbox(tmp_path: Path) -> None:
    """SPEC.md 9 acceptance, end to end on the shipping sandbox."""
    assert FIXTURES is not None
    Sandbox = _real_sandbox_class()
    with Sandbox(base_dir=tmp_path) as sb:
        outcome = dz.DosizBackend().run(RunRequest(
            sandbox=sb,
            program=str(FIXTURES / "DJ_PRINTF.exe"),
            timeout_ms=20000,
        ))
        assert outcome.exit_code == 7
        assert outcome.exit_code_meaning is ExitCodeMeaning.GUEST_EXIT
        assert outcome.stderr_filtered == list(DOSIZ_STDERR_NOISE)
        # The program was staged into the sandbox and named relatively, so the
        # argv[0] trap cannot fire.
        assert outcome.raw is not None
        assert outcome.raw.cwd == str(sb.guest_dir)
        assert (sb.guest_dir / "DJ_PRINTF.exe").is_file()
        assert sb.created_since_snapshot() == []


@needs_real_sandbox
@needs_dosiz
@needs_fixtures
def test_live_sort_on_the_real_sandbox_under_100ms(tmp_path: Path) -> None:
    assert FIXTURES is not None
    Sandbox = _real_sandbox_class()
    with Sandbox(base_dir=tmp_path) as sb:
        outcome = dz.DosizBackend().run(RunRequest(
            sandbox=sb,
            program=str(FIXTURES / "SORT.EXE"),
            stdin=b"pear\nbanana\napple\ncherry\n",
            timeout_ms=20000,
        ))
    assert outcome.exit_code == 0
    assert outcome.stdout == b"apple\r\nbanana\r\ncherry\r\npear\r\n"
    assert outcome.wall_ms < 100, f"wall_ms={outcome.wall_ms}"


@needs_real_sandbox
@needs_dosiz
def test_live_the_real_sandbox_deadline_kills_the_spin_loop(tmp_path: Path) -> None:
    """SPEC.md 5.5: dosiz has no internal timeout, so the sandbox is the only
    thing that ends this."""
    Sandbox = _real_sandbox_class()
    with Sandbox(base_dir=tmp_path) as sb:
        prog = sb.root / "SPIN.COM"
        prog.write_bytes(bytes([0xEB, 0xFE]))
        started = time.monotonic()
        outcome = dz.DosizBackend().run(RunRequest(
            sandbox=sb, program=str(prog), timeout_ms=800,
        ))
        elapsed_ms = (time.monotonic() - started) * 1000
    assert outcome.timed_out is True
    assert outcome.exit_code is None
    assert outcome.exit_code_meaning is ExitCodeMeaning.TIMEOUT
    assert outcome.exit_reason is ExitReason.TIMEOUT
    assert elapsed_ms < 5000, f"deadline did not fire: {elapsed_ms:.0f} ms"


@needs_real_sandbox
@needs_dosiz
def test_live_the_real_sandbox_pins_home_against_a_backend_that_tries(
    tmp_path: Path,
) -> None:
    """Invariant 1 has teeth: ``env`` adds to the hermetic base and cannot drop
    it. Asserted here because the dosiz adapter is a caller of ``exec`` and a
    caller is exactly what would break it."""
    Sandbox = _real_sandbox_class()
    with Sandbox(base_dir=tmp_path) as sb:
        env = sb.build_env({"HOME": "/Users/somebody", "MYVAR": "1"})
    assert env["HOME"] == str(sb.home_dir)
    assert env["XDG_CONFIG_HOME"] == str(sb.xdg_dir)
    assert env["MYVAR"] == "1"
