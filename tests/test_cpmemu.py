"""cpmemu adapter tests. Every emulator assertion here ran the real binary.

The acceptance cases are SPEC.md 9 phase 1's, on the fixture the spec names:
``80un.com`` on ``tests/samples/arc/method9.arc``. Where the measurement no
longer matches the spec, the test asserts the measurement and says which line
of the spec it diverges from -- the spec is evidence, not scripture.

There is no ``eightymcp.sandbox`` yet. :class:`StubSandbox` below is a real
implementation of :class:`~eightymcp.mbp.SandboxProtocol` -- hermetic env, own
process group, wall-clock deadline, group kill on expiry -- so the adapter is
exercised against the real cpmemu rather than against a mock of it. When
``eightymcp.sandbox`` lands it replaces this stub and nothing in the adapter
changes; that is what the Protocol is for.
"""

from __future__ import annotations

import base64
import dataclasses
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from eightymcp.backends.cpmemu import (
    BACKEND_NAME,
    DIVERGENCES,
    PROFILE,
    CpmemuBackend,
    blocked_by,
    build_tail,
    find_binary,
    guest_name_for,
    parse_stderr,
    synthesize_cfg,
    tail_argument,
)
from eightymcp.mbp import (
    BackendFailure,
    BootSpec,
    DiskSpec,
    IdleRequest,
    Op,
    RecvRequest,
    REQUIRED_OPS,
    RunRequest,
    SandboxProtocol,
    SendRequest,
    StopRequest,
)
from eightymcp.types import (
    AdapterShape,
    Cpu,
    DefaultMode,
    ExecResult,
    ExitReason,
    Family,
    FileIn,
    RunResult,
    Tier,
    ToolExecutionError,
)

# ---------------------------------------------------------------------------
# Fixtures on disk
# ---------------------------------------------------------------------------

CPMEMU = find_binary()

#: The 80un checkout SPEC.md 7.1 measures against. Overridable, because the
#: path is a working copy and not part of either repo. Searched in the same
#: order as examples/agent/80un/run.py: an explicit override, then a sibling of
#: this checkout, then the conventional ~/src location.
def _find_un80() -> Path:
    override = os.environ.get("EIGHTYMCP_80UN")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent.parent
    for cand in (here.parent / "80un", Path.home() / "src" / "80un"):
        if (cand / "80un.com").is_file():
            return cand
    # Nothing found: return the first candidate so the skip reason names a
    # real path a reader can act on rather than an empty string.
    return here.parent / "80un"


UN80_ROOT = _find_un80()
UN80_COM = UN80_ROOT / "80un.com"
METHOD9_ARC = UN80_ROOT / "tests" / "samples" / "arc" / "method9.arc"

needs_cpmemu = pytest.mark.skipif(CPMEMU is None, reason="cpmemu binary not found")
needs_80un = pytest.mark.skipif(
    not (UN80_COM.is_file() and METHOD9_ARC.is_file()),
    reason=f"80un fixture not found under {UN80_ROOT}",
)


# ---------------------------------------------------------------------------
# A sandbox that satisfies SandboxProtocol
# ---------------------------------------------------------------------------

