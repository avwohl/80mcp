"""The ``cpmemu`` one-shot adapter -- MBP shape 3. SPEC.md 5.3.

``boot`` is a no-op; ``run`` is one exec in the sandbox with this stdin,
captured, under the supervisor's deadline; there is no ``regs`` and no
``step``. Nothing upstream changes to make this work.

Everything this module knows about cpmemu was measured against
``/Users/wohl/src/cpmemu/src/cpmemu`` (built from cpmemu ed778f7,
``src/cpmemu.cc``, 3429 lines, no header -- which is also why there is no
debugger to adapt: ``CPMEmulator`` is declared inside the .cc and nothing
outside the process can reach it). Line citations below are to that file.

The four things this adapter exists to get right:

1. **default_mode is binary and only a synthesized .cfg can say so.** There is
   no CLI flag: ``default_mode`` is a config directive (``cpmemu.cc:1058``).
   SPEC.md 5.4 Invariant 3.
2. **printer and aux_output are real files in the sandbox.** Never empty, never
   ``/dev/null``. SPEC.md 5.5 and Appendix B item 2.
3. **exit_reason comes from a stderr sentinel, and there is no exit code.**
   cpmemu ``exit(0)``s on every guest-visible path (``program_exit`` at
   ``cpmemu.cc:133-137``, the ^C hatch at ``:172-176``, the end-of-input
   give-up at ``:209-213``) including the runaway watchdog (``:3415-3428``
   prints ``Reached instruction limit`` and then ``return 0``). SPEC.md 5.4
   Invariant 4: :class:`~eightymcp.types.RunResult` has no ``exit_code`` field
   and :attr:`RunOutcome.exit_code` stays ``None`` here, asserted below.
4. **Host paths in the command tail are absolute or ``./``-prefixed.**
   ``filename_to_fcb`` (``cpmemu.cc:1289``) strips the directory only when the
   argument starts with ``/`` or ``./`` (``:1298-1303``), and the command-tail
   builder tests the same two prefixes (``:649-658``). A bare ``sub/M9.ARC``
   therefore builds an FCB from ``SUB/M9`` -- ``/`` is not a valid CP/M
   character, so it is replaced with ``_`` -- and the guest's open fails on a
   name the caller never wrote.

Measured on this machine, 2026-09-04, with ``80un.com`` (21336 bytes) on
``tests/samples/arc/method9.arc`` (54842 bytes), a 23-member ARC:

* ``default_mode=binary``, ``eol_convert=false`` -- ``23 file(s) extracted``,
  ``Program exit via JMP 0``, ``b5-time.inf`` 1664 bytes.
* ``default_mode=auto``, ``eol_convert=true`` -- ``1 file(s) extracted``,
  ``Error``, ``b5-time.inf`` 1437 bytes, and process rc 0.
* ``default_mode=auto``, ``eol_convert=false`` -- ``23 file(s) extracted``,
  1664 bytes.

The third line is a divergence from SPEC.md 9's phase-1 acceptance list, which
says the fixture under ``default_mode:"auto"`` gives ``1 file(s) extracted``.
Both are true; the spec's auto.cfg (``evidence/t3/auto.cfg``) omits
``eol_convert``, and cpmemu's default for it is **true**
(``cpmemu.cc:398``: ``default_mode(MODE_AUTO), default_eol_convert(true)``).
The corruption is the pair, and the mechanism is exact:

* BDOS 22 Make copies the default mode into the open file *unresolved* --
  ``of.mode = default_mode`` (``cpmemu.cc:2115``), so a created file keeps
  MODE_AUTO. The auto-to-binary resolution at ``:852``, ``:868`` and ``:876``
  is on the *find* path, and Make has nothing to find (``:2096-2098``).
* ``write_with_conversion`` (``:952``) writes raw only when
  ``of.mode == MODE_BINARY || !of.eol_convert``. MODE_AUTO is neither TEXT nor
  BINARY, so with eol_convert on it falls into the text branch, which stops at
  the first 0x1A (``:964: if (ch == CPM_EOF)``) and drops CR before LF.
  ``B5-TIME.INF`` is truncated at its first 0x1A byte: 1437 of 1664.

So this adapter writes ``eol_convert`` explicitly on every run rather than
leaving it to cpmemu's default, and warns when ``default_mode`` is ``auto``.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

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
    SandboxProtocol,
    SendOutcome,
    SendRequest,
    StopOutcome,
    StopRequest,
    UnsupportedOp,
)
from ..types import (
    AdapterShape,
    Caps,
    Cpu,
    DefaultMode,
    Escalation,
    ExecResult,
    ExitReason,
    Family,
    Tier,
    ToolError,
    ToolExecutionError,
)

__all__ = [
    "BACKEND_NAME",
    "PROFILE",
    "BINARY_ENV_VAR",
    "DIVERGENCES",
    "SENTINELS",
    "StderrFacts",
    "find_binary",
    "binary_version",
    "blocked_by",
    "synthesize_cfg",
    "tail_argument",
    "build_tail",
    "guest_name_for",
    "parse_stderr",
    "CpmemuBackend",
]

#: As it appears in ``profiles[].backend`` and in an ``isError`` body.
BACKEND_NAME = "cpmemu"

#: The one profile this backend serves. SPEC.md 3.2: cpm-hosted is cpmemu.
PROFILE = "cpm-hosted"

#: Config override for the binary's location. Backends are never vendored;
#: they are found at runtime by config or PATH.
BINARY_ENV_VAR = "EIGHTYMCP_CPMEMU"

#: Fallback locations, tried in order after ``$EIGHTYMCP_CPMEMU`` and PATH.
#: The sibling-checkout form is the family's own build convention -- cpmemu's
#: makefile looks for a sibling ``../../cpmemu/src`` the same way (cpmemu
#: README, "Building" ).
_SIBLING_CANDIDATES: tuple[str, ...] = (
    "../cpmemu/src/cpmemu",
    "../../cpmemu/src/cpmemu",
)

#: SPEC.md 6.4: "fidelity.divergences is carried inline per profile, not buried
#: in docs. cpm-hosted carries, at minimum:" -- these two, verbatim from the
#: spec, each with the line that produces it.
DIVERGENCES: tuple[str, ...] = (
    "setup_command_line() never writes the FCB at 0x5C, so a program testing "
    "fcb(1)=' ' for its usage banner takes the wrong branch. cpmemu.cc:575 "
    "zeroes the FCB and cpmemu.cc:637 fills it only when an argument was "
    "given, where real CP/M's CCP blank-fills it either way.",
    "created filenames are lowercased on the host: TEST.TXT becomes test.txt. "
    "cpmemu.cc:2089-2092, on the BDOS 22 Make path.",
    "The LST:, PUN: and RDR: devices are seven-bit in both directions, so a "
    "file moved through list_output or punch_output is not byte-exact for "
    "eight-bit data (cpmemu README, 'Environment Variables').",
)

# ---------------------------------------------------------------------------
# stderr sentinels
# ---------------------------------------------------------------------------
#
# SPEC.md 6.4: "exit_reason is parsed from the backend's stable stderr sentinel
# lines". Every one of these was produced on this machine and matched against
# the line that prints it.

#: ``(pattern, reason)``, matched against whole stderr lines.
SENTINELS: tuple[tuple[re.Pattern[str], ExitReason], ...] = (
    # cpmemu.cc:1375, program_exit("Program exit via JMP 0")
    (re.compile(r"^Program exit via JMP 0$"), ExitReason.JMP_0),
    # cpmemu.cc:1410, program_exit("System reset") -- BDOS function 0
    (re.compile(r"^System reset$"), ExitReason.BDOS_0),
    # cpmemu.cc:2745, program_exit("BIOS WBOOT called - exiting")
    (re.compile(r"^BIOS WBOOT called - exiting$"), ExitReason.WBOOT),
    # cpmemu.cc:3415. The watchdog: 9e9 instructions, then `return 0`.
    (re.compile(r"^Reached instruction limit$"), ExitReason.INSTRUCTION_LIMIT),
    # cpmemu.cc:209. The count is CONSOLE_EOF_LIMIT (1024, cpmemu.cc:194) but
    # it is printed as %d, so it is matched as a number rather than as 1024.
    (
        re.compile(r"^\[Exiting: (?P<count>\d+) console reads past end of input\]$"),
        ExitReason.EOF_GIVEUP,
    ),
    # cpmemu.cc:172. Five ^C inside a two-second window.
    (
        re.compile(r"^\[Exiting: (?P<count>\d+) consecutive \^C received\]$"),
        ExitReason.CTRL_C,
    ),
)

#: cpmemu.cc:1580, ``fprintf(stderr, "Unimplemented BDOS function %d\n", func)``
#: -- printed once per *call*, not once per distinct function (measured: two
#: calls to BDOS 100 produced two lines), so the adapter deduplicates.
_UNIMPLEMENTED_BDOS = re.compile(r"^Unimplemented BDOS function (?P<func>\d+)$")

#: cpmemu.cc:1177 / :1188 / :1199. Appendix B item 2: an empty printer path
#: emits this on every run, and the LIST bytes then go to *stdout* as
#: "[PRINTER] c" (measured), which corrupts the console transcript an agent
#: reads. Routing to a sandbox file is what stops both.
_DEVICE_WARNING = re.compile(
    r"^Warning: Cannot open (?:printer|aux input|aux output) file '.*': "
)

#: cpmemu.cc:1118 and :1134. A mistyped directive silently becomes a file
#: mapping; these two lines are cpmemu telling us our synthesized cfg is wrong.
_CONFIG_LINE_WARNING = re.compile(r"^Config line \d+: ")

#: cpmemu.cc:997. Our cfg, not the caller's program: a supervisor bug.
_CONFIG_UNREADABLE = re.compile(r"^Cannot open config file: (?P<reason>.*)$")

#: cpmemu.cc:3343, then ``return 1``. The caller's .COM did not load.
_PROGRAM_UNREADABLE = re.compile(r"^Cannot open (?P<path>.+): (?P<reason>.+)$")

#: cpmemu.cc:3174. cpmemu reporting that it ate one of the guest's arguments
#: because it spelled one of its own options. Parsed rather than predicted, so
#: the adapter cannot drift from cpmemu's option list.
_OPTION_EATEN = re.compile(
    r"^Note: '(?P<arg>.*)' taken as an emulator option, not passed to the program$"
)

#: cpmemu.cc:1335 and :1356 (filename_to_fcb, and the extension field). A character the FCB cannot hold.
_INVALID_CPM_CHAR = re.compile(r"^Warning: invalid CP/M character ")

#: The two lines cpmemu prints on every successful start. Not filtered --
#: SPEC.md 7.1 shows them in the reported stderr verbatim -- but recognised so
#: that "no sentinel and nothing else either" can be told apart from them.
_BANNER = re.compile(r"^(?:CPU mode: (?:Z80|8080)|Loaded \d+ bytes from .*)$")

#: Environment variables cpmemu reads *after* the config file, so they replace
#: the directives this adapter synthesized (cpmemu README, "Environment
#: Variables"). Letting a caller set these would silently undo Appendix B
#: item 2, so they are dropped with a warning rather than honoured.
_ENV_OVERRIDES_CONFIG: tuple[str, ...] = ("CPM_PRINTER", "CPM_AUX_OUT", "CPM_AUX_IN")


# ---------------------------------------------------------------------------
# Locating the binary
# ---------------------------------------------------------------------------

def find_binary(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Where cpmemu is, or ``None``.

    Order: an explicit path, ``$EIGHTYMCP_CPMEMU``, ``PATH``, then a sibling
    checkout relative to this package. Never raises: an absent backend is a
    profile reporting ``ready:false`` with a ``blocked_by`` string, not a
    crash.
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if _is_runnable(p) else None

    env = os.environ.get(BINARY_ENV_VAR)
    if env:
        p = Path(env).expanduser()
        if _is_runnable(p):
            return p

    which = shutil.which(BACKEND_NAME)
    if which:
        return Path(which)

    # <repo>/src/eightymcp/backends/cpmemu.py -> <repo>
    repo_root = Path(__file__).resolve().parents[3]
    for rel in _SIBLING_CANDIDATES:
        p = (repo_root / rel).resolve()
        if _is_runnable(p):
            return p
    return None


def _is_runnable(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


def binary_version(binary: Path | None = None) -> str | None:
    """Always ``None``, and the reason is measured, not assumed.

    cpmemu has no ``--version``: the option parser (``cpmemu.cc:3116-3147``)
    does not define one, so ``--version`` falls through to
    ``try_option``'s ``return false`` and is taken as the program name. The run
    prints ``CPU mode: Z80`` then ``Cannot open --version: No such file or
    directory``. There is no version string in the binary either. Whatever
    reports a version for this backend has to get it from the packaging, not
    from the program.
    """
    return None


def blocked_by(binary: Path | None) -> list[str]:
    """``Profile.blocked_by`` entries for this backend, for profiles.py."""
    if binary is not None:
        return []
    return [
        f"{BACKEND_NAME} was not found: ${BINARY_ENV_VAR} is unset or does not "
        f"name an executable, {BACKEND_NAME!r} is not on PATH, and no sibling "
        f"checkout was found at any of {', '.join(_SIBLING_CANDIDATES)}. "
        f"Build it from the cpmemu repo (make) or set ${BINARY_ENV_VAR}."
    ]


# ---------------------------------------------------------------------------
# The synthesized .cfg
# ---------------------------------------------------------------------------

def synthesize_cfg(
    *,
    program: str | os.PathLike[str],
    guest_dir: str | os.PathLike[str],
    lst_path: str | os.PathLike[str],
    pun_path: str | os.PathLike[str],
    default_mode: DefaultMode = DefaultMode.BINARY,
    eol_convert: bool = False,
) -> str:
    """The cfg text, in SPEC.md 7.1's order and with its directives.

    ::

        program = /Users/wohl/src/80un/80un.com
        cd = <sandbox>/guest
        default_mode = binary
        eol_convert = false
        printer = <sandbox>/.lst
        aux_output = <sandbox>/.pun

    Order is not cosmetic. ``cd`` takes effect at the line it appears on, so a
    relative ``printer`` above it would open in the old directory; ``program``
    is resolved only after the whole file is read (cpmemu README,
    "Configuration Files"). Every path written here is absolute, which makes
    both hazards unreachable, and the order still matches the spec's so the two
    can be diffed.

    ``eol_convert`` is always written. Leaving it out takes cpmemu's default,
    which is ``true`` (``cpmemu.cc:398``) and is half of the pair that
    truncated 22 of 23 ARC members.
    """
    lines = [
        f"program = {_cfg_value('program', program)}",
        f"cd = {_cfg_value('cd', guest_dir)}",
        f"default_mode = {DefaultMode(default_mode).value}",
        f"eol_convert = {'true' if eol_convert else 'false'}",
        f"printer = {_cfg_value('printer', lst_path)}",
        f"aux_output = {_cfg_value('aux_output', pun_path)}",
    ]
    return "\n".join(lines) + "\n"


def _cfg_value(directive: str, value: str | os.PathLike[str]) -> str:
    """One directive value, checked for the two things the parser would eat.

    ``$VAR`` and ``${VAR}`` are expanded in the value of every directive
    (cpmemu README, "Configuration Files"), and there is no escape, so a
    sandbox path containing ``$`` would silently resolve somewhere else. A
    newline would split the line into a second directive, or into a file
    mapping. Both are refused here rather than discovered as a wrong answer.
    """
    text = os.fspath(value)
    if "\n" in text or "\r" in text:
        raise ToolExecutionError(
            ToolError.bad_argument(
                directive,
                "a cpmemu config value cannot contain a newline: the parser "
                "reads one directive per line",
                value=text,
            )
        )
    if "$" in text:
        raise ToolExecutionError(
            ToolError.bad_argument(
                directive,
                "cpmemu expands $VAR and ${VAR} in every directive value and "
                "has no escape, so this path would resolve somewhere else",
                value=text,
            )
        )
    return text


def tail_argument(arg: str, base: Path) -> str:
    """One command-tail argument, made safe for ``filename_to_fcb``.

    ``filename_to_fcb`` (``cpmemu.cc:1289``) strips the directory only when the
    argument begins with ``/`` or ``./`` (``:1298-1303``); the command-tail
    builder tests the same two prefixes (``:649-658``). So a relative host path
    like ``out/M9.ARC`` is not stripped: the FCB is built from ``OUT/M9``, the
    ``/`` fails ``is_valid_cpm_char`` and becomes ``_`` with a warning, and the
    guest opens a name nobody asked for (measured). Prefixing ``./`` puts it
    back on the stripping path, and the command tail then gets the uppercased
    basename, which is what the caller meant.

    The test for "is this a host path" is the filesystem, not the spelling:
    the argument is rewritten only when its first segment names a real
    directory under ``base`` -- the guest's working directory, which is where
    cpmemu resolves a relative path from. A CP/M tail carries ``/`` as a switch
    marker (``TEST,TEST.COM/N/E``, ``TEST/N/E``), and no switch letter is a
    directory, so a tail is left exactly as written. That is also cpmemu's own
    rule for anything it does not recognise: pass it to the program untouched.
    """
    if not arg or arg.startswith("/") or arg.startswith("./") or "/" not in arg:
        return arg
    head = arg.split("/", 1)[0]
    if not head:
        return arg
    try:
        if not (base / head).is_dir():
            return arg
    except OSError:
        return arg
    return "./" + arg


def build_tail(args: Sequence[str], base: Path) -> tuple[list[str], list[str]]:
    """The command tail, and a warning for every argument that was rewritten.

    A rewrite changes what the guest sees, so it is reported rather than done
    quietly.
    """
    tail: list[str] = []
    warnings: list[str] = []
    for arg in args:
        fixed = tail_argument(arg, base)
        tail.append(fixed)
        if fixed != arg:
            warnings.append(
                f"argument {arg!r} was passed as {fixed!r}: filename_to_fcb "
                f"(cpmemu.cc:1289) strips a directory only from a path starting "
                f"'/' or './', so the bare form would have built the FCB from "
                f"{arg.split('/', 1)[0].upper()}/... with '/' replaced by '_'."
            )
    return tail, warnings


def guest_name_for(host_name: str) -> str:
    """The CP/M name a host filename came from.

    cpmemu lowercases the whole 8.3 name on BDOS 22 Make
    (``cpmemu.cc:2089-2092``), so the inverse is an uppercase of the basename.
    Measured on method9.arc: ``b5-time.inf`` <- ``B5-TIME.INF``, ``-03mar86``
    <- ``-03MAR86``, ``ztim-s3.cpm`` <- ``ZTIM-S3.CPM``. SPEC.md 6.4 keeps
    ``host_name`` separate from ``guest_name`` for exactly this reason: "a
    caller writing case-sensitive assertions against host names will be
    wrong."
    """
    return os.path.basename(host_name).upper()


# ---------------------------------------------------------------------------
# stderr parsing
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StderrFacts:
    """What one run's stderr said. Nothing is inferred from the return code."""

    exit_reason: ExitReason | None = None
    unimplemented_bdos: list[int] = field(default_factory=list)
    config_warnings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: ``(path, reason)`` from cpmemu.cc:3343 -- the guest program never loaded.
    program_unreadable: tuple[str, str] | None = None
    #: cpmemu.cc:997 -- *our* cfg was unreadable. A supervisor bug.
    config_unreadable: str | None = None
    #: Guest arguments cpmemu took as its own options (cpmemu.cc:3174).
    options_eaten: list[str] = field(default_factory=list)


