"""MBP -- the machine backend protocol. SPEC.md 5.3.

    "One line protocol. This is the single most important boundary in the
    design, because it is what lets an upstream improvement swap an adapter
    without touching a tool schema."

The wire form, when a backend is out of process::

    transport: newline-delimited JSON on fd 3 (control)
               guest bytes on a pty (fd 0/1)
               diagnostics on fd 2
               -- three streams, never mixed
    request:   {"id":N,"op":"...","args":{...}}
    response:  {"id":N,"ok":true,"result":{...}}

Phase 1 ships no out-of-process MBP speaker: both phase-1 backends are
*one-shot adapters* (shape 3), which live inside this server process and are
driven through :class:`Backend` directly. :func:`encode_request` /
:func:`decode_response` and :meth:`Backend.call` exist so that the day
``emu88d`` speaks MBP on fd 3, or ``romwbw_emu`` grows ``--control=PATH`` and
moves from shape 2 to shape 1, the tools above the adapter do not change.
That is the property being paid for.

The three shapes, SPEC.md 5.3:

======================  ==========================  ================
shape                   backends                    upstream change
======================  ==========================  ================
:attr:`~eightymcp.types.AdapterShape.NATIVE_MBP`
                        ``emu88d`` (new); later
                        ``romwbwd``                 we wrote it
:attr:`~eightymcp.types.AdapterShape.PTY_ADAPTER`
                        ``romwbw_emu``,
                        ``mpm2_emu``                none
:attr:`~eightymcp.types.AdapterShape.ONE_SHOT`
                        ``cpmemu``, ``dosiz``       none
======================  ==========================  ================

A backend never decides policy. It does not choose a deadline, own a sandbox,
apply a normalization or evaluate an assertion; it reports what happened and
the tool layer decides what that means. In particular a backend never converts
a process return code into success: SPEC.md 5.4 Invariant 4.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .types import (
    AdapterShape,
    Caps,
    DefaultMode,
    Escalation,
    ExecResult,
    ExitCodeMeaning,
    ExitReason,
    FileIn,
    PmFault,
    ToolError,
    ToolExecutionError,
    UnimplementedInt21,
)

__all__ = [
    "Op",
    "REQUIRED_OPS",
    "OPTIONAL_OPS",
    "ALL_OPS",
    "MbpRequest",
    "MbpResponse",
    "encode_request",
    "decode_request",
    "encode_response",
    "decode_response",
    "UnsupportedOp",
    "BackendFailure",
    "SandboxProtocol",
    "DiskSpec",
    "BootSpec",
    "BootOutcome",
    "RunRequest",
    "RunOutcome",
    "SendRequest",
    "SendOutcome",
    "RecvRequest",
    "RecvOutcome",
    "IdleRequest",
    "IdleOutcome",
    "StopRequest",
    "StopOutcome",
    "Backend",
]


# ---------------------------------------------------------------------------
# The op set
# ---------------------------------------------------------------------------

class Op(StrEnum):
    """Every MBP op. SPEC.md 5.3.

    The seven in :data:`REQUIRED_OPS` are served by every backend. The rest are
    advertised through :meth:`Backend.caps` and, when not served, raise
    :class:`UnsupportedOp` carrying the structured body of SPEC.md 4.7 -- never
    a bare "not supported", which an agent cannot act on.
    """

    # required, every backend
    CAPS = "caps"
    BOOT = "boot"
    RUN = "run"
    SEND = "send"
    RECV = "recv"
    IDLE = "idle"
    STOP = "stop"
    # optional, advertised by caps
    REGS = "regs"
    MEM_READ = "mem_read"
    MEM_WRITE = "mem_write"
    STEP = "step"
    BP_SET = "bp_set"
    BP_CLEAR = "bp_clear"
    DISASM = "disasm"
    SCREEN_TEXT = "screen_text"
    SCREEN_PIXELS = "screen_pixels"
    CONSOLE_LIST = "console_list"
    CONSOLE_SELECT = "console_select"
    TRACE_ON = "trace_on"
    TRACE_OFF = "trace_off"
    TRACE_READ = "trace_read"
    SYSCALL_BP = "syscall_bp"


#: SPEC.md 5.3: "Required ops, every backend."
REQUIRED_OPS: frozenset[str] = frozenset({
    Op.CAPS, Op.BOOT, Op.RUN, Op.SEND, Op.RECV, Op.IDLE, Op.STOP,
})

#: SPEC.md 5.3: "Optional ops, advertised by caps."
OPTIONAL_OPS: frozenset[str] = frozenset({
    Op.REGS, Op.MEM_READ, Op.MEM_WRITE, Op.STEP, Op.BP_SET, Op.BP_CLEAR,
    Op.DISASM, Op.SCREEN_TEXT, Op.SCREEN_PIXELS, Op.CONSOLE_LIST,
    Op.CONSOLE_SELECT, Op.TRACE_ON, Op.TRACE_OFF, Op.TRACE_READ, Op.SYSCALL_BP,
})

ALL_OPS: frozenset[str] = REQUIRED_OPS | OPTIONAL_OPS

assert ALL_OPS == frozenset(m.value for m in Op)
assert len(REQUIRED_OPS) == 7
assert len(OPTIONAL_OPS) == 15
assert len(ALL_OPS) == 22


# ---------------------------------------------------------------------------
# The wire, for out-of-process backends (phase 5)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MbpRequest:
    """``{"id":N,"op":"...","args":{...}}``"""

    id: int
    op: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MbpResponse:
    """``{"id":N,"ok":true,"result":{...}}``, or ``ok:false`` with ``error``.

    ``error`` is a :class:`~eightymcp.types.ToolError` body, so an out-of-process
    backend's refusal reaches the agent in the same shape as an in-process
    one's.
    """

    id: int
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def encode_request(req: MbpRequest) -> bytes:
    """One line, newline-terminated, for fd 3."""
    return (
        json.dumps(
            {"id": req.id, "op": str(req.op), "args": req.args},
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def decode_request(line: bytes | str) -> MbpRequest:
    obj = json.loads(line)
    return MbpRequest(id=int(obj["id"]), op=str(obj["op"]), args=dict(obj.get("args") or {}))


def encode_response(resp: MbpResponse) -> bytes:
    out: dict[str, Any] = {"id": resp.id, "ok": resp.ok}
    if resp.result is not None:
        out["result"] = resp.result
    if resp.error is not None:
        out["error"] = resp.error
    return json.dumps(out, separators=(",", ":")).encode("utf-8") + b"\n"


def decode_response(line: bytes | str) -> MbpResponse:
    obj = json.loads(line)
    return MbpResponse(
        id=int(obj["id"]),
        ok=bool(obj["ok"]),
        result=obj.get("result"),
        error=obj.get("error"),
    )


# ---------------------------------------------------------------------------
# Backend errors
# ---------------------------------------------------------------------------

class UnsupportedOp(ToolExecutionError):
    """An op this backend does not serve.

    Carries SPEC.md 4.7's structured body, including ``alternative_profile``
    and a literal ``escalation`` call where one exists. The spec's own example
    is worth keeping in view::

        {"error":"unsupported","op":"step","backend":"cpmemu",
         "reason":"cpmemu has no debugger; CPMEmulator is declared inside a
                   3429-line cpmemu.cc with no header, so nothing can reach it
                   from outside the process",
         "alternative_profile":"cpm22",
         "escalation":{"tool":"x80_open","arguments":{"profile":"cpm22"}}}
    """

    def __init__(
        self,
        op: str,
        backend: str,
        reason: str,
        *,
        alternative_profile: str | None = None,
        escalation: Escalation | None = None,
    ):
        super().__init__(
            ToolError.unsupported(
                str(op),
                backend,
                reason,
                alternative_profile=alternative_profile,
                escalation=escalation,
            )
        )
        self.op = str(op)
        self.backend = backend


class BackendFailure(ToolExecutionError):
    """The backend was asked to do something it serves, and it failed.

    An ``isError:true`` result, not a JSON-RPC error: the request was
    well-formed and the run went wrong, which is exactly the distinction the
    two error channels exist to make.
    """

    def __init__(self, backend: str, reason: str, **extra: Any):
        super().__init__(
            ToolError("backend_failure", {"backend": backend, "reason": reason, **extra})
        )
        self.backend = backend


# ---------------------------------------------------------------------------
# What a backend needs from a sandbox
# ---------------------------------------------------------------------------

@runtime_checkable
class SandboxProtocol(Protocol):
    """The slice of :mod:`eightymcp.sandbox` a backend is allowed to touch.

    Declared here, structurally, so that ``backends/cpmemu.py`` and
    ``backends/dosiz.py`` can be written and tested against a stub. The real
    class in :mod:`eightymcp.sandbox` will have more on it; a Protocol only
    constrains what is used.

    The four launch invariants (SPEC.md 5.4) are the sandbox's job, not the
    backend's, and :meth:`exec` is where they are enforced in one place:

    1. Hermetic launch is four things -- ``HOME=<session>/home``,
       ``XDG_CONFIG_HOME=<session>/xdg``, ``--no-config``, and an explicit
       ``--boot=``. Any three of them is not enough: measured, romwbw_emu still
       loaded ``Loaded NVRAM setting 'C' from ~/.config/romwbw_emu/nvram``
       because ``get_legacy_nvram_path()`` is built from ``$HOME``
       unconditionally.
    2. Pace before every send.
    3. Binary mode by default on cpm-hosted.
    4. No bare exit code on any CP/M path.

    :meth:`exec` also owns the deadline and the process-group kill (SPEC.md
    5.5: nothing in the family has a timeout).
    """

    #: ``<sandbox>``. The value reported as ``sandbox`` in a tool result.
    root: Path
    #: ``<sandbox>/guest`` -- the directory the guest sees as its drive/cwd.
    guest_dir: Path
    #: ``<sandbox>/home`` -- Invariant 1's ``HOME``.
    home_dir: Path
    #: ``<sandbox>/xdg`` -- Invariant 1's ``XDG_CONFIG_HOME``.
    xdg_dir: Path
    #: ``<sandbox>/.lst`` -- CP/M LST: device. Never empty, never /dev/null.
    lst_path: Path
    #: ``<sandbox>/.pun`` -- CP/M PUN: device. Never empty, never /dev/null.
    pun_path: Path

    def stage(self, f: FileIn) -> Path:
        """Materialise one :class:`~eightymcp.types.FileIn` into the guest
        directory and return the host path written."""
        ...

    def snapshot(self) -> None:
        """Record the guest directory's contents, so that
        :meth:`created_since_snapshot` can answer "what did this run make?"
        -- the question no emulator in the family reports today."""
        ...

    def created_since_snapshot(self) -> list[Path]:
        """Host paths created or modified since :meth:`snapshot`."""
        ...

    def write_text(self, relative: str, text: str) -> Path:
        """Write a supervisor-owned file (a synthesized cpmemu ``.cfg``, for
        instance) into the sandbox root and return its path."""
        ...

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes = b"",
        timeout_ms: int = 10000,
    ) -> ExecResult:
        """Run one command in its own process group under a wall-clock
        deadline, killing the whole group on expiry.

        ``env`` is *added to* the hermetic base the sandbox builds; it does not
        replace it, so a backend cannot accidentally drop ``HOME`` or
        ``XDG_CONFIG_HOME`` and reintroduce the NVRAM leak.
        """
        ...


# ---------------------------------------------------------------------------
# Op arguments and results
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DiskSpec:
    """One ``disks[]`` entry from ``x80_open``.

    SPEC.md 5.5: catalog-sourced disks default ``write_protect:true``; a raw
    ``host_path`` defaults writable and is cloned regardless. Writable images
    are copy-on-write cloned into the session directory and the catalog copy is
    never mutated -- on APFS ``clonefile()`` makes the 51,380,224-byte combo
    image a matter of microseconds.
    """

    unit: int
    image_id: str | None = None
    host_path: str | None = None
    write_protect: bool = True


@dataclass(slots=True)
class BootSpec:
    """Arguments to :meth:`Backend.boot`.

    On a one-shot backend boot is a no-op and every hardware-tier field is
    ignored; the fields are here because the shape must be the one SPEC.md 5.3
    describes, so a pty or native backend drops in later without touching a
    tool.
    """

    sandbox: SandboxProtocol
    profile: str
    #: SPEC.md 5.4 Invariant 1 and Appendix B item 4: ALWAYS sent explicitly.
    #: romwbw_emu reads the legacy ``~/.config`` NVRAM as a migration fallback
    #: even under a sandbox ``XDG_CONFIG_HOME`` with ``--no-config``
    #: (``romwbw_emu.cc:1379-1390``, measured), so omitting this boots whatever
    #: the developer last chose interactively. There is no ``--start=ADDR``.
    boot_target: str | None = None
    rom: str | None = None
    disks: list[DiskSpec] = field(default_factory=list)
    cpu: str | None = None
    consoles: int = 1
    symbols: str | None = None
    #: SPEC.md 5.5: a boot deadline is a different control from a lifetime TTL
    #: and both are needed.
    boot_timeout_ms: int = 30000
    #: Hosted tier only: the one guest binary that will BE the session.
    program: str | None = None
    args: list[str] = field(default_factory=list)
    default_mode: DefaultMode = DefaultMode.BINARY
    eol_convert: bool = False
    files_in: list[FileIn] = field(default_factory=list)
    dialect: str = "xterm"
    record: bool = True
    mirror: bool = False
    allow_version_mismatch: bool = False
    #: Backend-specific extras. Nothing above the adapter reads this.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BootOutcome:
    """Result of :meth:`Backend.boot`.

    ``stopped`` is ``"boot_timeout"`` when the boot deadline fired, and
    ``boot_output`` then carries whatever arrived -- SPEC.md 5.5 requires the
    partial output rather than a hung tool call.
    """

    ok: bool
    boot_output: bytes = b""
    wall_ms: int = 0
    stopped: str | None = None
    warnings: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunRequest:
    """Arguments to :meth:`Backend.run`.

    For a one-shot backend this is the whole job: exec in the sandbox with this
    stdin, capture streams, wait for exit or deadline.
    """

    sandbox: SandboxProtocol
    #: Host path to the .COM/.EXE on the hosted tier; a host path staged onto
    #: the session image, or a bare guest-resident name, on the hardware tier.
    program: str
    args: list[str] = field(default_factory=list)
    #: Bytes fed to the guest console. Not str: a CP/M console stream is bytes.
    stdin: bytes = b""
    #: SPEC.md 5.5: enforced by the supervisor and killed by process group; no
    #: backend in this family has an internal wall clock.
    timeout_ms: int = 10000
    cpu: str | None = None
    #: SPEC.md 5.4 Invariant 3. Never default this to AUTO.
    default_mode: DefaultMode = DefaultMode.BINARY
    eol_convert: bool = False
    files_in: list[FileIn] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    boot_timeout_ms: int = 30000
    #: Phase 2: stop as soon as this literal substring appears.
    until: str | None = None
    #: Phase 2: instruction-count cap for this call.
    max_steps: int | None = None
    #: Phase 3: which mpm2 console. The console NUMBER the guest announced,
    #: not an attach index (SPEC.md 3.4).
    console: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunOutcome:
    """Result of :meth:`Backend.run`. One shape, two tool results.

    :mod:`eightymcp.tools` turns this into a
    :class:`~eightymcp.types.RunResult` for ``x80_cpm_run`` and a
    :class:`~eightymcp.types.DosRunResult` for ``x80_dos_run``. The CP/M path
    reads ``exit_reason`` and ignores ``exit_code`` -- which a CP/M backend
    must leave at ``None`` anyway (SPEC.md 5.4 Invariant 4).

    ``exit_reason`` comes from the backend's stable stderr sentinel lines where
    one was printed, and from the supervisor otherwise (TIMEOUT, CTRL_C,
    EMULATOR_ERROR). See :class:`~eightymcp.types.ExitReason`.

    ``stdout`` and ``stderr`` are bytes; ``stderr`` is the *unfiltered* stream.
    Anything the adapter removed by name is listed in ``stderr_filtered``, so
    the filtering is recorded rather than silently swallowed (SPEC.md 5.5).
    """

    exit_reason: ExitReason
    stdout: bytes = b""
    stderr: bytes = b""
    wall_ms: int = 0
    #: dosiz only. Must stay None on every CP/M backend.
    exit_code: int | None = None
    exit_code_meaning: ExitCodeMeaning | None = None
    #: Where the LST:/PUN: streams landed, for the tool layer to hash and size.
    list_output_path: Path | None = None
    punch_output_path: Path | None = None
    #: cpmemu's "Unimplemented BDOS function N" lines, deduplicated.
    unimplemented_bdos: list[int] = field(default_factory=list)
    #: dosiz's "unimplemented INT 21h AH=XXh" lines, one per distinct AH.
    unimplemented_int21: list[UnimplementedInt21] = field(default_factory=list)
    pm_fault: PmFault | None = None
    #: Known-noise stderr lines this adapter removed, verbatim.
    stderr_filtered: list[str] = field(default_factory=list)
    #: Problems with the synthesized config, e.g. a mapping that will not route
    #: a BDOS 22 Make.
    config_warnings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timed_out: bool = False
    #: The raw exec, for the doctor and for debugging. Never surfaced to an
    #: agent as-is.
    raw: ExecResult | None = None


@dataclass(slots=True)
class SendRequest:
    """Arguments to :meth:`Backend.send`.

    SPEC.md 5.4 Invariant 2 -- pace before every send, or you corrupt the run.
    Measured: the only change between a working and a broken run was removing a
    1.5 s wait; the ``DIR`` was eaten by romldr's ``AutoBoot in 0 Seconds``
    prompt and the process still returned 0. ``wait_idle`` defaults true and
    the wait is for the backend's *real* idle signal or a prompt match, never a
    fixed sleep.
    """

    data: bytes
    console: int | None = None
    wait_idle: bool = True
    wait_timeout_ms: int = 5000


@dataclass(slots=True)
class SendOutcome:
    bytes_sent: int
    waited_ms: int = 0
    #: How the pacing wait resolved: "idle_signal", "match", "quiet_timer",
    #: "not_waited" or "timeout". Reported because Invariant 2's failure mode
    #: is silent.
    paced_by: str = "not_waited"


@dataclass(slots=True)
class RecvRequest:
    max_bytes: int = 65536
    console: int | None = None
    timeout_ms: int = 0


@dataclass(slots=True)
class RecvOutcome:
    data: bytes
    console: int | None = None
    #: True when ``max_bytes`` cut the read short.
    more: bool = False


@dataclass(slots=True)
class IdleRequest:
    console: int | None = None
    #: Used only where a backend has no real idle signal. romwbw_emu, mpm2 and
    #: emu88 all have one and it is preferred (SPEC.md 1.4, 6.5).
    quiet_ms: int = 300


@dataclass(slots=True)
class IdleOutcome:
    """``source`` is not decoration.

    SPEC.md 1.4: "Make the idle signal actually fire ... Every one of your
    backends has a *real* idle signal (``HBIOSDispatch::isConsoleIdle()``,
    ``EmulatorEngine::isIdle()``, ``dos_machine::is_waiting_for_key()``); use
    it, and never ship a heuristic in its place." Reporting which one answered
    makes a heuristic creeping back in visible.
    """

    idle: bool
    #: "idle_signal" | "quiet_timer" | "process_exited" | "not_started"
    source: str = "idle_signal"


@dataclass(slots=True)
class StopRequest:
    flush_disks: bool = True
    timeout_ms: int = 5000


@dataclass(slots=True)
class StopOutcome:
    stopped: bool
    exit_code: int | None = None
    signal: int | None = None
    #: SPEC.md 4.3: a crashed machine keeps the last 64 KB of stderr, because
    #: "a crash is the most valuable moment in a debugging session".
    stderr_tail: bytes = b""


# ---------------------------------------------------------------------------
# The Backend ABC
# ---------------------------------------------------------------------------

class Backend(ABC):
    """One machine backend. SPEC.md 5.3.

    Subclass responsibilities, in order:

    * set :attr:`name` and :attr:`shape` as class attributes;
    * implement the seven required ops;
    * override only the optional ops it actually serves, and list every op it
      serves in :meth:`caps`;
    * never invent a deadline, a sandbox or a success criterion.

    A one-shot backend (phase 1) still implements all seven. The semantics are:

    ``boot``   no-op, returns ``BootOutcome(ok=True)`` immediately.
    ``run``    exec in the sandbox, capture, wait for exit or deadline.
    ``send``   append to the pending stdin buffer for the next ``run``.
    ``recv``   return the captured output of the last ``run``.
    ``idle``   ``idle=True`` with ``source="process_exited"`` when no run is in
               flight.
    ``stop``   kill the process group if one is running; otherwise a no-op.
    """

    #: Backend id as it appears in ``profiles[].backend`` and in the ``backend``
    #: field of a :class:`UnsupportedOp` body. E.g. ``"cpmemu"``, ``"dosiz"``.
    name: str = "unnamed"

    #: Which of SPEC.md 5.3's three adapter shapes this is.
    shape: AdapterShape = AdapterShape.ONE_SHOT

    # -- required ops -----------------------------------------------------

    @abstractmethod
    def caps(self) -> Caps:
        """Everything this backend serves.

        Callers ask this instead of testing the class, which is what lets
        romwbw_emu move from a pty adapter to a native MBP backend when it
        grows ``--control=PATH`` with nothing above the adapter changing.
        """

    @abstractmethod
    def boot(self, spec: BootSpec) -> BootOutcome:
        """Bring the machine to a usable state. A no-op on a one-shot."""

    @abstractmethod
    def run(self, req: RunRequest) -> RunOutcome:
        """Run to completion, a match, or the deadline."""

    @abstractmethod
    def send(self, req: SendRequest) -> SendOutcome:
        """Type at the guest. Paces first unless told not to (Invariant 2)."""

    @abstractmethod
    def recv(self, req: RecvRequest) -> RecvOutcome:
        """Read whatever the guest has produced and not yet been read."""

    @abstractmethod
    def idle(self, req: IdleRequest) -> IdleOutcome:
        """Is the guest waiting for input? From a real signal where one
        exists."""

    @abstractmethod
    def stop(self, req: StopRequest) -> StopOutcome:
        """Tear the machine down, by process group."""

    # -- optional ops, advertised by caps ---------------------------------
    #
    # Each default raises UnsupportedOp with the structured body of SPEC.md
    # 4.7. A backend that serves one overrides it AND lists it in caps().ops;
    # `serves` below checks the two agree.

    def regs(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.REGS)

    def mem_read(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.MEM_READ)

    def mem_write(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.MEM_WRITE)

    def step(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.STEP)

    def bp_set(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.BP_SET)

    def bp_clear(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.BP_CLEAR)

    def disasm(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.DISASM)

    def screen_text(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.SCREEN_TEXT)

    def screen_pixels(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.SCREEN_PIXELS)

    def console_list(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.CONSOLE_LIST)

    def console_select(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.CONSOLE_SELECT)

    def trace_on(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.TRACE_ON)

    def trace_off(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.TRACE_OFF)

    def trace_read(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.TRACE_READ)

    def syscall_bp(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raise self.unsupported(Op.SYSCALL_BP)

    # -- helpers ----------------------------------------------------------

    def serves(self, op: str) -> bool:
        """Does :meth:`caps` advertise this op?"""
        return self.caps().supports(str(op))

    def unsupported(
        self,
        op: str,
        *,
        reason: str | None = None,
        alternative_profile: str | None = None,
        escalation: Escalation | None = None,
    ) -> UnsupportedOp:
        """Build the refusal for an op this backend does not serve.

        Returned rather than raised so a caller can ``raise self.unsupported(
        ...)`` and keep the traceback at the call site. Subclasses should pass
        a ``reason`` naming the actual obstacle -- SPEC.md 4.7's example says
        *why* cpmemu has no debugger, which is what makes the message
        actionable.
        """
        return UnsupportedOp(
            str(op),
            self.name,
            reason or f"{self.name} does not serve the {op} op",
            alternative_profile=alternative_profile,
            escalation=escalation,
        )

    #: Ops :meth:`call` can dispatch from a plain JSON args object: everything
    #: whose arguments survive a JSON round trip. The other six required ops
    #: (boot, run, send, recv, idle, stop) take typed dataclasses because they
    #: carry a sandbox handle and raw guest bytes, which do not.
    JSON_DISPATCHABLE_OPS: frozenset[str] = OPTIONAL_OPS | {Op.CAPS}

    def call(self, op: str, args: Mapping[str, Any] | None = None) -> Any:
        """Generic MBP dispatch by op name, for the ops in
        :attr:`JSON_DISPATCHABLE_OPS`.

        This is the path an out-of-process MBP speaker's fd-3 loop will use for
        the debugger ops. ``boot``/``run``/``send``/``recv``/``idle``/``stop``
        are reached through their typed methods; when a native MBP backend
        lands (SPEC.md 8, item 2) its fd-3 loop deserializes those itself,
        because a sandbox is a live object and guest output is bytes.
        """
        name = str(op)
        if name not in ALL_OPS:
            raise self.unsupported(name, reason=f"{name!r} is not an MBP op")
        if name not in self.JSON_DISPATCHABLE_OPS:
            raise self.unsupported(
                name,
                reason=(
                    f"{name} takes typed arguments (a sandbox handle and raw "
                    f"bytes); call Backend.{name}() directly"
                ),
            )
        if name == Op.CAPS:
            return self.caps()
        return getattr(self, name)(dict(args or {}))
