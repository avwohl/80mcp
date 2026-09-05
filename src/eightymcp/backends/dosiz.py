"""The ``dosiz`` one-shot adapter -- MBP shape 3 (SPEC.md 5.3).

``dosiz`` traps INT 21h/31h/67h to the host filesystem, so there is no image to
mount and no boot: ``boot`` is a no-op, ``run`` is one deadlined exec in the
sandbox, and there are no debugger ops. What this module actually adds over
``subprocess.run`` is five measured behaviours that an agent gets wrong every
time:

1. **The argv[0] trap.** ``dosiz`` builds the guest's ``argv[0]`` as ``"C:\\"``
   plus the program path with its leading ``./`` or ``/`` stripped, slashes
   turned into backslashes and the whole thing uppercased
   (``dosiz/src/bridge.cc:7109-7122``). DJGPP's go32-v2 stub reopens
   ``argv[0]`` to load its COFF payload, so an *absolute* host path becomes a
   nonexistent ``C:\\`` path and the run dies before ``main``. Measured, with
   this build::

       $ dosiz /…/dz-probe/DJ_PRINTF.exe
       C:\\PRIVATE\\TMP\\…\\DZ-PROBE\\DJ_PRINTF.EXE: can't open   # rc 102

       $ cd /…/dz-probe && dosiz DJ_PRINTF.exe
       int=42 str=hello …                                        # rc 7

   :meth:`DosizBackend.run` therefore stages the program into the sandbox and
   runs with ``cwd`` = the guest directory and a bare relative name. There is
   no argument that turns this off.

2. **The exit code is real, and rc 1 is ambiguous.** DOS ``INT 21h AH=4Ch``'s
   AL reaches the host exit status (``bridge.cc:5438``: ``s_exit_code =
   reg_al``); measured, ``DJ_PRINTF.exe`` gives rc 7. But ``dosiz`` also
   ``_exit(1)``s when it could not load the program (``bridge.cc:8429-8431``)
   and when the guest took an unhandled protected-mode fault
   (``bridge.cc:1988``, ``:2013``). ``exit_code_meaning`` is decided from
   stderr, never from the number -- see :func:`classify_exit`.

3. **Two stderr lines print on every single run** and are filtered by name into
   ``diagnostics.stderr_filtered`` rather than silently swallowed (SPEC.md
   5.5). They are :data:`~eightymcp.types.DOSIZ_STDERR_NOISE`.

4. **An unimplemented INT 21h AH does not stop the program.** ``dosiz`` logs it
   once per distinct AH and returns invalid-function
   (``bridge.cc:6141-6150``), so a run can exit 0 with a degraded result. The
   whole set comes back in ``diagnostics.unimplemented_int21``.

5. **There is no internal timeout.** Measured: a 2-byte ``EB FE`` spin loop ran
   until SIGKILL. The deadline is the sandbox's (SPEC.md 5.5) and this module
   never invents one.

Two hermeticity notes, both measured in the source rather than assumed:

* ``dosiz`` auto-loads a sidecar ``<stem>.cfg`` sitting next to the program
  (``dosiz/src/dosiz.cc:104-109``) -- which would silently change
  ``default_mode`` and ``eol_convert`` for a program a caller staged. Passing
  an *explicit* cfg as the first argument takes the other branch
  (``dosiz.cc:91-95``) and no sidecar is consulted, so this adapter always
  synthesizes one. The same branch skips ``resolve_program_path``, so
  ``DOSIZ_PATH`` cannot pull in a program from outside the sandbox either.
* ``dosiz``'s debug switches (``DOSIZ_TRACE``, ``DOSIZ_NO_DPMI``,
  ``DOSIZ_FORCE_DPMI``, …) are read with a bare ``getenv() != nullptr``
  (``dosiz/src/debug_settings.cpp:22-24``), so an *empty* value still counts as
  set and they cannot be neutralised by overriding them. Keeping them out is
  the sandbox's hermetic base env's job; this adapter warns if a caller tries
  to pass one in.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from ..mbp import (
    Backend,
    BackendFailure,
    BootOutcome,
    BootSpec,
    IdleOutcome,
    IdleRequest,
    Op,
    RecvOutcome,
    RecvRequest,
    RunOutcome,
    RunRequest,
    SendOutcome,
    SendRequest,
    StopOutcome,
    StopRequest,
)
from ..types import (
    DOSIZ_STDERR_NOISE,
    AdapterShape,
    Caps,
    DefaultMode,
    ExecResult,
    ExitCodeMeaning,
    ExitReason,
    Family,
    PmFault,
    Tier,
    UnimplementedInt21,
    decode_guest,
)

__all__ = [
    "BACKEND_NAME",
    "BINARY_ENV_VAR",
    "SERVED_PROFILES",
    "GUEST_ENV_PASSTHROUGH",
    "DosizBackend",
    "find_binary",
    "probe_version",
    "dos_argv0",
    "filter_noise",
    "parse_unimplemented_int21",
    "parse_pm_fault",
    "find_loader_failure",
    "detect_argv0_trap",
    "classify_exit",
    "synthesize_cfg",
]

#: The id this backend reports in ``profiles[].backend`` and in the ``backend``
#: field of every structured error it raises.
BACKEND_NAME = "dosiz"

#: Checked before ``PATH``. ``80mcp doctor`` prints whichever won.
BINARY_ENV_VAR = "EIGHTYMCP_DOSIZ"

#: SPEC.md 6.4: ``x80_dos_run``'s ``profile`` enum is ``["dos-hosted",
#: "freedos"]``. ``freedos`` boots a real kernel on ``emu88`` and is not this
#: backend (SPEC.md 3.7: it stays ``available:false`` until one job proves it).
SERVED_PROFILES: tuple[str, ...] = ("dos-hosted",)

#: The only host environment variables that reach the guest's DOS environment
#: block, from ``dosiz/src/bridge.cc:7089-7090``. Everything else a caller
#: passes as ``env`` is set on the *host* dosiz process and is invisible to the
#: DOS program, which is worth a warning rather than a silent no-op.
GUEST_ENV_PASSTHROUGH: frozenset[str] = frozenset({
    "HOME", "USER", "TMPDIR", "LANG", "TZ",
    "DJGPP", "WATCOM", "INCLUDE", "LIB", "LIBPATH",
})

#: ``bridge.cc:7081-7085`` appends this to the guest's ``PATH``, and
#: ``config.cc:218-229`` searches it for a bare program name. Both would reach
#: outside the sandbox, so the adapter refuses to forward it.
_HOST_ONLY_ENV: frozenset[str] = frozenset({"DOSIZ_PATH"})

#: The name of the cfg this adapter writes into the sandbox root. Not in the
#: guest directory: a file there would show up in ``created_since_snapshot()``
#: and be reported to the agent as something the program made.
CFG_NAME = "dosiz.cfg"

#: ``build_psp`` writes the command tail at PSP+0x80 by joining args with
#: spaces (``dosiz/src/bridge.cc:7128-7160``), so a space inside the program
#: name is ambiguous to the guest's own argv splitter.
_SPACE_IN_PROGRAM_NAME = (
    "program name {name!r} contains a space; DOS builds the command tail by "
    "splitting on whitespace, so the guest may see a truncated argv[0]"
)


# ---------------------------------------------------------------------------
# Finding the binary
# ---------------------------------------------------------------------------

def find_binary(explicit: str | os.PathLike[str] | None = None) -> str | None:
    """Locate the ``dosiz`` binary, or return None.

    Order: an explicit path, then :data:`BINARY_ENV_VAR`, then ``PATH``.
    Backends are never vendored (SPEC.md 5.1 rule 1) and an absent one is a
    profile reporting ``ready:false``, never a crash -- so this returns None
    rather than raising, and every caller is expected to handle None.
    """
    for candidate in (explicit, os.environ.get(BINARY_ENV_VAR)):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            return None
    found = shutil.which(BACKEND_NAME)
    return found or None


def probe_version(binary: str | os.PathLike[str]) -> str | None:
    """``dosiz --version`` -> ``"dosiz 0.1.0-dev (backend: emu88)"`` on stdout,
    rc 0 (``dosiz/src/dosiz.cc:70-75``). Returns the line, or None if the
    binary would not run.
    """
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    line = decode_guest(proc.stdout).strip().splitlines()
    return line[0] if line else None


# ---------------------------------------------------------------------------
# argv[0], reimplemented so the trap can be asserted rather than described
# ---------------------------------------------------------------------------

def dos_argv0(program_path: str) -> str:
    """The ``argv[0]`` ``dosiz`` will hand the guest for this program path.

    A transcription of ``dosiz/src/bridge.cc:7109-7122``. It exists so
    :mod:`tests.test_dosiz` can assert the failure mode is real without
    depending on the emulator being installed, and so the adapter can name the
    exact string in an error when the trap somehow fires anyway.
    """
    rel = program_path
    if len(rel) >= 2 and rel[0] == "." and rel[1] in "/\\":
        rel = rel[2:]
    elif rel and rel[0] in "/\\":
        rel = rel[1:]
    rel = "".join("\\" if c == "/" else c.upper() for c in rel)
    return "C:\\" + rel


# ---------------------------------------------------------------------------
# stderr parsing
# ---------------------------------------------------------------------------

def filter_noise(stderr_text: str) -> tuple[str, list[str]]:
    """Split the two unconditional lines out of stderr.

    Returns ``(kept_text, removed_lines)``. SPEC.md 5.5 requires the filtering
    be *recorded*, not silent, so the removed lines go to
    ``diagnostics.stderr_filtered`` verbatim. Matching is on the exact string
    from :data:`~eightymcp.types.DOSIZ_STDERR_NOISE`, never on a substring or a
    prefix: a future ``dosiz`` that changes the wording must show up as an
    unfiltered line rather than be quietly absorbed.
    """
    noise = set(DOSIZ_STDERR_NOISE)
    kept: list[str] = []
    removed: list[str] = []
    for line in stderr_text.split("\n"):
        stripped = line.rstrip("\r")
        if stripped in noise:
            removed.append(stripped)
        else:
            kept.append(line)
    return "\n".join(kept), removed


#: ``bridge.cc:6143-6148``. Printed once per distinct AH: a ``static
#: std::set<uint8_t> warned`` guards it (``bridge.cc:6141``), so one run yields
#: the whole set and the adapter does not need to deduplicate.
_INT21_RE = re.compile(
    r"^dosiz: unimplemented INT 21h AH=([0-9A-Fa-f]{2})h "
    r"\(AL=([0-9A-Fa-f]{2})h BX=([0-9A-Fa-f]{4})h "
    r"CX=([0-9A-Fa-f]{4})h DX=([0-9A-Fa-f]{4})h\)",
    re.MULTILINE,
)


def parse_unimplemented_int21(stderr_text: str) -> list[UnimplementedInt21]:
    """Collect ``diagnostics.unimplemented_int21``.

    Measured against this build with a hand-assembled ``UNIMP.COM``
    (``B4 7F CD 21  B4 6B CD 21  B8 03 4C CD 21``)::

        dosiz: unimplemented INT 21h AH=7Fh (AL=00h BX=0000h CX=0000h DX=0000h) -- returning invalid-function, program continues
        dosiz: unimplemented INT 21h AH=6Bh (AL=01h BX=0000h CX=0000h DX=0000h) -- returning invalid-function, program continues

    The line carries AL, not AX; ``UnimplementedInt21.ax`` is reassembled as
    ``(AH << 8) | AL`` so the schema's ``{ah,ax,bx,cx,dx}`` shape is honest
    about the register the guest actually had in AX at the trap.
    """
    out: list[UnimplementedInt21] = []
    seen: set[int] = set()
    for m in _INT21_RE.finditer(stderr_text):
        ah = int(m.group(1), 16)
        al = int(m.group(2), 16)
        if ah in seen:
            continue
        seen.add(ah)
        out.append(UnimplementedInt21(
            ah=ah,
            ax=(ah << 8) | al,
            bx=int(m.group(3), 16),
            cx=int(m.group(4), 16),
            dx=int(m.group(5), 16),
        ))
    return out


#: ``bridge.cc:1936-1940`` (32-bit gate) and ``:1954-1958`` (16-bit gate). The
#: 32-bit form is measured; see :func:`parse_pm_fault`.
_LE_FAULT_HEAD_RE = re.compile(
    r"^dosiz: LE client exception 0x([0-9A-Fa-f]{2}) \((.*?)\) -- terminating\.",
    re.MULTILINE,
)
_LE_FAULT_AT_RE = re.compile(
    r"^\s+fault at CS:E?IP = ([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4,8})"
    r"\s+E?FLAGS = ([0-9A-Fa-f]{4,8})",
    re.MULTILINE,
)
_LE_FAULT_ERR_RE = re.compile(
    r"^\s+error code = 0x([0-9A-Fa-f]+)", re.MULTILINE)
#: ``bridge.cc:1971-1975`` / ``:1993-1997``: no per-vector info, so no CS:EIP.
_LE_FAULT_UNKNOWN_RE = re.compile(
    r"^dosiz: LE client PM exception \(vector unknown\) -- terminating\.",
    re.MULTILINE,
)
#: ``bridge.cc:2204-2207``.
_PM_NO_HANDLER_RE = re.compile(
    r"^dosiz: PM exception vec=(-?\d+) at ([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4,8}) "
    r"err=0x([0-9A-Fa-f]+) \(no user handler installed, terminating\)",
    re.MULTILINE,
)
#: ``bridge.cc:2188-2191``.
_PM_RECURSIVE_RE = re.compile(
    r"^dosiz: PM exception dispatcher in recursive-fault loop "
    r"\(vec=(-?\d+) cs:eip=([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4,8}) "
    r"err=0x([0-9A-Fa-f]+)\) -- terminating",
    re.MULTILINE,
)
#: The register dump that follows every LE client exception,
#: ``bridge.cc:1981-1987`` (32-bit) and ``:2007-2012`` (16-bit).
_REG_PAIR_RE = re.compile(
    r"\b(E?(?:AX|BX|CX|DX|SI|DI|BP|SP|IP|FLAGS)|[CDEFGS]S)=([0-9A-Fa-f]+)\b")


def parse_pm_fault(stderr_text: str) -> PmFault | None:
    """Parse ``diagnostics.pm_fault`` out of stderr, or None.

    Four shapes reach here, all from ``dosiz/src/bridge.cc``. The 32-bit LE
    client form is measured -- built by patching ``dosiz/tests/gen_le_min.py``
    to emit ``0F 0B`` (UD2) as the LE entry point, which produced exactly::

        dosiz: LE client exception 0x06 (#UD  invalid opcode) -- terminating.
          fault at CS:EIP = 0024:00020010  EFLAGS = 00000000
          EAX=00000000 EBX=00000000 ECX=00000000 EDX=00000000
          ESI=00000000 EDI=00000000 EBP=00000000
          DS=001c ES=002c FS=0000 GS=0000

    with host rc 1 (``bridge.cc:1988`` sets ``s_exit_code = 1``). The 16-bit
    gate form, the vector-unknown form and the two DPMI-dispatcher forms are
    transcribed from the format strings; none of the 152 fixtures in
    ``dosiz/tests`` faults, so they are covered by constructed input in
    ``tests/test_dosiz.py`` rather than by a live run.

    ``regs`` carries every ``NAME=hex`` pair in the block plus ``vec`` where
    the line reports one -- the schema pins ``cs``, ``eip`` and ``err`` and
    leaves the rest free-form, and throwing away a register dump that a human
    debugging a fault would want is the wrong trade.
    """
    regs: dict[str, int] = {}
    cs = eip = err = None
    vec: int | None = None

    head = _LE_FAULT_HEAD_RE.search(stderr_text)
    if head is not None:
        vec = int(head.group(1), 16)
        at = _LE_FAULT_AT_RE.search(stderr_text, head.end())
        if at is not None:
            cs = int(at.group(1), 16)
            eip = int(at.group(2), 16)
            regs["eflags"] = int(at.group(3), 16)
        e = _LE_FAULT_ERR_RE.search(stderr_text, head.end())
        err = int(e.group(1), 16) if e is not None else 0
    elif (unk := _LE_FAULT_UNKNOWN_RE.search(stderr_text)) is not None:
        # No vector, so no CS:EIP -- the frame is dumped raw for a human. Report
        # zeros rather than dropping the fault: an agent needs to know it
        # happened even when the address is unrecoverable.
        cs, eip, err = 0, 0, 0
        head = unk
    else:
        for pattern in (_PM_NO_HANDLER_RE, _PM_RECURSIVE_RE):
            m = pattern.search(stderr_text)
            if m is not None:
                vec = int(m.group(1), 10)
                cs = int(m.group(2), 16)
                eip = int(m.group(3), 16)
                err = int(m.group(4), 16)
                head = m
                break

    if cs is None or eip is None or err is None:
        return None

    for name, value in _REG_PAIR_RE.findall(stderr_text[head.end():]):
        regs.setdefault(name.lower(), int(value, 16))
    if vec is not None:
        regs["vec"] = vec
    return PmFault(cs=cs, eip=eip, err=err, regs=regs)


#: Every stderr line that means "the program never ran", with the source that
#: prints it. All of them precede ``s_exit_code = 1`` -- ``dosiz.cc`` returns 1
#: from ``main`` (``:80``, ``:87``, ``:93``, ``:102``, ``:106``, ``:117``,
#: ``:121``) and ``bridge.cc:8429-8431`` sets it when ``load_program`` fails --
#: which is the rc that is otherwise indistinguishable from a guest that
#: deliberately exited 1.
_LOADER_FAILURE_RES: tuple[re.Pattern[str], ...] = (
    # dosiz/src/dosiz.cc
    re.compile(r"^dosiz: unknown option: .*$", re.MULTILINE),
    re.compile(r"^dosiz: cannot find '.*' on DOSIZ_PATH$", re.MULTILINE),
    re.compile(r"^dosiz: no program to run\b.*$", re.MULTILINE),
    # dosiz/src/bridge.cc, read_file() and load_program()
    re.compile(r"^dosiz: cannot open .*: .*$", re.MULTILINE),
    re.compile(r"^dosiz: read error on .*$", re.MULTILINE),
    re.compile(r"^dosiz: .* has no MZ signature$", re.MULTILINE),
    re.compile(r"^dosiz: .* too small to be an MZ \.EXE$", re.MULTILINE),
    re.compile(r"^dosiz: .* too large for \.COM \(\d+ bytes\)$", re.MULTILINE),
    re.compile(r"^dosiz: .* header describes \d+ bytes, file has \d+$", re.MULTILINE),
    re.compile(r"^dosiz: .* reloc \d+ out of bounds$", re.MULTILINE),
    re.compile(r"^dosiz: .* is LE/LX but le_load_objects failed$", re.MULTILINE),
    re.compile(r"^dosiz: LE entry_obj=\d+ out of range$", re.MULTILINE),
    re.compile(r"^dosiz: LE descriptor install failed$", re.MULTILINE),
    re.compile(r"^dosiz: LE obj \d+: pm_alloc \d+ bytes failed$", re.MULTILINE),
    re.compile(r"^dosiz: LE: no LDT run of \d+ descriptors$", re.MULTILINE),
    re.compile(r"^dosiz: LE: no data object to host the synth stack$", re.MULTILINE),
    re.compile(r"^dosiz: bring-up (?:failed|threw): .*$", re.MULTILINE),
)


def find_loader_failure(stderr_text: str) -> str | None:
    """The first loader-failure line in stderr, verbatim, or None.

    Measured: ``dosiz NOSUCH.EXE`` prints ``dosiz: cannot find 'NOSUCH.EXE' on
    DOSIZ_PATH`` and exits 1 -- the exact rc a guest gets from ``INT 21h
    AH=4Ch AL=1``.
    """
    for pattern in _LOADER_FAILURE_RES:
        m = pattern.search(stderr_text)
        if m is not None:
            return m.group(0).rstrip("\r")
    return None


#: The go32-v2 stub's own failure, on the guest's *stdout*, with host rc 102.
#: Measured, verbatim: ``C:\PRIVATE\TMP\…\DJ_PRINTF.EXE: can't open``.
_ARGV0_TRAP_RE = re.compile(r"^(C:\\.*): can't open\s*$", re.MULTILINE)

#: The host status a go32 stub exits with when it cannot reopen ``argv[0]``.
#: Measured on ``DJ_PRINTF.exe`` given an absolute path.
ARGV0_TRAP_RC = 102


def detect_argv0_trap(stdout_text: str, rc: int | None) -> str | None:
    """The ``C:\\…: can't open`` path, when the argv[0] trap fired.

    :meth:`DosizBackend.run` makes this unreachable by construction; it is
    checked anyway because the failure is otherwise a silent rc 102 with an
    empty-looking result, and because a future ``dosiz`` change to
    ``build_env`` would land here rather than in a bug report.
    """
    if rc != ARGV0_TRAP_RC:
        return None
    m = _ARGV0_TRAP_RE.search(stdout_text)
    return m.group(1) if m is not None else None


def classify_exit(
    *,
    rc: int | None,
    timed_out: bool,
    stderr_text: str,
    stdout_text: str = "",
) -> tuple[int | None, ExitCodeMeaning]:
    """Turn a host exit status into ``(exit_code, exit_code_meaning)``.

    SPEC.md 6.4: "rc 1 is ambiguous between 'the guest exited 1' and 'dosiz
    failed to load the program', so ``exit_code_meaning`` disambiguates it from
    stderr". Precedence, most specific first:

    ``timeout``          the sandbox deadline fired. There is no guest exit
                         status at all, so ``exit_code`` is None -- SPEC.md
                         5.5, nothing in the family has an internal timeout.
    ``pm_fault``         an unhandled protected-mode fault. rc is 1 from
                         ``bridge.cc:1988``, not from the guest.
    ``loader_failure``   ``dosiz`` printed one of :data:`_LOADER_FAILURE_RES`,
                         or the go32 stub could not reopen ``argv[0]``.
    ``guest_exit``       ``INT 21h AH=4Ch``'s AL reached the host
                         (``bridge.cc:5438``). Measured: ``DJ_PRINTF.exe`` -> 7.
    """
    if timed_out:
        return None, ExitCodeMeaning.TIMEOUT
    if parse_pm_fault(stderr_text) is not None:
        return rc, ExitCodeMeaning.PM_FAULT
    if find_loader_failure(stderr_text) is not None:
        return rc, ExitCodeMeaning.LOADER_FAILURE
    if detect_argv0_trap(stdout_text, rc) is not None:
        return rc, ExitCodeMeaning.LOADER_FAILURE
    return rc, ExitCodeMeaning.GUEST_EXIT


# ---------------------------------------------------------------------------
# cfg synthesis
# ---------------------------------------------------------------------------

_CFG_MODE = {
    DefaultMode.BINARY: "binary",
    DefaultMode.TEXT: "text",
    DefaultMode.AUTO: "auto",
}


def synthesize_cfg(
    *,
    program: str,
    default_mode: DefaultMode,
    eol_convert: bool,
    printer_path: str | None = None,
    aux_output_path: str | None = None,
    machine: str | None = None,
    cputype: str | None = None,
    memsize_mb: int | None = None,
) -> str:
    """Build the cfg text this adapter hands ``dosiz`` as its first argument.

    Keys are exactly the ones ``dosiz/src/config.cc:88-100`` recognises.
    ``program`` must stay *relative*: ``dosiz.cc:91-95`` takes the cfg branch
    without calling ``resolve_program_path``, so it is opened relative to the
    process cwd and reaches ``build_env`` unchanged -- which is what keeps
    ``argv[0]`` short enough for the go32 stub to reopen.

    ``default_mode`` and ``eol_convert`` have no CLI flag (``dosiz.cc:64-77``
    accepts only ``--window``, ``--verbose``, ``--machine=``, ``--cpu=`` and
    ``--memsize=``), which is the reason a cfg exists at all rather than a
    longer argv.
    """
    if "/" in program or "\\" in program:
        raise ValueError(
            f"program must be a bare name relative to the guest directory, "
            f"got {program!r}; an absolute or nested path makes dosiz build "
            f"argv[0] as \"{dos_argv0(program)}\" and the go32 stub cannot "
            f"reopen it (dosiz/src/bridge.cc:7109-7122)"
        )
    lines = [
        "# Synthesized by 80mcp. Passed to dosiz as an explicit cfg so that no",
        "# sidecar <stem>.cfg beside the program is auto-loaded",
        "# (dosiz/src/dosiz.cc:91-95 vs :104-109).",
        f"program = {program}",
        f"default_mode = {_CFG_MODE[default_mode]}",
        f"eol_convert = {'true' if eol_convert else 'false'}",
        "headless = true",
    ]
    if printer_path:
        # SPEC.md 5.5: printer and aux go to files in the sandbox, never to
        # empty and never to /dev/null. dosiz creates them lazily on the first
        # byte, so an untouched device leaves no file rather than an empty one.
        lines.append(f"printer = {printer_path}")
    if aux_output_path:
        lines.append(f"aux_output = {aux_output_path}")
    if machine:
        lines.append(f"machine = {machine}")
    if cputype:
        lines.append(f"cputype = {cputype}")
    if memsize_mb:
        lines.append(f"memsize = {memsize_mb}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------

class DosizBackend(Backend):
    """One-shot adapter over the ``dosiz`` binary. SPEC.md 5.3 shape 3.

    Construction never fails and never touches the filesystem beyond a
    ``which``: an absent backend is a profile reporting ``ready:false`` with a
    ``blocked_by`` string, so :meth:`caps` answers with ``binary_path=None``
    and only :meth:`run` refuses.
    """

    name = BACKEND_NAME
    shape = AdapterShape.ONE_SHOT

    def __init__(self, binary: str | os.PathLike[str] | None = None) -> None:
        self._explicit = str(binary) if binary is not None else None
        self._binary: str | None = find_binary(binary)
        self._version: str | None = None
        self._version_probed = False
        #: One-shot ``send`` semantics: bytes queued for the next ``run``.
        self._pending_stdin = bytearray()
        #: One-shot ``recv`` semantics: the last run's captured guest output.
        self._last_stdout = b""
        self._last_exec: ExecResult | None = None
        self._ever_ran = False

    # -- discovery ---------------------------------------------------------

    @property
    def binary_path(self) -> str | None:
        return self._binary

    def available(self) -> bool:
        return self._binary is not None

    def blocked_by(self) -> list[str]:
        """``profiles[].blocked_by`` entries, empty when the backend is usable.

        Actionable rather than descriptive: the string names the two places
        that were searched.
        """
        if self._binary is not None:
            return []
        if self._explicit is not None:
            return [
                f"dosiz binary not found or not executable at the configured "
                f"path {self._explicit!r}"
            ]
        return [
            f"dosiz binary not found: set ${BINARY_ENV_VAR} to it or put "
            f"'dosiz' on PATH (build it with "
            f"'cmake -S src -B build && cmake --build build', which needs a "
            f"qxDOS checkout beside the dosiz one for the emu88 core)"
        ]

    def version(self) -> str | None:
        if not self._version_probed:
            self._version_probed = True
            if self._binary is not None:
                self._version = probe_version(self._binary)
        return self._version

    def _require_binary(self) -> str:
        if self._binary is None:
            raise BackendFailure(
                self.name,
                self.blocked_by()[0],
                searched=[BINARY_ENV_VAR, "PATH"],
                hint="x80_profiles reports this as ready:false with the same string",
            )
        return self._binary

    # -- caps --------------------------------------------------------------

    def caps(self) -> Caps:
        return Caps(
            backend=self.name,
            shape=self.shape,
            family=Family.X86,
            tier=Tier.HOSTED,
            # Phase 1 serves the seven required ops and nothing else: a
            # one-shot has no live machine between calls, so there is nothing
            # for regs/step/bp_* to read (SPEC.md 5.3, shape 3).
            ops=frozenset({
                Op.CAPS, Op.BOOT, Op.RUN, Op.SEND, Op.RECV, Op.IDLE, Op.STOP,
            }),
            # --cpu= is forwarded verbatim to dosbox's cputype= and nothing
            # validates it: measured, 8088/186/286/386/486_slow/pentium_slow
            # were all accepted and DJ_PRINTF.exe ran unchanged under each. The
            # value is a hint, not a constraint.
            cpus=("8088", "186", "286", "386"),
            consoles=1,
            # Measured: DJ_PRINTF.exe -> rc 7, from INT 21h AH=4Ch AL
            # (bridge.cc:5438). dosiz is the one backend in the family where
            # this is true (SPEC.md 5.4 Invariant 4).
            has_exit_code=True,
            # dos_machine::is_waiting_for_key() is a real signal, but a
            # one-shot adapter has no control channel to ask it: the process is
            # spawned, fed and reaped inside a single run(). Claiming an idle
            # signal we cannot read would be exactly the heuristic SPEC.md 1.4
            # forbids.
            has_idle_signal=False,
            version=self.version(),
            binary_path=self._binary,
            divergences=(
                "argv[0] is built as 'C:' plus the uppercased backslashed host "
                "program path (bridge.cc:7109-7122) and DJGPP's go32 stub "
                "reopens it, so an absolute program path fails with "
                "\"C:\\...: can't open\" and rc 102; the adapter always chdirs "
                "into the sandbox and passes a bare relative name",
                "an unimplemented INT 21h AH returns invalid-function and the "
                "program continues (bridge.cc:6141-6150), so a run can exit 0 "
                "with a degraded result -- assert on "
                "diagnostics.unimplemented_int21, not on the exit code",
                "only COMSPEC, PATH and the host variables "
                + ", ".join(sorted(GUEST_ENV_PASSTHROUGH))
                + " reach the guest's DOS environment (bridge.cc:7085-7096); "
                "any other env key is set on the host process only",
                "no internal timeout and no instruction limit: a 2-byte EB FE "
                "spin loop runs until SIGKILL, so every run is bounded by the "
                "supervisor's deadline",
                "two stderr lines print on every run (the slirp and Crynwr "
                "pktdrv notices) and are removed by exact match into "
                "diagnostics.stderr_filtered",
            ),
        )

    # -- boot: a no-op, but not an unconditional one -----------------------

    def boot(self, spec: BootSpec) -> BootOutcome:
        """No-op. A one-shot has nothing to bring up (SPEC.md 5.3, shape 3).

        It still checks the profile, because ``x80_dos_run``'s enum also
        carries ``freedos`` -- a real FreeDOS kernel on ``emu88``, which is a
        different backend and which SPEC.md 3.7 keeps ``available:false`` until
        one job proves it.
        """
        if spec.profile and spec.profile not in SERVED_PROFILES:
            raise self.unsupported(
                Op.BOOT,
                reason=(
                    f"profile {spec.profile!r} is not served by dosiz; dosiz "
                    f"traps INT 21h to the host filesystem and boots nothing. "
                    f"freedos boots a real kernel on emu88, needs a disk image, "
                    f"and has no exit code (SPEC.md 3.7, 6.4)"
                ),
                alternative_profile=SERVED_PROFILES[0],
            )
        return BootOutcome(ok=True, stopped=None)

    # -- run ---------------------------------------------------------------

    def run(self, req: RunRequest) -> RunOutcome:
        """One deadlined exec: stage, synthesize the cfg, chdir, capture.

        The only structural requirement is the one SPEC.md 5.5 states as
        mandatory: cwd is the sandbox guest directory and the program is a bare
        relative name.
        """
        binary = self._require_binary()
        sandbox = req.sandbox
        warnings: list[str] = []
        config_warnings: list[str] = []
        self._refuse_phase2_fields(req)

        program_name = self._stage_program(req, warnings)
        for f in req.files_in:
            sandbox.stage(f)

        cfg_text = synthesize_cfg(
            program=program_name,
            default_mode=req.default_mode,
            eol_convert=req.eol_convert,
            printer_path=str(sandbox.lst_path),
            aux_output_path=str(sandbox.pun_path),
            cputype=req.cpu,
        )
        cfg_path = sandbox.write_text(CFG_NAME, cfg_text)

        # Re-snapshot now that staging is done, so created_since_snapshot()
        # means "what the program wrote" rather than "what the program wrote
        # plus the copy of itself and its inputs". Only the adapter knows when
        # staging finished, so only the adapter can draw the line.
        sandbox.snapshot()

        env = self._guest_env(req.env, warnings)

        stdin = bytes(self._pending_stdin) + req.stdin
        self._pending_stdin.clear()

        argv = [binary, str(cfg_path), *req.args]
        result = sandbox.exec(
            argv,
            cwd=sandbox.guest_dir,
            env=env,
            stdin=stdin,
            timeout_ms=req.timeout_ms,
        )
        self._last_exec = result
        self._last_stdout = result.stdout
        self._ever_ran = True

        return self._outcome(req, result, warnings, config_warnings)

    def _refuse_phase2_fields(self, req: RunRequest) -> None:
        """Refuse a stop condition a one-shot cannot enforce.

        ``until`` and ``max_steps`` belong to ``x80_run`` (SPEC.md 6.5, phase
        2), which needs a live machine on a pty. Running to completion and
        reporting success would be the silent wrong answer this design keeps
        arguing against, so it is an error instead.
        """
        for field, tool in (("until", "x80_run"), ("max_steps", "x80_run")):
            if getattr(req, field) is not None:
                raise self.unsupported(
                    Op.RUN,
                    reason=(
                        f"{field!r} needs a machine that can be stopped part "
                        f"way through; dosiz runs as a one-shot (SPEC.md 5.3, "
                        f"shape 3) and would run to completion and report "
                        f"success. {tool} on a phase-2 profile is the verb "
                        f"that honours it"
                    ),
                )
        if req.console not in (None, 0):
            raise self.unsupported(
                Op.RUN,
                reason=(
                    f"console {req.console} does not exist: dosiz has one "
                    f"console. Multiple consoles are mpm2 only (SPEC.md 3.4)"
                ),
                alternative_profile="mpm2",
            )

    def _stage_program(self, req: RunRequest, warnings: list[str]) -> str:
        """Copy the program into the guest directory and return its bare name.

        This is the argv[0] fix (SPEC.md 5.5, "mandatory, not stylistic"). The
        basename is kept exactly as given: ``dos_to_host``
        (``bridge.cc:533-598``) falls back to a case-insensitive directory walk
        when the exact-case name misses, specifically so that the uppercased
        ``argv[0]`` finds a lowercase host file, so ``DJ_PRINTF.exe`` works
        unchanged on a case-sensitive filesystem.
        """
        source = Path(req.program).expanduser()
        if not source.is_file():
            raise BackendFailure(
                self.name,
                f"program not found on the host: {req.program!r}",
                program=req.program,
                hint=(
                    "x80_dos_run's `program` is a HOST path to the .EXE/.COM; "
                    "the server copies it into the sandbox and runs it there"
                ),
            )
        name = source.name
        stage_program = getattr(req.sandbox, "stage_program", None)
        if callable(stage_program):
            # eightymcp.sandbox.Sandbox.stage_program exists for exactly this
            # requirement -- its docstring cites the same rc 102 -- and it also
            # refuses a guest_name that would escape the guest directory. Fall
            # back to a plain copy only for a sandbox that implements no more
            # than SandboxProtocol.
            if " " in name:
                warnings.append(_SPACE_IN_PROGRAM_NAME.format(name=name))
            stage_program(source, name)
            return name
        if " " in name:
            warnings.append(_SPACE_IN_PROGRAM_NAME.format(name=name))
        target = Path(req.sandbox.guest_dir) / name
        if target.exists() and not target.samefile(source):
            warnings.append(
                f"a staged file already occupied the guest name {name!r}; it "
                f"was replaced by the program"
            )
        shutil.copy2(source, target)
        return name

    def _guest_env(
        self, requested: Mapping[str, str], warnings: list[str]
    ) -> dict[str, str]:
        """Filter the caller's ``env`` and say what will not reach the guest.

        ``sandbox.exec`` adds this to its hermetic base, so anything here is a
        *host* process variable. Only :data:`GUEST_ENV_PASSTHROUGH` is copied
        into the DOS environment block (``bridge.cc:7089-7096``); the rest is
        invisible to the program, which is a silent no-op worth naming.

        :class:`eightymcp.sandbox.Sandbox` already builds its base environment
        from an allowlist that excludes every ``DOSIZ_*`` key, so the drops
        below are a second lock rather than the only one -- they hold for any
        sandbox that implements no more than
        :class:`~eightymcp.mbp.SandboxProtocol`, and they turn a silent
        no-effect into a named warning either way.
        """
        env: dict[str, str] = {}
        invisible: list[str] = []
        for key, value in requested.items():
            if key in _HOST_ONLY_ENV:
                warnings.append(
                    f"env {key!r} was dropped: dosiz searches it for programs "
                    f"(config.cc:218-229) and appends it to the guest's PATH "
                    f"(bridge.cc:7081-7085), either of which would reach "
                    f"outside the sandbox"
                )
                continue
            if key.startswith("DOSIZ_"):
                warnings.append(
                    f"env {key!r} was dropped: dosiz's debug switches are read "
                    f"with a bare getenv() != nullptr "
                    f"(debug_settings.cpp:22-24), so even an empty value "
                    f"changes emulator behaviour"
                )
                continue
            env[key] = value
            if key not in GUEST_ENV_PASSTHROUGH:
                invisible.append(key)
        if invisible:
            warnings.append(
                "env " + ", ".join(repr(k) for k in sorted(invisible))
                + " is set on the host dosiz process but does not reach the "
                "guest's DOS environment; only COMSPEC, PATH and "
                + ", ".join(sorted(GUEST_ENV_PASSTHROUGH))
                + " are copied in (bridge.cc:7085-7096)"
            )
        return env

    def _outcome(
        self,
        req: RunRequest,
        result: ExecResult,
        warnings: list[str],
        config_warnings: list[str],
    ) -> RunOutcome:
        stdout_text = decode_guest(result.stdout)
        raw_stderr = decode_guest(result.stderr)
        kept_stderr, filtered = filter_noise(raw_stderr)

        exit_code, meaning = classify_exit(
            rc=result.rc,
            timed_out=result.timed_out,
            stderr_text=kept_stderr,
            stdout_text=stdout_text,
        )
        pm_fault = parse_pm_fault(kept_stderr) if meaning is ExitCodeMeaning.PM_FAULT else None

        if meaning is ExitCodeMeaning.LOADER_FAILURE:
            trapped = detect_argv0_trap(stdout_text, result.rc)
            if trapped is not None:
                warnings.append(
                    f"the argv[0] trap fired even though the program was run "
                    f"as a relative name from the sandbox: the go32 stub could "
                    f"not reopen {trapped!r} (dosiz/src/bridge.cc:7109-7122)"
                )
            else:
                warnings.append(
                    f"dosiz did not load the program: "
                    f"{find_loader_failure(kept_stderr)}"
                )

        unimplemented = parse_unimplemented_int21(kept_stderr)
        if unimplemented:
            warnings.append(
                "dosiz returned invalid-function for "
                + ", ".join(f"INT 21h AH={u.ah:02X}h" for u in unimplemented)
                + " and let the program continue, so a zero exit code does not "
                "mean the run was complete (bridge.cc:6141-6150)"
            )

        return RunOutcome(
            exit_reason=self._exit_reason(result, meaning),
            stdout=result.stdout,
            # The UNFILTERED stream, per RunOutcome's contract; what was removed
            # is listed in stderr_filtered.
            stderr=result.stderr,
            wall_ms=result.wall_ms,
            exit_code=exit_code,
            exit_code_meaning=meaning,
            list_output_path=self._device_path(req.sandbox.lst_path),
            punch_output_path=self._device_path(req.sandbox.pun_path),
            unimplemented_int21=unimplemented,
            pm_fault=pm_fault,
            stderr_filtered=filtered,
            config_warnings=config_warnings,
            warnings=warnings,
            timed_out=result.timed_out,
            raw=result,
        )

    @staticmethod
    def _device_path(path: Path) -> Path | None:
        """LST:/PUN: are created lazily by dosiz on the first byte, so an
        untouched device is an absent file rather than an empty one."""
        return path if Path(path).exists() else None

    @staticmethod
    def _exit_reason(result: ExecResult, meaning: ExitCodeMeaning) -> ExitReason:
        """The internal ``exit_reason``. ``x80_dos_run`` does not carry it --
        ``DosRunResult`` has ``exit_code``/``exit_code_meaning`` instead -- but
        ``RunOutcome`` is one shape for both verbs, so it is filled honestly.

        ``BDOS_0`` is CP/M's "System Reset", BDOS function 0. DOS's ``INT 21h
        AH=4Ch`` is the same call under a different number and it is what
        ``bridge.cc:5438`` reads AL from, so a normal DOS termination maps
        there rather than to an invented member: SPEC.md 6.4 fixes the enum at
        exactly nine values.
        """
        if result.timed_out:
            return ExitReason.TIMEOUT
        if meaning in (ExitCodeMeaning.PM_FAULT, ExitCodeMeaning.LOADER_FAILURE):
            return ExitReason.EMULATOR_ERROR
        if result.signal == 2:  # SIGINT
            return ExitReason.CTRL_C
        if result.signal is not None:
            return ExitReason.EMULATOR_ERROR
        return ExitReason.BDOS_0

    # -- the remaining one-shot ops ----------------------------------------

    def send(self, req: SendRequest) -> SendOutcome:
        """Queue bytes for the next :meth:`run`.

        A one-shot has no live machine to type at, so there is nothing to pace
        against and ``paced_by`` says ``"not_waited"`` rather than claiming an
        idle signal (SPEC.md 5.4 Invariant 2 is about a *running* guest; the
        honest answer here is that no wait happened).
        """
        self._pending_stdin.extend(req.data)
        return SendOutcome(bytes_sent=len(req.data), waited_ms=0, paced_by="not_waited")

    def recv(self, req: RecvRequest) -> RecvOutcome:
        """Return the last run's captured guest output."""
        data = self._last_stdout[: req.max_bytes]
        return RecvOutcome(
            data=data,
            console=req.console,
            more=len(self._last_stdout) > req.max_bytes,
        )

    def idle(self, req: IdleRequest) -> IdleOutcome:
        """Always idle: the process is spawned, fed and reaped inside one
        :meth:`run`, so between calls there is nothing running."""
        return IdleOutcome(
            idle=True,
            source="process_exited" if self._ever_ran else "not_started",
        )

    def stop(self, req: StopRequest) -> StopOutcome:
        """A no-op. ``sandbox.exec`` already killed the process group on the
        way out of :meth:`run`, whether it exited or hit the deadline."""
        last = self._last_exec
        return StopOutcome(
            stopped=True,
            exit_code=last.rc if last is not None else None,
            signal=last.signal if last is not None else None,
            stderr_tail=last.stderr[-65536:] if last is not None else b"",
        )