def parse_stderr(text: str) -> StderrFacts:
    """Read one cpmemu stderr stream.

    The last sentinel wins: cpmemu prints exactly one exit line and then
    ``exit(0)``, so a second would mean something has changed and the newer
    line is the one that ended the run.
    """
    facts = StderrFacts()
    seen_bdos: set[int] = set()

    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue

        matched = False
        for pattern, reason in SENTINELS:
            if pattern.match(line):
                facts.exit_reason = reason
                matched = True
                break
        if matched:
            continue

        m = _UNIMPLEMENTED_BDOS.match(line)
        if m:
            func = int(m.group("func"))
            if func not in seen_bdos:
                seen_bdos.add(func)
                facts.unimplemented_bdos.append(func)
            continue

        if _DEVICE_WARNING.match(line) or _CONFIG_LINE_WARNING.match(line):
            facts.config_warnings.append(line)
            continue

        m = _CONFIG_UNREADABLE.match(line)
        if m:
            facts.config_unreadable = m.group("reason")
            facts.config_warnings.append(line)
            continue

        m = _OPTION_EATEN.match(line)
        if m:
            facts.options_eaten.append(m.group("arg"))
            facts.warnings.append(line)
            continue

        if _INVALID_CPM_CHAR.match(line):
            facts.warnings.append(line)
            continue

        if _BANNER.match(line):
            continue

        m = _PROGRAM_UNREADABLE.match(line)
        if m:
            facts.program_unreadable = (m.group("path"), m.group("reason"))
            continue

    return facts


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------