class StubSandbox:
    """The four launch invariants' half that a one-shot backend needs.

    Hermetic env (Invariant 1's HOME and XDG_CONFIG_HOME), a real deadline and
    a process-group kill (SPEC.md 5.5: nothing in the family has a timeout),
    ``.lst``/``.pun`` as real files (Appendix B item 2).
    """

    def __init__(self, root: Path):
        self.root = root
        self.guest_dir = root / "guest"
        self.home_dir = root / "home"
        self.xdg_dir = root / "xdg"
        self.lst_path = root / ".lst"
        self.pun_path = root / ".pun"
        for d in (self.guest_dir, self.home_dir, self.xdg_dir):
            d.mkdir(parents=True, exist_ok=True)
        # Never empty, never /dev/null.
        self.lst_path.touch()
        self.pun_path.touch()
        self._snapshot: dict[Path, tuple[float, int]] = {}

    def stage(self, f: FileIn) -> Path:
        dest = self.guest_dir / f.guest_name
        if f.host_path is not None:
            shutil.copyfile(f.host_path, dest)
        else:
            dest.write_bytes(base64.b64decode(f.content_b64 or ""))
        return dest

    def snapshot(self) -> None:
        self._snapshot = self._walk()

    def created_since_snapshot(self) -> list[Path]:
        now = self._walk()
        return sorted(p for p, st in now.items() if self._snapshot.get(p) != st)

    def _walk(self) -> dict[Path, tuple[float, int]]:
        out: dict[Path, tuple[float, int]] = {}
        for p in self.guest_dir.rglob("*"):
            if p.is_file():
                s = p.stat()
                out[p] = (s.st_mtime, s.st_size)
        return out

    def write_text(self, relative: str, text: str) -> Path:
        p = self.root / relative
        p.write_text(text, encoding="utf-8")
        return p

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
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home_dir),
            "XDG_CONFIG_HOME": str(self.xdg_dir),
            "LC_ALL": "C",
        }
        base.update(env or {})
        # `env` adds to the hermetic base; it cannot drop HOME/XDG_CONFIG_HOME.
        base["HOME"] = str(self.home_dir)
        base["XDG_CONFIG_HOME"] = str(self.xdg_dir)

        started = time.monotonic()
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd or self.guest_dir),
            env=base,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            out, err = proc.communicate(input=stdin, timeout=timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
        wall_ms = int((time.monotonic() - started) * 1000)
        rc = proc.returncode
        return ExecResult(
            argv=list(argv),
            cwd=str(cwd or self.guest_dir),
            rc=rc if (rc is not None and rc >= 0) else None,
            signal=-rc if (rc is not None and rc < 0) else None,
            stdout=out,
            stderr=err,
            wall_ms=wall_ms,
            timed_out=timed_out,
            deadline_ms=timeout_ms,
        )


@pytest.fixture()
def sandbox(tmp_path: Path) -> StubSandbox:
    return StubSandbox(tmp_path / "sbx")


def com(sandbox: StubSandbox, name: str, hexbytes: str) -> Path:
    """A hand-assembled .COM, so a sentinel can be provoked on purpose."""
    p = sandbox.root / name
    p.write_bytes(bytes(int(b, 16) for b in hexbytes.split()))
    return p


def test_stub_sandbox_satisfies_the_protocol(sandbox: StubSandbox) -> None:
    assert isinstance(sandbox, SandboxProtocol)


# ---------------------------------------------------------------------------
# Discovery and caps
# ---------------------------------------------------------------------------

@needs_cpmemu
def test_find_binary_locates_an_executable() -> None:
    assert CPMEMU is not None
    assert CPMEMU.is_file() and os.access(CPMEMU, os.X_OK)


def test_missing_binary_is_a_structured_error_not_a_crash(
    tmp_path: Path, sandbox: StubSandbox
) -> None:
    backend = CpmemuBackend(binary_path=tmp_path / "not-here")
    assert backend.available is False
    assert backend.binary is None
    assert backend.blocked_by() and "EIGHTYMCP_CPMEMU" in backend.blocked_by()[0]
    # caps() still answers: an absent backend is a profile, not an exception.
    assert backend.caps().binary_path is None
    with pytest.raises(ToolExecutionError) as exc:
        backend.run(RunRequest(sandbox=sandbox, program=str(tmp_path / "x.com")))
    assert exc.value.err.to_json()["error"] == "backend_missing"
    assert blocked_by(None)[0].startswith("cpmemu was not found")


@needs_cpmemu
def test_caps_reports_seven_ops_and_no_exit_code() -> None:
    caps = CpmemuBackend().caps()
    assert caps.backend == BACKEND_NAME
    assert caps.shape is AdapterShape.ONE_SHOT
    assert caps.family is Family.Z80 and caps.tier is Tier.HOSTED
    assert caps.ops == REQUIRED_OPS
    assert caps.cpus == (Cpu.I8080.value, Cpu.Z80.value)
    # SPEC.md 5.4 Invariant 4, and SPEC.md 1.4 on idle heuristics.
    assert caps.has_exit_code is False
    assert caps.has_idle_signal is False
    # cpmemu has no --version (measured); whatever reports one gets it from
    # packaging, not from the program.
    assert caps.version is None
    assert caps.divergences == DIVERGENCES
    assert any("lowercased" in d for d in caps.divergences)


@needs_cpmemu
def test_debugger_ops_refuse_with_an_actionable_body() -> None:
    backend = CpmemuBackend()
    with pytest.raises(ToolExecutionError) as exc:
        backend.step({})
    body = exc.value.err.to_json()
    assert body["error"] == "unsupported"
    assert body["op"] == "step"
    assert body["backend"] == "cpmemu"
    assert "no header" in body["reason"]
    assert body["alternative_profile"] == "cpm22"
    assert backend.serves(Op.STEP) is False
    assert backend.serves(Op.RUN) is True


# ---------------------------------------------------------------------------
# The synthesized cfg -- Invariant 3 and Appendix B item 2
# ---------------------------------------------------------------------------

def test_cfg_is_spec_7_1_verbatim() -> None:
    text = synthesize_cfg(
        program="/Users/wohl/src/80un/80un.com",
        guest_dir="/sbx/guest",
        lst_path="/sbx/.lst",
        pun_path="/sbx/.pun",
    )
    assert text == (
        "program = /Users/wohl/src/80un/80un.com\n"
        "cd = /sbx/guest\n"
        "default_mode = binary\n"
        "eol_convert = false\n"
        "printer = /sbx/.lst\n"
        "aux_output = /sbx/.pun\n"
    )


def test_cfg_never_leaves_a_device_empty_or_on_dev_null() -> None:
    """Appendix B item 2. Empty warns on every run and sends LIST bytes to
    stdout as "[PRINTER] c"; /dev/null destroys the stream silently."""
    text = synthesize_cfg(
        program="/p.com", guest_dir="/g", lst_path="/sbx/.lst", pun_path="/sbx/.pun"
    )
    for line in text.splitlines():
        key, _, value = line.partition(" = ")
        if key in ("printer", "aux_output"):
            assert value.strip() != ""
            assert value.strip() != "/dev/null"


def test_cfg_default_mode_is_binary_and_eol_convert_is_always_written() -> None:
    text = synthesize_cfg(
        program="/p.com", guest_dir="/g", lst_path="/l", pun_path="/p"
    )
    assert "default_mode = binary\n" in text
    # cpmemu.cc:398 defaults eol_convert to true; omitting the line is half of
    # the measured corruption, so the line is never omitted.
    assert "eol_convert = false\n" in text


def test_cfg_refuses_a_value_the_parser_would_eat() -> None:
    for bad in ("/tmp/a$b/guest", "/tmp/a\nb"):
        with pytest.raises(ToolExecutionError) as exc:
            synthesize_cfg(
                program="/p.com", guest_dir=bad, lst_path="/l", pun_path="/p"
            )
        assert exc.value.err.to_json()["error"] == "bad_argument"


# ---------------------------------------------------------------------------
# The FCB hazard
# ---------------------------------------------------------------------------

def test_tail_argument_prefixes_a_relative_host_path(sandbox: StubSandbox) -> None:
    base = sandbox.guest_dir
    (base / "out").mkdir()
    (base / "a").mkdir()
    (base / "a" / "b").mkdir()

    assert tail_argument("out/M9.ARC", base) == "./out/M9.ARC"
    assert tail_argument("a/b/c.dat", base) == "./a/b/c.dat"
    # Nothing to strip, or already on the stripping path.
    assert tail_argument("M9.ARC", base) == "M9.ARC"
    assert tail_argument("./out/M9.ARC", base) == "./out/M9.ARC"
    assert tail_argument("/abs/M9.ARC", base) == "/abs/M9.ARC"
    # A CP/M tail with switches is not a host path: no switch letter is a
    # directory, so the filesystem answers this without a spelling heuristic.
    assert tail_argument("TEST,TEST.COM/N/E", base) == "TEST,TEST.COM/N/E"
    assert tail_argument("FOO.COM/N", base) == "FOO.COM/N"
    assert tail_argument("TEST/N/E", base) == "TEST/N/E"
    # A directory that does not exist yet is not a path this can fix.
    assert tail_argument("missing/OUT.DAT", base) == "missing/OUT.DAT"


def test_build_tail_reports_every_rewrite(sandbox: StubSandbox) -> None:
    base = sandbox.guest_dir
    (base / "out").mkdir()
    tail, warnings = build_tail(["M9.ARC", "out/RESULT.DAT"], base)
    assert tail == ["M9.ARC", "./out/RESULT.DAT"]
    assert len(warnings) == 1
    assert "out/RESULT.DAT" in warnings[0] and "cpmemu.cc:1289" in warnings[0]


@needs_cpmemu
@needs_80un
def test_bare_relative_path_really_does_build_the_wrong_fcb(
    sandbox: StubSandbox,
) -> None:
    """Why :func:`tail_argument` exists, measured rather than asserted.

    filename_to_fcb (cpmemu.cc:1289) strips the directory only for ``/`` and
    ``./`` (:1298-1303), so ``sub/M9.ARC`` builds an FCB from ``SUB/M9`` and
    the ``/`` is replaced with ``_``.
    """
    sub = sandbox.guest_dir / "sub"
    sub.mkdir()
    shutil.copyfile(METHOD9_ARC, sub / "M9.ARC")
    cfg = sandbox.write_text(
        "raw.cfg",
        synthesize_cfg(
            program=UN80_COM,
            guest_dir=sandbox.guest_dir,
            lst_path=sandbox.lst_path,
            pun_path=sandbox.pun_path,
        ),
    )
    bare = sandbox.exec([str(CPMEMU), "--z80", str(cfg), "sub/M9.ARC"])
    assert b"invalid CP/M character '/'" in bare.stderr
    assert b"23 file(s) extracted" not in bare.stdout

    dotted = sandbox.exec([str(CPMEMU), "--z80", str(cfg), "./sub/M9.ARC"])
    assert b"invalid CP/M character" not in dotted.stderr
    assert b"23 file(s) extracted" in dotted.stdout


@needs_cpmemu
@needs_80un
def test_run_rewrites_the_bare_relative_path_and_says_so(
    sandbox: StubSandbox,
) -> None:
    """The same argument that mangles the FCB raw, through the adapter."""
    sub = sandbox.guest_dir / "sub"
    sub.mkdir()
    shutil.copyfile(METHOD9_ARC, sub / "M9.ARC")
    outcome = CpmemuBackend().run(
        RunRequest(sandbox=sandbox, program=str(UN80_COM), args=["sub/M9.ARC"])
    )
    assert b"23 file(s) extracted" in outcome.stdout
    assert b"invalid CP/M character" not in outcome.stderr
    assert outcome.raw is not None and "./sub/M9.ARC" in outcome.raw.argv
    assert any("./sub/M9.ARC" in w for w in outcome.warnings)


def test_guest_name_is_the_uppercase_of_the_host_name() -> None:
    # Measured on method9.arc's own output.
    assert guest_name_for("b5-time.inf") == "B5-TIME.INF"
    assert guest_name_for("-03mar86") == "-03MAR86"
    assert guest_name_for("/sbx/guest/ztim-s3.cpm") == "ZTIM-S3.CPM"


# ---------------------------------------------------------------------------
# SPEC.md 9 phase 1 acceptance
# ---------------------------------------------------------------------------

def _run_method9(sandbox: StubSandbox, **kw) -> tuple:
    backend = CpmemuBackend()
    outcome = backend.run(
        RunRequest(
            sandbox=sandbox,
            program=str(UN80_COM),
            args=["M9.ARC"],
            files_in=[
                FileIn(guest_name="M9.ARC", host_path=str(METHOD9_ARC), mode="binary")
            ],
            timeout_ms=10000,
            **kw,
        )
    )
    created = {p.name: p for p in sandbox.created_since_snapshot()}
    return outcome, created


@needs_cpmemu
@needs_80un
def test_acceptance_method9_under_binary_mode(sandbox: StubSandbox) -> None:
    """SPEC.md 9: 23 file(s) extracted, exit_reason jmp_0, B5-TIME.INF 1664."""
    outcome, created = _run_method9(sandbox)

    assert b"23 file(s) extracted" in outcome.stdout
    assert outcome.exit_reason is ExitReason.JMP_0
    assert created["b5-time.inf"].stat().st_size == 1664
    assert guest_name_for("b5-time.inf") == "B5-TIME.INF"
    # 23 members, and the staged input is not one of them.
    assert len(created) == 23
    assert "M9.ARC" not in created

    # SPEC.md 5.4 Invariant 4.
    assert outcome.exit_code is None
    assert outcome.exit_code_meaning is None
    assert outcome.unimplemented_bdos == []
    assert outcome.config_warnings == []
    assert outcome.warnings == []
    assert outcome.timed_out is False
    # The stderr the spec quotes, verbatim and unfiltered.
    assert outcome.stderr.startswith(b"CPU mode: Z80\n")
    assert outcome.stderr.endswith(b"Program exit via JMP 0\n")
    assert outcome.stderr_filtered == []
    # And the process still exited 0, which is why none of the above is read
    # off the return code.
    assert outcome.raw is not None and outcome.raw.rc == 0


@needs_cpmemu
@needs_80un
def test_acceptance_the_negative_case_still_exits_zero(sandbox: StubSandbox) -> None:
    """SPEC.md 9's second acceptance line, with the eol_convert its cfg omitted.

    SPEC.md 9 says the fixture "with default_mode:'auto'" gives 1 of 23. The
    spec's own auto.cfg (evidence/t3/auto.cfg) has no eol_convert line and
    cpmemu defaults it to true (cpmemu.cc:398), so the measured corruption is
    the pair. See test_auto_alone_no_longer_corrupts for the other half.
    """
    outcome, created = _run_method9(
        sandbox, default_mode=DefaultMode.AUTO, eol_convert=True
    )

    assert b"1 file(s) extracted" in outcome.stdout
    assert b"Error" in outcome.stdout
    assert b"23 file(s) extracted" not in outcome.stdout
    # Truncated at the first 0x1A: 1437 of 1664.
    assert created["b5-time.inf"].stat().st_size == 1437
    # Two files on disk for "1 file(s) extracted": the zero-byte -03MAR86
    # member was created before the write path corrupted the second one.
    assert sorted(created) == ["-03mar86", "b5-time.inf"]
    assert created["-03mar86"].stat().st_size == 0

    # A 96%-failed run, and every process-level signal says success.
    assert outcome.raw is not None and outcome.raw.rc == 0
    assert outcome.exit_reason is ExitReason.JMP_0
    assert outcome.stderr.endswith(b"Program exit via JMP 0\n")
    assert outcome.exit_code is None

    # SPEC.md 7.1's warning, verbatim, plus the mechanism.
    assert (
        "default_mode was 'auto'; cpmemu's auto mode never resolves on write"
        in outcome.warnings
    )
    assert any("1437" in w for w in outcome.warnings)


@needs_cpmemu
@needs_80un
def test_auto_alone_no_longer_corrupts_this_fixture(sandbox: StubSandbox) -> None:
    """A measured divergence from SPEC.md 9's second acceptance line.

    With eol_convert false -- which is what x80_cpm_run's schema defaults to
    (SPEC.md 6.4) and what this adapter always writes -- default_mode auto
    extracts 23 of 23 at the full 1664 bytes. write_with_conversion's guard is
    ``of.mode == MODE_BINARY || !of.eol_convert`` (cpmemu.cc:953), and the
    second half short-circuits before MODE_AUTO can reach the text branch. The
    warning is still emitted: the mode is still unresolved on write and the
    next fixture may not be so lucky.
    """
    outcome, created = _run_method9(
        sandbox, default_mode=DefaultMode.AUTO, eol_convert=False
    )
    assert b"23 file(s) extracted" in outcome.stdout
    assert created["b5-time.inf"].stat().st_size == 1664
    assert (
        "default_mode was 'auto'; cpmemu's auto mode never resolves on write"
        in outcome.warnings
    )
    assert not any("1437" in w for w in outcome.warnings)


@needs_cpmemu
@needs_80un
def test_acceptance_against_the_real_sandbox(tmp_path: Path) -> None:
    """The same acceptance case through ``eightymcp.sandbox``, not the stub.

    The adapter is written against :class:`~eightymcp.mbp.SandboxProtocol`, so
    this should be a no-op difference. It is asserted rather than assumed
    because "works against my own stub" is not evidence that it works.
    """
    sandbox_mod = pytest.importorskip("eightymcp.sandbox")
    with sandbox_mod.Sandbox(base_dir=tmp_path) as real:
        outcome = CpmemuBackend().run(
            RunRequest(
                sandbox=real,
                program=str(UN80_COM),
                args=["M9.ARC"],
                files_in=[
                    FileIn(
                        guest_name="M9.ARC",
                        host_path=str(METHOD9_ARC),
                        mode="binary",
                    )
                ],
            )
        )
        created = {p.name: p for p in real.created_since_snapshot()}
        assert b"23 file(s) extracted" in outcome.stdout
        assert outcome.exit_reason is ExitReason.JMP_0
        assert outcome.exit_code is None
        assert created["b5-time.inf"].stat().st_size == 1664
        assert len(created) == 23
        assert outcome.list_output_path == real.lst_path


def test_run_result_has_no_exit_code_field_at_all() -> None:
    """SPEC.md 9: "no exit_code field anywhere in the result"."""
    names = {f.name for f in dataclasses.fields(RunResult)}
    assert "exit_code" not in names
    assert "exit_code_meaning" not in names


# ---------------------------------------------------------------------------
# LST: and PUN:
# ---------------------------------------------------------------------------

@needs_cpmemu
def test_list_and_punch_streams_land_in_the_sandbox(sandbox: StubSandbox) -> None:
    """Appendix B item 2, both halves.

    dev.com writes 'X' and 'Y' to the LIST device (BDOS 5) and 'P' to the
    PUNCH device (BDOS 4). With the devices routed to sandbox files those
    bytes are recoverable; with an empty path cpmemu warns on every run and
    the LIST bytes appear in the guest's *stdout* as "[PRINTER] c".
    """
    prog = com(
        sandbox,
        "dev.com",
        "1E 58 0E 05 CD 05 00"  # LIST 'X'
        " 1E 50 0E 04 CD 05 00"  # PUNCH 'P'
        " 1E 59 0E 05 CD 05 00"  # LIST 'Y'
        " C3 00 00",  # JMP 0
    )
    outcome = CpmemuBackend().run(RunRequest(sandbox=sandbox, program=str(prog)))

    assert outcome.exit_reason is ExitReason.JMP_0
    assert outcome.list_output_path == sandbox.lst_path
    assert outcome.punch_output_path == sandbox.pun_path
    assert sandbox.lst_path.read_bytes() == b"XY"
    assert sandbox.pun_path.read_bytes() == b"P"
    # Not in the console transcript, which is the point.
    assert b"[PRINTER]" not in outcome.stdout
    assert outcome.config_warnings == []


@needs_cpmemu
def test_an_empty_printer_path_is_what_the_sandbox_routing_prevents(
    sandbox: StubSandbox,
) -> None:
    """The failure mode Appendix B item 2 names, provoked directly."""
    prog = com(sandbox, "dev.com", "1E 58 0E 05 CD 05 00 C3 00 00")
    cfg = sandbox.write_text(
        "empty.cfg",
        f"program = {prog}\ncd = {sandbox.guest_dir}\ndefault_mode = binary\n"
        f"eol_convert = false\nprinter = \naux_output = \n",
    )
    result = sandbox.exec([str(CPMEMU), "--z80", str(cfg)])
    assert b"Warning: Cannot open printer file '': " in result.stderr
    assert b"[PRINTER] X" in result.stdout  # the LIST byte, in the transcript
    facts = parse_stderr(result.stderr.decode())
    assert facts.config_warnings and "printer file ''" in facts.config_warnings[0]


# ---------------------------------------------------------------------------
# exit_reason sentinels and diagnostics, against the binary
# ---------------------------------------------------------------------------

@needs_cpmemu
@pytest.mark.parametrize(
    "name,code,expected",
    [
        # BDOS 0 -> program_exit("System reset"), cpmemu.cc:1410
        ("reset.com", "0E 00 CD 05 00", ExitReason.BDOS_0),
        # JMP 0FE03h, the BIOS WBOOT entry -> cpmemu.cc:2745
        ("wboot.com", "C3 03 FE", ExitReason.WBOOT),
        # JMP 0 -> cpmemu.cc:1375
        ("jmp0.com", "C3 00 00", ExitReason.JMP_0),
    ],
)
def test_exit_reason_comes_from_the_stderr_sentinel(
    sandbox: StubSandbox, name: str, code: str, expected: ExitReason
) -> None:
    prog = com(sandbox, name, code)
    outcome = CpmemuBackend().run(RunRequest(sandbox=sandbox, program=str(prog)))
    assert outcome.exit_reason is expected
    assert outcome.exit_code is None  # on every one of them


@needs_cpmemu
def test_unimplemented_bdos_is_collected_and_deduplicated(
    sandbox: StubSandbox,
) -> None:
    """cpmemu.cc:1580 prints one line per call, not per distinct function."""
    prog = com(
        sandbox,
        "bad.com",
        "0E 64 CD 05 00"  # BDOS 100
        " 0E 2D CD 05 00"  # BDOS 45
        " 0E 64 CD 05 00"  # BDOS 100 again
        " C3 00 00",
    )
    outcome = CpmemuBackend().run(RunRequest(sandbox=sandbox, program=str(prog)))
    assert outcome.stderr.count(b"Unimplemented BDOS function 100") == 2
    assert outcome.unimplemented_bdos == [100, 45]
    assert outcome.exit_reason is ExitReason.JMP_0


@needs_cpmemu
def test_a_runaway_guest_is_killed_by_the_deadline(sandbox: StubSandbox) -> None:
    """SPEC.md 5.5: cpmemu's only guard is a 9e9-instruction watchdog, so the
    wall clock is the supervisor's."""
    prog = com(sandbox, "spin.com", "C3 00 01")  # JMP 0100h
    started = time.monotonic()
    outcome = CpmemuBackend().run(
        RunRequest(sandbox=sandbox, program=str(prog), timeout_ms=1200)
    )
    elapsed_ms = (time.monotonic() - started) * 1000

    assert outcome.timed_out is True
    assert outcome.exit_reason is ExitReason.TIMEOUT
    assert outcome.exit_code is None
    assert elapsed_ms < 6000, "the deadline did not kill the process group"
    assert outcome.raw is not None and outcome.raw.signal == signal.SIGKILL


@needs_cpmemu
def test_a_guest_reading_past_end_of_input_gives_up(sandbox: StubSandbox) -> None:
    """cpmemu.cc:209, CONSOLE_EOF_LIMIT at :199. 1024 blocking reads."""
    prog = com(
        sandbox,
        "reader.com",
        "0E 01 CD 05 00 C3 00 01",  # BDOS 1 (console in), loop
    )
    outcome = CpmemuBackend().run(
        RunRequest(sandbox=sandbox, program=str(prog), timeout_ms=10000)
    )
    assert outcome.exit_reason is ExitReason.EOF_GIVEUP
    assert b"console reads past end of input" in outcome.stderr
    assert outcome.timed_out is False


# ---------------------------------------------------------------------------
# stderr parsing, in isolation
# ---------------------------------------------------------------------------

def test_parse_stderr_reads_every_sentinel() -> None:
    for text, expected in [
        ("Program exit via JMP 0\n", ExitReason.JMP_0),
        ("System reset\n", ExitReason.BDOS_0),
        ("BIOS WBOOT called - exiting\n", ExitReason.WBOOT),
        ("Reached instruction limit\nPC = 0x0103\n", ExitReason.INSTRUCTION_LIMIT),
        ("\n[Exiting: 1024 console reads past end of input]\n", ExitReason.EOF_GIVEUP),
        ("\n[Exiting: 5 consecutive ^C received]\n", ExitReason.CTRL_C),
    ]:
        assert parse_stderr(text).exit_reason is expected


def test_parse_stderr_ignores_the_banner_and_keeps_the_rest() -> None:
    facts = parse_stderr(
        "CPU mode: Z80\n"
        "Loaded 21336 bytes from /x/80un.com\n"
        "Config line 4: 'defualt_mode' is being read as a file mapping; the "
        "directive is spelled 'default_mode'\n"
        "Warning: Cannot open aux output file '': No such file or directory\n"
        "Note: '--progress' taken as an emulator option, not passed to the program\n"
        "Warning: invalid CP/M character '/' in filename 'sub/M9.ARC'\n"
        "Unimplemented BDOS function 45\n"
        "Program exit via JMP 0\n"
    )
    assert facts.exit_reason is ExitReason.JMP_0
    assert facts.unimplemented_bdos == [45]
    assert len(facts.config_warnings) == 2
    assert facts.options_eaten == ["--progress"]
    assert any("invalid CP/M character" in w for w in facts.warnings)
    assert facts.program_unreadable is None


def test_parse_stderr_separates_our_config_from_their_program() -> None:
    ours = parse_stderr("Cannot open config file: /sbx/cpmemu.cfg\n")
    assert ours.config_unreadable == "/sbx/cpmemu.cfg"
    assert ours.program_unreadable is None

    theirs = parse_stderr(
        "CPU mode: Z80\nCannot open /x/nope.com: No such file or directory\n"
    )
    assert theirs.program_unreadable == ("/x/nope.com", "No such file or directory")
    assert theirs.exit_reason is None


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------

@needs_cpmemu
def test_a_missing_program_is_refused_before_the_exec(
    sandbox: StubSandbox, tmp_path: Path
) -> None:
    """cpmemu's own answer is rc 1, an empty stdout and one stderr line, which
    reads like a program that ran and printed nothing."""
    with pytest.raises(ToolExecutionError) as exc:
        CpmemuBackend().run(
            RunRequest(sandbox=sandbox, program=str(tmp_path / "nope.com"))
        )
    body = exc.value.err.to_json()
    assert body["error"] == "bad_argument"
    assert body["argument"] == "program"
    assert body["profile"] == PROFILE


@needs_cpmemu
def test_an_unsupported_cpu_is_refused(sandbox: StubSandbox) -> None:
    prog = com(sandbox, "jmp0.com", "C3 00 00")
    with pytest.raises(ToolExecutionError) as exc:
        CpmemuBackend().run(
            RunRequest(sandbox=sandbox, program=str(prog), cpu="386")
        )
    body = exc.value.err.to_json()
    assert body["error"] == "bad_argument" and body["argument"] == "cpu"


@needs_cpmemu
@pytest.mark.parametrize("cpu,banner", [("z80", b"CPU mode: Z80"), ("8080", b"CPU mode: 8080")])
def test_the_cpu_flag_is_always_written_out(
    sandbox: StubSandbox, cpu: str, banner: bytes
) -> None:
    prog = com(sandbox, "jmp0.com", "C3 00 00")
    outcome = CpmemuBackend().run(
        RunRequest(sandbox=sandbox, program=str(prog), cpu=cpu)
    )
    assert outcome.stderr.startswith(banner)
    assert outcome.raw is not None
    assert ("--z80" if cpu == "z80" else "--8080") in outcome.raw.argv


@needs_cpmemu
def test_env_that_would_undo_the_device_routing_is_dropped(
    sandbox: StubSandbox, tmp_path: Path
) -> None:
    """cpmemu reads the environment after the config file, so CPM_PRINTER
    replaces the printer directive (cpmemu README)."""
    elsewhere = tmp_path / "elsewhere.lst"
    prog = com(sandbox, "dev.com", "1E 58 0E 05 CD 05 00 C3 00 00")
    outcome = CpmemuBackend().run(
        RunRequest(
            sandbox=sandbox,
            program=str(prog),
            env={"CPM_PRINTER": str(elsewhere), "SOMETHING_ELSE": "kept"},
        )
    )
    assert sandbox.lst_path.read_bytes() == b"X"
    assert not elsewhere.exists()
    assert any("CPM_PRINTER was dropped" in w for w in outcome.warnings)


@needs_cpmemu
def test_files_in_are_staged_and_excluded_from_what_the_run_created(
    sandbox: StubSandbox,
) -> None:
    prog = com(sandbox, "jmp0.com", "C3 00 00")
    CpmemuBackend().run(
        RunRequest(
            sandbox=sandbox,
            program=str(prog),
            files_in=[
                FileIn(
                    guest_name="IN.DAT",
                    content_b64=base64.b64encode(b"hello").decode(),
                )
            ],
        )
    )
    assert (sandbox.guest_dir / "IN.DAT").read_bytes() == b"hello"
    assert sandbox.created_since_snapshot() == []


# ---------------------------------------------------------------------------
# The other five required ops
# ---------------------------------------------------------------------------

@needs_cpmemu
def test_boot_is_a_no_op_that_names_what_it_ignored(sandbox: StubSandbox) -> None:
    backend = CpmemuBackend()
    plain = backend.boot(BootSpec(sandbox=sandbox, profile=PROFILE))
    assert plain.ok is True and plain.wall_ms == 0 and plain.warnings == []

    loaded = backend.boot(
        BootSpec(
            sandbox=sandbox,
            profile=PROFILE,
            rom="rom.bin",
            disks=[DiskSpec(unit=0, image_id="hd1k_combo")],
            consoles=4,
        )
    )
    assert loaded.ok is True
    assert len(loaded.warnings) == 2
    assert "rom, disks" in loaded.warnings[0]
    assert "mpm2" in loaded.warnings[1]


@needs_cpmemu
def test_boot_refuses_a_profile_this_backend_does_not_serve(
    sandbox: StubSandbox,
) -> None:
    with pytest.raises(BackendFailure) as exc:
        CpmemuBackend().boot(BootSpec(sandbox=sandbox, profile="cpm3"))
    body = exc.value.err.to_json()
    assert body["error"] == "backend_failure"
    assert body["requested_profile"] == "cpm3"
    assert body["serves"] == [PROFILE]


@needs_cpmemu
def test_send_queues_stdin_for_the_next_run(sandbox: StubSandbox) -> None:
    """A guest that reads two characters and echoes them, so the queued bytes
    have to have arrived."""
    # BDOS 1 does not echo a redirected byte (measured: a read-only guest
    # produced an empty stdout), so the guest writes each byte back with
    # BDOS 2 and the transcript proves the byte arrived.
    prog = com(
        sandbox,
        "echo2.com",
        "0E 01 CD 05 00 5F 0E 02 CD 05 00"  # BDOS 1 read -> E -> BDOS 2 write
        " 0E 01 CD 05 00 5F 0E 02 CD 05 00"  # again
        " C3 00 00",
    )
    backend = CpmemuBackend()
    sent = backend.send(SendRequest(data=b"AB"))
    assert sent.bytes_sent == 2 and sent.paced_by == "not_waited"

    outcome = backend.run(RunRequest(sandbox=sandbox, program=str(prog)))
    assert b"AB" in outcome.stdout
    # The queue is consumed, not replayed.
    assert backend.send(SendRequest(data=b"")).bytes_sent == 0
    second = backend.run(RunRequest(sandbox=sandbox, program=str(prog)))
    assert b"AB" not in second.stdout


@needs_cpmemu
def test_recv_hands_back_the_last_run_once(sandbox: StubSandbox) -> None:
    prog = com(sandbox, "jmp0.com", "C3 00 00")
    backend = CpmemuBackend()
    assert backend.recv(RecvRequest()).data == b""
    backend.run(RunRequest(sandbox=sandbox, program=str(prog)))
    first = backend.recv(RecvRequest(max_bytes=1))
    assert first.more is (len(backend._last_stdout) > 1)
    rest = backend.recv(RecvRequest())
    assert first.data + rest.data == backend._last_stdout
    assert rest.more is False


@needs_cpmemu
def test_idle_says_which_fact_answered(sandbox: StubSandbox) -> None:
    prog = com(sandbox, "jmp0.com", "C3 00 00")
    backend = CpmemuBackend()
    before = backend.idle(IdleRequest())
    assert before.idle is True and before.source == "not_started"
    backend.run(RunRequest(sandbox=sandbox, program=str(prog)))
    after = backend.idle(IdleRequest())
    assert after.idle is True and after.source == "process_exited"
    # Never claimed as a real signal (SPEC.md 1.4).
    assert backend.caps().has_idle_signal is False


@needs_cpmemu
def test_stop_reports_the_last_exit_and_keeps_stderr(sandbox: StubSandbox) -> None:
    prog = com(sandbox, "jmp0.com", "C3 00 00")
    backend = CpmemuBackend()
    empty = backend.stop(StopRequest())
    assert empty.stopped is True and empty.exit_code is None and empty.stderr_tail == b""

    backend.run(RunRequest(sandbox=sandbox, program=str(prog)))
    stopped = backend.stop(StopRequest())
    assert stopped.stopped is True
    # The process return code, which says nothing about the guest.
    assert stopped.exit_code == 0
    assert b"Program exit via JMP 0" in stopped.stderr_tail


@needs_cpmemu
def test_call_routes_caps_and_refuses_the_typed_ops() -> None:
    backend = CpmemuBackend()
    assert backend.call("caps").backend == BACKEND_NAME
    with pytest.raises(ToolExecutionError) as exc:
        backend.call("run", {})
    assert "typed arguments" in exc.value.err.to_json()["reason"]


# ---------------------------------------------------------------------------
# The write-mode matrix, measured
# ---------------------------------------------------------------------------

@needs_cpmemu
@needs_80un
def test_write_corruption_needs_both_auto_and_eol_convert(tmp_path):
    """SPEC.md 5.4 Invariant 3, pinned as a full matrix rather than one point.

    SPEC.md 9's acceptance line named ``default_mode:"auto"`` alone. That is
    not sufficient: measured against cpmemu 4.8.0, the corruption needs a
    non-binary ``default_mode`` AND ``eol_convert = true``. ``binary`` is
    sufficient protection on its own whatever ``eol_convert`` says, which is
    why it is the schema default. Running cpmemu with no config file lands on
    the corrupting pair, because those are its built-in defaults.

    The matrix is here so neither half of the claim can rot out of the docs.
    """
    import shutil
    import subprocess

    def run(cfg_body: str | None, tag: str) -> tuple[str, int | None]:
        work = tmp_path / tag
        work.mkdir()
        shutil.copy(UN80_COM, work / "80un.com")
        shutil.copy(METHOD9_ARC, work / "M9.ARC")
        if cfg_body is None:
            argv = ["./80un.com", "M9.ARC"]
        else:
            (work / "r.cfg").write_text(f"program = {work}/80un.com\n" + cfg_body)
            argv = ["r.cfg", "M9.ARC"]
        out = subprocess.run(
            [str(CPMEMU), *argv], cwd=work, capture_output=True, timeout=60
        )
        tail = out.stdout.decode("latin-1").replace("\r", "").strip().splitlines()[-1]
        b5 = work / "b5-time.inf"
        return tail, (b5.stat().st_size if b5.is_file() else None)

    def cfg(mode: str, eol: str) -> str:
        return f"default_mode = {mode}\neol_convert = {eol}\n"

    GOOD = ("23 file(s) extracted", 1664)
    BAD = ("1 file(s) extracted", 1437)

    # binary protects regardless of eol_convert -- this is why it is the default.
    assert run(cfg("binary", "false"), "bin_f") == GOOD
    assert run(cfg("binary", "true"), "bin_t") == GOOD

    # auto and text are only safe with the conversion off.
    assert run(cfg("auto", "false"), "auto_f") == GOOD
    assert run(cfg("text", "false"), "text_f") == GOOD

    # Both halves together are what corrupts, and SPEC.md 9 names only the first.
    assert run(cfg("auto", "true"), "auto_t") == BAD
    assert run(cfg("text", "true"), "text_t") == BAD

    # No config file at all gets the corrupting pair as cpmemu's own defaults.
    assert run(None, "nocfg") == BAD
    assert run("\n", "emptycfg") == BAD