class CpmemuBackend(Backend):
    """cpmemu as an MBP shape-3 adapter.

    Serves the seven required ops and nothing else. The debugger ops are not
    merely unimplemented here -- there is nothing to implement against:
    ``CPMEmulator`` is declared inside ``cpmemu.cc`` with no header, so no
    other translation unit can reach it. :meth:`Backend.unsupported` says so,
    with ``cpm22`` as the alternative profile.
    """

    name = BACKEND_NAME
    shape = AdapterShape.ONE_SHOT

    def __init__(self, binary_path: str | os.PathLike[str] | None = None):
        self._binary = find_binary(binary_path)
        #: Bytes queued by :meth:`send` and not yet consumed by a :meth:`run`.
        self._pending_stdin = bytearray()
        #: The last run's captured stdout, and how much of it recv has handed
        #: back.
        self._last_stdout = b""
        self._recv_cursor = 0
        self._last_exec: ExecResult | None = None
        self._ran = False

    # -- discovery --------------------------------------------------------

    @property
    def binary(self) -> Path | None:
        """The located binary, or None. Re-probed on each access is wrong --
        a run must not change binaries mid-session -- so this is what
        ``__init__`` found."""
        return self._binary

    @property
    def available(self) -> bool:
        return self._binary is not None

    def blocked_by(self) -> list[str]:
        return blocked_by(self._binary)

    def _require_binary(self) -> Path:
        if self._binary is None:
            raise ToolExecutionError(
                ToolError.backend_missing(
                    BACKEND_NAME,
                    blocked_by(None)[0],
                    hint=f"set ${BINARY_ENV_VAR} to the cpmemu executable",
                )
            )
        return self._binary

    # -- caps -------------------------------------------------------------

    def caps(self) -> Caps:
        """The seven required ops, and no optional ones.

        ``has_exit_code`` is False and that is Invariant 4, not an omission:
        cpmemu exits 0 on every path a guest can reach, including the run that
        extracted 1 of 23 files and printed ``Error``.

        ``has_idle_signal`` is False and that is honest: a one-shot has no live
        guest to ask. SPEC.md 1.4 -- never ship a heuristic in place of a real
        idle signal, and never claim one that does not exist.
        """
        return Caps(
            backend=BACKEND_NAME,
            shape=AdapterShape.ONE_SHOT,
            family=Family.Z80,
            tier=Tier.HOSTED,
            ops=frozenset({Op.CAPS, Op.BOOT, Op.RUN, Op.SEND, Op.RECV, Op.IDLE, Op.STOP}),
            cpus=(Cpu.I8080.value, Cpu.Z80.value),
            consoles=1,
            has_exit_code=False,
            has_idle_signal=False,
            version=binary_version(self._binary),
            binary_path=str(self._binary) if self._binary else None,
            divergences=DIVERGENCES,
        )

    def unsupported(
        self,
        op: str,
        *,
        reason: str | None = None,
        alternative_profile: str | None = None,
        escalation: Escalation | None = None,
    ) -> UnsupportedOp:
        """Every refusal from this backend names the same obstacle and the same
        way out. SPEC.md 4.7's worked example is this call."""
        return super().unsupported(
            op,
            reason=reason
            or (
                "cpmemu has no debugger; CPMEmulator is declared inside a "
                "3429-line cpmemu.cc with no header, so nothing can reach it "
                "from outside the process"
            ),
            alternative_profile=alternative_profile or "cpm22",
            escalation=escalation,
        )

    # -- boot -------------------------------------------------------------

    def boot(self, spec: BootSpec) -> BootOutcome:
        """A no-op, in zero milliseconds. Shape 3 has nothing to bring up.

        The hardware-tier fields are not silently ignored: a caller who passed
        a ROM or a disk asked for something this profile cannot do, and the
        warning says so rather than letting the run look equivalent.
        """
        if spec.profile and spec.profile != PROFILE:
            raise BackendFailure(
                BACKEND_NAME,
                f"{BACKEND_NAME} serves the {PROFILE!r} profile only",
                requested_profile=spec.profile,
                serves=[PROFILE],
            )

        warnings: list[str] = []
        ignored = [
            field_name
            for field_name, value in (
                ("rom", spec.rom),
                ("disks", spec.disks),
                ("boot_target", spec.boot_target),
                ("symbols", spec.symbols),
            )
            if value
        ]
        if ignored:
            warnings.append(
                f"{PROFILE} runs a .COM directly on a hosted CP/M; "
                f"{', '.join(ignored)} named hardware-tier settings that have "
                f"no effect here. A hardware profile (cpm22, cpm3) reads them."
            )
        if spec.consoles > 1:
            warnings.append(
                f"{PROFILE} has one console; consoles={spec.consoles} was "
                f"requested. Four consoles on one Z80 is the mpm2 profile."
            )
        return BootOutcome(ok=True, wall_ms=0, warnings=warnings)

    # -- run --------------------------------------------------------------

    def run(self, req: RunRequest) -> RunOutcome:
        """One exec, one deadline, one captured pair of streams.

        The five things SPEC.md 7.1 says the server does and the caller never
        sees, minus the mktemp, which belongs to the sandbox: synthesize the
        cfg, stage the input files, snapshot the guest directory, exec under a
        deadline in the sandbox's own process group, and read the answer out of
        stderr rather than out of the return code.
        """
        binary = self._require_binary()
        sandbox: SandboxProtocol = req.sandbox

        program = self._resolve_program(req.program)
        default_mode = DefaultMode(req.default_mode)
        warnings = list(self._mode_warnings(default_mode, req.eol_convert))

        for f in req.files_in:
            sandbox.stage(f)

        # After staging: a staged input is not something the run created.
        sandbox.snapshot()

        cfg_text = synthesize_cfg(
            program=program,
            guest_dir=sandbox.guest_dir,
            lst_path=sandbox.lst_path,
            pun_path=sandbox.pun_path,
            default_mode=default_mode,
            eol_convert=req.eol_convert,
        )
        cfg_path = sandbox.write_text("cpmemu.cfg", cfg_text)

        tail, tail_warnings = build_tail(req.args, Path(sandbox.guest_dir))
        warnings.extend(tail_warnings)
        argv = [str(binary), self._cpu_flag(req.cpu), str(cfg_path), *tail]

        env, env_warnings = self._filter_env(req.env)
        warnings.extend(env_warnings)

        stdin = bytes(self._pending_stdin) + req.stdin
        self._pending_stdin.clear()

        exec_result = sandbox.exec(
            argv,
            cwd=sandbox.guest_dir,
            env=env,
            stdin=stdin,
            timeout_ms=req.timeout_ms,
        )
        self._last_exec = exec_result
        self._last_stdout = exec_result.stdout
        self._recv_cursor = 0
        self._ran = True

        return self._outcome(exec_result, req, warnings)

    def _outcome(
        self, exec_result: ExecResult, req: RunRequest, warnings: list[str]
    ) -> RunOutcome:
        facts = parse_stderr(exec_result.stderr.decode("utf-8", errors="replace"))

        if facts.config_unreadable is not None:
            # Our file, not the caller's. Nothing the agent did produced this.
            raise BackendFailure(
                BACKEND_NAME,
                f"cpmemu could not read the config this server synthesized: "
                f"{facts.config_unreadable}",
                sandbox=str(req.sandbox.root),
            )

        warnings = list(warnings)
        warnings.extend(facts.warnings)

        if facts.program_unreadable is not None:
            path, reason = facts.program_unreadable
            warnings.append(
                f"cpmemu could not load the program: {path}: {reason}. "
                f"Nothing ran, so stdout and the file manifest are empty."
            )

        exit_reason = self._exit_reason(exec_result, facts, warnings)

        return RunOutcome(
            exit_reason=exit_reason,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            wall_ms=exec_result.wall_ms,
            # SPEC.md 5.4 Invariant 4. Not a nullable field that happens to be
            # None here -- a field this backend must never set.
            exit_code=None,
            exit_code_meaning=None,
            list_output_path=self._device_path(req.sandbox.lst_path),
            punch_output_path=self._device_path(req.sandbox.pun_path),
            unimplemented_bdos=facts.unimplemented_bdos,
            stderr_filtered=[],  # cpmemu prints no unconditional noise.
            config_warnings=facts.config_warnings,
            warnings=warnings,
            timed_out=exec_result.timed_out,
            raw=exec_result,
        )

    @staticmethod
    def _exit_reason(
        exec_result: ExecResult, facts: StderrFacts, warnings: list[str]
    ) -> ExitReason:
        """The deadline outranks the sentinel; the sentinel outranks the rc.

        A killed process may still have flushed a sentinel line before the
        signal landed, and the sentinel would then describe an exit that never
        finished. The supervisor's own deadline is the ground truth for that
        run (SPEC.md 5.5: nothing in the family has a timeout, so every one of
        these is ours).
        """
        if exec_result.timed_out:
            return ExitReason.TIMEOUT
        if facts.exit_reason is not None:
            return facts.exit_reason
        if facts.program_unreadable is not None:
            return ExitReason.EMULATOR_ERROR
        warnings.append(
            f"cpmemu printed no exit sentinel (rc={exec_result.rc}, "
            f"signal={exec_result.signal}); exit_reason is emulator_error "
            f"because the run ended in a way cpmemu does not report."
        )
        return ExitReason.EMULATOR_ERROR

    @staticmethod
    def _device_path(path: Path) -> Path | None:
        """A device file only counts if it is there to read."""
        try:
            return path if path.exists() else None
        except OSError:
            return None

    def _resolve_program(self, program: str) -> str:
        """The .COM, as an absolute host path that exists.

        Checked before the exec, because cpmemu's own answer is
        ``Cannot open <path>`` on stderr with rc 1 (``cpmemu.cc:3342-3344``)
        and an empty stdout, which reads to an agent like a program that ran
        and printed nothing.
        """
        p = Path(program).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            resolved = p.resolve()
            exists = resolved.is_file()
        except OSError as exc:  # pragma: no cover - a path that cannot be stat'd
            raise ToolExecutionError(
                ToolError.bad_argument("program", f"{program}: {exc}")
            ) from exc
        if not exists:
            raise ToolExecutionError(
                ToolError.bad_argument(
                    "program",
                    f"no such file: {resolved}",
                    profile=PROFILE,
                    hint=(
                        "cpm-hosted takes a host path to a .COM; a bare "
                        "guest-resident name needs a hardware profile"
                    ),
                )
            )
        return str(resolved)

    @staticmethod
    def _cpu_flag(cpu: str | None) -> str:
        """``--z80`` or ``--8080``, always written out.

        cpmemu defaults to Z80 and so does the schema (SPEC.md 6.4), but the
        flag is passed explicitly on every run for the same reason Invariant 1
        passes ``--boot=`` explicitly: a default that is not stated is a
        default that can move.
        """
        if cpu is None:
            return "--z80"
        text = str(cpu)
        if text == Cpu.Z80.value:
            return "--z80"
        if text == Cpu.I8080.value:
            return "--8080"
        raise ToolExecutionError(
            ToolError.bad_argument(
                "cpu",
                f"{BACKEND_NAME} runs 8080 or z80, not {text!r}",
                supported=[Cpu.I8080.value, Cpu.Z80.value],
            )
        )

    @staticmethod
    def _mode_warnings(default_mode: DefaultMode, eol_convert: bool) -> list[str]:
        """Invariant 3, said in the result rather than only in the docs."""
        if default_mode is not DefaultMode.AUTO:
            return []
        # SPEC.md 7.1's negative case reports this line verbatim.
        out = ["default_mode was 'auto'; cpmemu's auto mode never resolves on write"]
        if eol_convert:
            out.append(
                "default_mode 'auto' with eol_convert true is the measured "
                "corruption: BDOS 22 Make keeps MODE_AUTO (cpmemu.cc:2115) and "
                "write_with_conversion takes the text branch for it "
                "(cpmemu.cc:952-964), truncating every written file at its "
                "first 0x1A. On a 23-member ARC that gave 1 extracted file of "
                "23, at 1437 bytes instead of 1664, and process rc 0."
            )
        return out

    @staticmethod
    def _filter_env(env: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
        """Drop the variables that would undo the synthesized cfg.

        cpmemu reads the environment *after* the config file, so ``CPM_PRINTER``
        and ``CPM_AUX_OUT`` replace the ``printer`` and ``aux_output``
        directives (cpmemu README, "Environment Variables"). Honouring them
        would silently re-open Appendix B item 2 -- LST: bytes going somewhere
        the result does not report -- so they are dropped and named.
        """
        kept: dict[str, str] = {}
        warnings: list[str] = []
        for key, value in env.items():
            if key in _ENV_OVERRIDES_CONFIG:
                warnings.append(
                    f"env {key} was dropped: cpmemu reads the environment after "
                    f"the config file, so it would replace the sandbox "
                    f"printer/aux routing and the LST:/PUN: streams would not "
                    f"be reported."
                )
                continue
            kept[key] = value
        return kept, warnings

    # -- the rest of the required seven -----------------------------------

    def send(self, req: SendRequest) -> SendOutcome:
        """Queue bytes for the next :meth:`run`.

        A one-shot has no live guest between runs, so there is nothing to pace
        against and nothing to corrupt: ``paced_by`` is ``"not_waited"`` and
        that is the truth, not a shortcut. Invariant 2's hazard belongs to the
        pty backends, which have a guest that can eat a keystroke.
        """
        self._pending_stdin.extend(req.data)
        return SendOutcome(bytes_sent=len(req.data), waited_ms=0, paced_by="not_waited")

    def recv(self, req: RecvRequest) -> RecvOutcome:
        """Hand back the last run's stdout, once."""
        chunk = self._last_stdout[self._recv_cursor : self._recv_cursor + req.max_bytes]
        self._recv_cursor += len(chunk)
        return RecvOutcome(
            data=chunk,
            console=req.console,
            more=self._recv_cursor < len(self._last_stdout),
        )

    def idle(self, req: IdleRequest) -> IdleOutcome:
        """Always idle: the process is gone by the time anyone can ask.

        ``source`` says which fact answered. It is never ``"idle_signal"``
        here, because cpmemu does not have one to ask and a quiet timer
        standing in for one is the thing SPEC.md 1.4 says not to ship.
        """
        return IdleOutcome(
            idle=True, source="process_exited" if self._ran else "not_started"
        )

    def stop(self, req: StopRequest) -> StopOutcome:
        """A no-op with the last exit attached.

        The deadline and the process-group kill are the sandbox's
        (:meth:`SandboxProtocol.exec`), so by the time a caller can reach
        ``stop`` there is no group left to signal. ``stderr_tail`` carries the
        last 64 KB because SPEC.md 4.3 keeps it: "a crash is the most valuable
        moment in a debugging session".
        """
        last = self._last_exec
        return StopOutcome(
            stopped=True,
            exit_code=last.rc if last else None,
            signal=last.signal if last else None,
            stderr_tail=last.stderr[-65536:] if last else b"",
        )


# ---------------------------------------------------------------------------
# Import-time invariant check
# ---------------------------------------------------------------------------

def _assert_run_never_reports_an_exit_code() -> None:
    """SPEC.md 5.4 Invariant 4, enforced rather than documented.

    ``RunOutcome.exit_code`` is a real field -- dosiz needs it -- so the thing
    to protect is that *this* backend never sets it. The construction in
    :meth:`CpmemuBackend._outcome` is the only one in this module; if a second
    appears, or that one grows an exit_code, this fails at import.
    """
    import inspect

    source = inspect.getsource(CpmemuBackend._outcome)
    constructions = source.count("RunOutcome(")
    if constructions != 1:
        raise AssertionError(
            f"cpmemu builds {constructions} RunOutcomes; Invariant 4 is only "
            f"checked against one"
        )
    if "exit_code=None" not in source:
        raise AssertionError(
            "cpmemu's RunOutcome must set exit_code=None explicitly: SPEC.md "
            "5.4 Invariant 4, no bare exit code on any CP/M path"
        )


_assert_run_never_reports_an_exit_code()
