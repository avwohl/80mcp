"""Dataclasses and enums shared by every module in 80mcp.

This module imports nothing from the rest of the package, so everything else
may import it freely.

Two rules govern the shapes here, and both come out of measurement rather than
taste:

* **SPEC.md 5.4 Invariant 4 -- no bare exit code on any CP/M path.**
  :class:`RunResult` has no ``exit_code`` field at all: not a nullable one, not
  one guarded by an ``if``. cpmemu ``exit(0)``s on every normal path including
  its runaway watchdog, romwbw_emu always returns 0, and a measured run that
  extracted 1 of 23 ARC members, printed ``Error`` and truncated the one file
  it wrote still exited 0. :func:`_assert_no_exit_code_on_cpm` enforces it at
  import time. :class:`DosRunResult` does carry one, because dosiz propagates
  the DOS AH=4Ch AL value (measured: ``DJ_PRINTF.exe`` -> rc 7).

* **SPEC.md Appendix B item 6.** The rejected ``resultType`` and
  ``cacheScope`` values are not legal. :class:`ResultType` and
  :class:`CacheScope` are closed enums, checked at import time.

Field names match the JSON keys they serialize to, so that four agents writing
four modules do not each invent a different mapping. The two exceptions are
Python keywords and are called out where they occur: ``pass`` -> ``pass_`` on
the result types, ``for`` -> ``for_`` on :class:`ExternalServerRec`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any, Iterable, Mapping

__all__ = [
    # sentinels and helpers
    "UNSET", "Unset", "compact", "decode_guest", "guest_text_encoding",
    # protocol enums
    "ProtocolRevision", "ResultType", "CacheScope",
    # domain enums
    "Family", "Tier", "Cpu", "AdapterShape", "FileMode", "DefaultMode",
    "Normalization", "ReturnContent", "CompareMode", "FilesOp", "Via", "Since",
    "ResolveMechanism", "ExitReason", "ExitCodeMeaning", "Stopped",
    "ProbeVerdict", "AssertionKind", "SyscallLayer",
    # annotations
    "ToolAnnotations",
    # errors
    "ToolError", "ToolExecutionError",
    # file and stream shapes
    "FileIn", "FileOut", "StreamOut", "Assertion", "Fidelity",
    # diagnostics
    "CpmDiagnostics", "DosDiagnostics", "UnimplementedInt21", "PmFault",
    "DOSIZ_STDERR_NOISE",
    # results
    "RunResult", "DosRunResult", "FileDiff", "DiffResult",
    "UnimplementedCall", "Escalation", "ProbeResult",
    # profiles
    "ImageRequirement", "ProfileImages", "Profile", "ExternalServerRec",
    "ProfilesResult", "Caps",
    # process
    "ExecResult",
]


# ---------------------------------------------------------------------------
# Sentinels and small helpers
# ---------------------------------------------------------------------------

class Unset:
    """Type of :data:`UNSET`.

    ``None`` is a real value in several of these shapes -- ``exit_code: null``
    on the freedos profile means "this backend has no exit code", which is not
    the same as "the field is absent". :data:`UNSET` distinguishes them.
    """

    _instance: "Unset | None" = None

    def __new__(cls) -> "Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "UNSET"


#: Absent, as distinct from ``None``.
UNSET = Unset()


def compact(obj: Mapping[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is :data:`UNSET`. ``None`` is kept.

    Insertion order is preserved, so a ``to_json`` that builds its dict in
    SPEC.md's field order emits SPEC.md's field order.
    """
    return {k: v for k, v in obj.items() if not isinstance(v, Unset)}


def guest_text_encoding(data: bytes) -> str:
    """Return the codec :func:`decode_guest` will use for ``data``.

    utf-8 when the bytes are valid utf-8, otherwise latin-1. Reported in
    ``warnings`` by the tool layer when it is not utf-8, because a caller
    diffing guest output needs to know which mapping produced the string.
    """
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "latin-1"
    return "utf-8"


def decode_guest(data: bytes) -> str:
    """Decode guest console bytes to a str without losing any byte.

    utf-8 first; latin-1 as the fallback, which is lossless because every one
    of the 256 byte values maps to a distinct codepoint. Never
    ``errors="replace"``: a CP/M or DOS transcript is evidence, and an agent
    asserting on it must be able to get the original bytes back.
    """
    return data.decode(guest_text_encoding(data))


# ---------------------------------------------------------------------------
# Protocol enums
# ---------------------------------------------------------------------------

class ProtocolRevision(StrEnum):
    """MCP revisions 80mcp speaks.

    The values are ISO dates, so lexicographic order is chronological order and
    ``a >= b`` on the string is a real revision comparison.

    SPEC.md targets 2026-07-28 throughout (section 4). The two older revisions
    are here because every shipping MCP client today still opens with an
    ``initialize`` request, which 2026-07-28 removed; a server that only speaks
    2026-07-28 connects to nothing. See :mod:`eightymcp.jsonrpc`.
    """

    V2026_07_28 = "2026-07-28"
    V2025_11_25 = "2025-11-25"
    V2025_06_18 = "2025-06-18"


class ResultType(StrEnum):
    """SPEC.md 4.1: legal ``resultType`` values, from ``schema.ts:216``.

    The rejected third value is not one of them (Appendix B item 6). It is
    never spelled as a quoted literal anywhere in this package: the SPEC.md
    6.0 conformance test greps for it and fails the build on a hit.

    SPEC.md 4.5: v1 never returns ``INPUT_REQUIRED``. Every destructive thing
    is an explicit required argument in the schema (``overwrite:true``,
    ``allow_fetch:true``) and the server returns ``isError`` instead of asking,
    which deletes the whole re-entrancy hazard class rather than mitigating it.
    The member exists because the protocol defines it, not because we emit it.
    """

    COMPLETE = "complete"
    INPUT_REQUIRED = "input_required"


class CacheScope(StrEnum):
    """SPEC.md 4.1: legal ``cacheScope`` values, from ``schema.ts:1109``.

    ``"server"`` is not one of them (Appendix B item 6).
    """

    PUBLIC = "public"
    PRIVATE = "private"


assert [m.value for m in ResultType] == ["complete", "input_required"]
assert [m.value for m in CacheScope] == ["public", "private"]


# ---------------------------------------------------------------------------
# Domain enums
# ---------------------------------------------------------------------------

class Family(StrEnum):
    """CPU family. The ``family`` schema argument also accepts ``"all"`` /
    ``"auto"``; those are filters, not families, and are not members here."""

    Z80 = "z80"
    X86 = "x86"


class Tier(StrEnum):
    """SPEC.md 3.1, the 3x2 grid.

    HOSTED translates the OS API straight to the host filesystem (cpmemu,
    dosiz): instant, no disk image, file-oriented. HARDWARE runs the real OS on
    an emulated machine (romwbw_emu, mpm2_emu, emu88d).
    """

    HOSTED = "hosted"
    HARDWARE = "hardware"


class Cpu(StrEnum):
    """The union of the ``cpu`` enums across the tool schemas."""

    I8080 = "8080"
    Z80 = "z80"
    I8088 = "8088"
    I186 = "186"
    I286 = "286"
    I386 = "386"


class AdapterShape(StrEnum):
    """SPEC.md 5.3, the three adapter shapes. Every backend is one of them.

    ONE_SHOT is the only shape phase 1 ships: ``boot`` is a no-op, ``run`` is
    an exec in the sandbox with this stdin, capture streams, wait for exit or
    deadline; no ``regs``, no ``step``.
    """

    NATIVE_MBP = "native_mbp"
    PTY_ADAPTER = "pty_adapter"
    ONE_SHOT = "one_shot"


class FileMode(StrEnum):
    """``FileIn.mode`` -- how a staged file is written into the sandbox."""

    BINARY = "binary"
    TEXT = "text"


class DefaultMode(StrEnum):
    """cpmemu's ``default_mode`` config key.

    SPEC.md 5.4 Invariant 3: AUTO never resolves on *write*. Measured, the same
    23-member ARC under ``default_mode = auto`` extracted 1 of 23, printed
    ``Error``, truncated the one file it wrote (1437 bytes vs 1664 under
    binary), and exited 0. BINARY is the default everywhere as a correctness
    requirement.
    """

    BINARY = "binary"
    TEXT = "text"
    AUTO = "auto"


class Normalization(StrEnum):
    """SPEC.md 6.3 ``Normalize``. An enum, never a boolean.

    Measured on a 23-member ARC: with no normalization ``diff -rq`` reports 46
    "Only in" lines and 0 matches; under LOWERCASE_NAMES alone, 2 match and 21
    differ; under LOWERCASE_NAMES + PAD_TO_RECORD, 23 of 23 match. Those are
    exactly the two that are needed and exactly no more.
    """

    LOWERCASE_NAMES = "lowercase_names"
    PAD_TO_RECORD = "pad_to_record"
    STRIP_CPM_EOF = "strip_cpm_eof"
    CRLF_TO_LF = "crlf_to_lf"


class ReturnContent(StrEnum):
    """``Collect.return_content``."""

    NEVER = "never"
    INLINE_IF_UNDER_KB = "inline_if_under_kb"
    ALWAYS = "always"


class CompareMode(StrEnum):
    """``x80_diff_run.compare``."""

    BYTES = "bytes"
    SHA256 = "sha256"


class FilesOp(StrEnum):
    """``x80_files.op``. The direction is in the op name, not in a flag."""

    LIST = "list"
    TO_GUEST = "to_guest"
    FROM_GUEST = "from_guest"
    HANDLES = "handles"
    RESOLVE = "resolve"


class Via(StrEnum):
    """``x80_files.via``. Only SANDBOX ships in phase 1 (SPEC.md 6.1)."""

    AUTO = "auto"
    SANDBOX = "sandbox"
    HOSTFILE = "hostfile"
    IMAGE = "image"


class Since(StrEnum):
    """``x80_files.since``."""

    START = "start"
    LAST_CALL = "last_call"
    NEVER = "never"


class ResolveMechanism(StrEnum):
    """``x80_files{op:"resolve"}`` -> ``mechanism``."""

    CONFIG_MAPPING = "config_mapping"
    ARGV_AUTOMAP = "argv_automap"
    DRIVE_LETTER = "drive_letter"
    CWD_FALLBACK = "cwd_fallback"


class ExitReason(StrEnum):
    """SPEC.md 6.4, ``x80_cpm_run``'s ``exit_reason``. Exactly this list.

    Parsed from the backend's stable stderr sentinel lines:

    ==========================================  ====================
    stderr sentinel                             member
    ==========================================  ====================
    ``Program exit via JMP 0``                  JMP_0
    ``System reset``                            BDOS_0
    ``BIOS WBOOT called - exiting``             WBOOT
    ``Reached instruction limit``               INSTRUCTION_LIMIT
    ``[Exiting: 1024 console reads past end     EOF_GIVEUP
    of input]``
    ==========================================  ====================

    CTRL_C, TIMEOUT, BOOT_TIMEOUT and EMULATOR_ERROR are decided by the
    supervisor, not by the sentinel: no backend in this family has an internal
    wall clock (SPEC.md 5.5), so the deadline is ours and so is the reason.
    """

    JMP_0 = "jmp_0"
    BDOS_0 = "bdos_0"
    WBOOT = "wboot"
    INSTRUCTION_LIMIT = "instruction_limit"
    EOF_GIVEUP = "eof_giveup"
    CTRL_C = "ctrl_c"
    TIMEOUT = "timeout"
    BOOT_TIMEOUT = "boot_timeout"
    EMULATOR_ERROR = "emulator_error"


class ExitCodeMeaning(StrEnum):
    """SPEC.md 6.4, ``x80_dos_run``'s ``exit_code_meaning``.

    dosiz rc 1 is ambiguous between "the guest exited 1" and "dosiz failed to
    load the program", so the meaning is disambiguated from stderr rather than
    guessed from the number. NONE_AVAILABLE is the freedos profile: emu88 has
    no DOS and therefore no ERRORLEVEL.
    """

    GUEST_EXIT = "guest_exit"
    LOADER_FAILURE = "loader_failure"
    PM_FAULT = "pm_fault"
    TIMEOUT = "timeout"
    NONE_AVAILABLE = "none_available"


class Stopped(StrEnum):
    """SPEC.md 6.5, the ``stopped`` enum on ``x80_run`` (line 1011) plus
    ``x80_step``'s COUNT (line 1081).

    Phase 2 surface, defined here in phase 1 so that the conformance test in
    SPEC.md 6.0 -- "stripping x80_ leaves a superset of altairsim's interactive
    verb set and its stopped enum" -- has one place to check.

    SPEC.md 1.4: IDLE must come from the backend's real idle signal
    (``HBIOSDispatch::isConsoleIdle()``, ``EmulatorEngine::isIdle()``,
    ``dos_machine::is_waiting_for_key()``), never from a heuristic. altairsim's
    heuristic fires instantly on one machine and never on another: measured, a
    ``run`` at the CP/M 3 ``A>`` prompt burned the full 20.00 s and returned
    TIMEOUT.
    """

    MATCH = "match"
    IDLE = "idle"
    TIMEOUT = "timeout"
    MAX_STEPS = "max_steps"
    HALT = "halt"
    BREAKPOINT = "breakpoint"
    SYSCALL_BREAKPOINT = "syscall_breakpoint"
    PAGER = "pager"
    EXITED = "exited"
    CRASHED = "crashed"
    BOOT_TIMEOUT = "boot_timeout"
    COUNT = "count"


class ProbeVerdict(StrEnum):
    """``x80_probe`` -> ``verdict``."""

    SUFFICIENT = "sufficient"
    NEEDS_RICHER_OS = "needs_richer_os"
    NEEDS_HARDWARE_TIER = "needs_hardware_tier"
    FAILED_TO_LOAD = "failed_to_load"


class AssertionKind(StrEnum):
    """The ``kind`` of an :class:`Assertion`, one per key of an ``assert``
    object across the phase-1 schemas, plus ``expect_exit_code`` which
    ``x80_dos_run`` carries as a top-level argument."""

    STDOUT_CONTAINS = "stdout_contains"
    STDOUT_NOT_CONTAINS = "stdout_not_contains"
    FILES_CREATED = "files_created"
    FILE_SHA256 = "file_sha256"
    NO_UNIMPLEMENTED_BDOS = "no_unimplemented_bdos"
    NO_UNIMPLEMENTED_INT21 = "no_unimplemented_int21"
    EXPECT_EXIT_CODE = "expect_exit_code"


class SyscallLayer(StrEnum):
    """``x80_probe.unimplemented[].layer`` and ``x80_trace.layers``."""

    BDOS = "bdos"
    BIOS = "bios"
    XDOS = "xdos"
    INT21 = "int21"
    INT31 = "int31"
    INT67 = "int67"
    INT10 = "int10"
    DPMI = "dpmi"
    EXCEPTIONS = "exceptions"
    HBIOS = "hbios"
    INSTRUCTIONS = "instructions"


# ---------------------------------------------------------------------------
# Tool annotations
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolAnnotations:
    """MCP tool annotations.

    SPEC.md 4.1 records the defaults that bite: ``readOnlyHint`` false,
    ``destructiveHint`` **true**, ``idempotentHint`` false, ``openWorldHint``
    **true**. A server that omits annotations therefore ships every tool as
    open-world and destructive, so every field here is required and every tool
    carries an explicit row (SPEC.md 6.2, transcribed in
    :data:`eightymcp.schemas.ANNOTATIONS`).

    The JSON keys are camelCase because MCP's are.
    """

    readOnlyHint: bool
    destructiveHint: bool
    idempotentHint: bool
    openWorldHint: bool
    title: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.title is not None:
            out["title"] = self.title
        out["readOnlyHint"] = self.readOnlyHint
        out["destructiveHint"] = self.destructiveHint
        out["idempotentHint"] = self.idempotentHint
        out["openWorldHint"] = self.openWorldHint
        return out


# ---------------------------------------------------------------------------
# Errors an agent reads
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolError:
    """A structured, actionable tool-execution error.

    This is the body of an ``isError:true`` result, never a JSON-RPC error
    object. SPEC.md 4.2 requires it for an expired handle -- "expired handle ->
    tool execution error, not JSON-RPC error" -- and SPEC.md 4.7 gives the
    shape for an unsupported op::

        {"error":"unsupported","op":"step","backend":"cpmemu",
         "reason":"cpmemu has no debugger; ...",
         "alternative_profile":"cpm22",
         "escalation":{"tool":"x80_open","arguments":{"profile":"cpm22"}}}

    "An agent can act on that. A bare "not supported" cannot be acted on."

    ``error`` is the machine-readable discriminator; everything else goes in
    ``fields`` and is spread into the JSON object at the top level, matching
    the spec's examples.
    """

    error: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.error}
        out.update(self.fields)
        return out

    # -- constructors for the cases the spec names ------------------------

    @classmethod
    def unsupported(
        cls,
        op: str,
        backend: str,
        reason: str,
        *,
        alternative_profile: str | None = None,
        escalation: "Escalation | None" = None,
    ) -> "ToolError":
        """SPEC.md 4.7, verbatim shape."""
        f: dict[str, Any] = {"op": op, "backend": backend, "reason": reason}
        if alternative_profile is not None:
            f["alternative_profile"] = alternative_profile
        if escalation is not None:
            f["escalation"] = escalation.to_json()
        return cls("unsupported", f)

    @classmethod
    def handle_expired(
        cls, expired_at: str, sandbox_path: str | None = None
    ) -> "ToolError":
        """SPEC.md 4.2, verbatim shape. The hint quotes the 4.3 retention
        policy: the sandbox outlives the handle by 24 hours."""
        f: dict[str, Any] = {"expired_at": expired_at}
        if sandbox_path is not None:
            f["sandbox_path"] = sandbox_path
        f["hint"] = "the sandbox is preserved for 24h; call x80_open again"
        return cls("handle_expired", f)

    @classmethod
    def timeout(
        cls, *, deadline_ms: int, phase: str = "run", partial: Any = None
    ) -> "ToolError":
        """A deadline the supervisor enforced. SPEC.md 5.5: nothing in the
        family has a timeout, so every one of these is ours."""
        f: dict[str, Any] = {"deadline_ms": deadline_ms, "phase": phase}
        if partial is not None:
            f["partial"] = partial
        return cls("timeout", f)

    @classmethod
    def bad_argument(cls, argument: str, reason: str, **extra: Any) -> "ToolError":
        """An argument that passed the schema but cannot be honoured -- an
        out-of-range drive letter, a host path that does not exist, a
        ``handle`` and a ``sandbox`` supplied together."""
        return cls("bad_argument", {"argument": argument, "reason": reason, **extra})

    @classmethod
    def backend_missing(
        cls, backend: str, reason: str, *, hint: str | None = None
    ) -> "ToolError":
        """SPEC.md briefing rule: an absent backend is a profile reporting
        ready:false with a blocked_by string, never a crash."""
        f: dict[str, Any] = {"backend": backend, "reason": reason}
        if hint is not None:
            f["hint"] = hint
        return cls("backend_missing", f)


class ToolExecutionError(Exception):
    """Raised inside a tool handler to produce an ``isError:true`` result.

    :mod:`eightymcp.server` catches this and turns it into a normal result with
    ``isError:true``, ``resultType:"complete"`` and ``structuredContent`` set
    to ``err.to_json()``. It is never turned into a JSON-RPC error: the two
    channels are kept rigorously apart, because a JSON-RPC error means the
    request was malformed and an ``isError`` result means the request was fine
    and the run failed.
    """

    def __init__(self, err: ToolError):
        super().__init__(err.error)
        self.err = err

    def to_json(self) -> dict[str, Any]:
        return self.err.to_json()


# ---------------------------------------------------------------------------
# File and stream shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FileIn:
    """SPEC.md 6.3 ``FileIn``. Exactly one of ``host_path`` / ``content_b64``.

    The schema enforces the ``oneOf``; :meth:`from_json` re-checks it because a
    backend may build one of these itself.
    """

    guest_name: str
    host_path: str | None = None
    content_b64: str | None = None
    mode: FileMode = FileMode.BINARY

    def __post_init__(self) -> None:
        if (self.host_path is None) == (self.content_b64 is None):
            raise ValueError(
                "FileIn requires exactly one of host_path or content_b64 "
                f"(guest_name={self.guest_name!r})"
            )

    @classmethod
    def from_json(cls, obj: Mapping[str, Any]) -> "FileIn":
        return cls(
            guest_name=obj["guest_name"],
            host_path=obj.get("host_path"),
            content_b64=obj.get("content_b64"),
            mode=FileMode(obj.get("mode", "binary")),
        )

    @classmethod
    def list_from_json(cls, items: Iterable[Mapping[str, Any]]) -> list["FileIn"]:
        return [cls.from_json(o) for o in items]

    def to_json(self) -> dict[str, Any]:
        return compact({
            "guest_name": self.guest_name,
            "host_path": self.host_path if self.host_path is not None else UNSET,
            "content_b64": self.content_b64 if self.content_b64 is not None else UNSET,
            "mode": self.mode,
        })


@dataclass(slots=True)
class FileOut:
    """One entry of ``files_out``.

    ``host_name`` is reported separately from ``guest_name`` because cpmemu
    lowercases on create -- ``B5-TIME.INF`` becomes ``b5-time.inf`` on the host
    (measured) -- so a caller writing case-sensitive assertions against host
    names will be wrong. It is omitted when the two are the same.

    ``bytes`` shadows the builtin as an attribute name on purpose: the JSON key
    is ``bytes`` and one name for one thing beats a mapping table.
    """

    guest_name: str
    bytes: int
    sha256: str
    host_name: str | None = None
    content_b64: str | None = None

    def to_json(self) -> dict[str, Any]:
        return compact({
            "guest_name": self.guest_name,
            "host_name": self.host_name if self.host_name is not None else UNSET,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "content_b64": self.content_b64 if self.content_b64 is not None else UNSET,
        })


@dataclass(slots=True)
class StreamOut:
    """``list_output`` / ``punch_output``.

    SPEC.md 5.5: ``printer`` and ``aux_output`` go to files in the sandbox, not
    to ``/dev/null`` and not to empty. Empty produces
    ``Warning: Cannot open printer file '': No such file or directory`` on
    every run; ``/dev/null`` silences that but silently destroys the LST: and
    PUN: byte streams, so a CP/M utility that writes its report to the list
    device returns an empty stdout and looks like a clean no-op.

    ``sha256`` and ``content_b64`` are omitted when absent; SPEC.md 7.1 shows
    ``"list_output":{"bytes":0}`` for an untouched stream.
    """

    bytes: int
    sha256: str | None = None
    content_b64: str | None = None

    def to_json(self) -> dict[str, Any]:
        return compact({
            "bytes": self.bytes,
            "sha256": self.sha256 if self.sha256 is not None else UNSET,
            "content_b64": self.content_b64 if self.content_b64 is not None else UNSET,
        })


@dataclass(slots=True)
class Assertion:
    """One evaluated assertion.

    ``actual`` is present only on failure (SPEC.md 7.1 shows
    ``{"kind":"stdout_contains","value":"23 file(s) extracted","ok":false,
    "actual":"1 file(s) extracted"}`` next to passing entries that omit it).
    :data:`UNSET`, not ``None``, marks it absent, because ``null`` is a
    legitimate actual value.
    """

    kind: AssertionKind | str
    value: Any
    ok: bool
    actual: Any = UNSET

    @classmethod
    def passed(cls, kind: AssertionKind | str, value: Any) -> "Assertion":
        return cls(kind=kind, value=value, ok=True)

    @classmethod
    def failed(
        cls, kind: AssertionKind | str, value: Any, actual: Any = UNSET
    ) -> "Assertion":
        return cls(kind=kind, value=value, ok=False, actual=actual)

    def to_json(self) -> dict[str, Any]:
        return compact({
            "kind": self.kind,
            "value": self.value,
            "ok": self.ok,
            "actual": self.actual,
        })


@dataclass(slots=True)
class Fidelity:
    """SPEC.md 6.3 ``Fidelity``.

    "fidelity.divergences is carried inline per profile, not buried in docs"
    (6.4). For ``cpm-hosted`` it carries, at minimum, the two measured strings
    in SPEC.md 6.4 and 7.1: the missing FCB at 0x5C and the lowercased created
    filenames.
    """

    tier: Tier
    divergences: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"tier": self.tier, "divergences": list(self.divergences)}


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CpmDiagnostics:
    """``x80_cpm_run`` -> ``diagnostics``.

    ``unimplemented_bdos`` comes from cpmemu's ``Unimplemented BDOS function N``
    stderr line and is, per SPEC.md 6.4, "the cheapest possible signal for
    *this package needs a richer CP/M than this one -- rerun under cpm3*".
    """

    unimplemented_bdos: list[int] = field(default_factory=list)
    config_warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "unimplemented_bdos": list(self.unimplemented_bdos),
            "config_warnings": list(self.config_warnings),
        }


@dataclass(slots=True)
class UnimplementedInt21:
    """One entry of ``diagnostics.unimplemented_int21``.

    dosiz prints ``dosiz: unimplemented INT 21h AH=XXh (...) -- returning
    invalid-function, program continues`` **once per distinct AH** and keeps
    going, so one run yields the whole set (SPEC.md 6.4).
    """

    ah: int
    ax: int | None = None
    bx: int | None = None
    cx: int | None = None
    dx: int | None = None

    def to_json(self) -> dict[str, Any]:
        return compact({
            "ah": self.ah,
            "ax": self.ax if self.ax is not None else UNSET,
            "bx": self.bx if self.bx is not None else UNSET,
            "cx": self.cx if self.cx is not None else UNSET,
            "dx": self.dx if self.dx is not None else UNSET,
        })


@dataclass(slots=True)
class PmFault:
    """``diagnostics.pm_fault`` -- a dosiz protected-mode fault."""

    cs: int
    eip: int
    err: int
    regs: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"cs": self.cs, "eip": self.eip, "err": self.err, "regs": dict(self.regs)}


#: SPEC.md 5.5: "Two dosiz stderr lines print on every single run and are
#: filtered by name, with the filtering recorded rather than silently
#: swallowed." Verbatim from evidence/t4/err.txt. The dosiz adapter filters
#: exactly these two strings and lists what it removed in
#: ``diagnostics.stderr_filtered``.
DOSIZ_STDERR_NOISE: tuple[str, ...] = (
    "dosiz: ethernet/slirp backend unavailable; INT 0x60 packet driver will "
    "accept guest calls but RX/TX will no-op.",
    "dosiz: Crynwr pktdrv installed at INT 60h, stub at 0060:0000",
)


@dataclass(slots=True)
class DosDiagnostics:
    """``x80_dos_run`` -> ``diagnostics``."""

    unimplemented_int21: list[UnimplementedInt21] = field(default_factory=list)
    stderr_filtered: list[str] = field(default_factory=list)
    pm_fault: PmFault | None = None

    def to_json(self) -> dict[str, Any]:
        return compact({
            "unimplemented_int21": [u.to_json() for u in self.unimplemented_int21],
            "pm_fault": self.pm_fault.to_json() if self.pm_fault is not None else UNSET,
            "stderr_filtered": list(self.stderr_filtered),
        })


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RunResult:
    """``x80_cpm_run``'s result. **There is no exit_code field.**

    SPEC.md 5.4 Invariant 4, and it is not a style point:

    * CP/M has no exit-status concept.
    * cpmemu ``exit(0)``s on every normal path including its runaway watchdog.
    * romwbw_emu always returns 0.
    * All three of 80un's failure modes exit 0.
    * Measured: a 23-member ARC under ``default_mode = auto`` extracted 1 file,
      truncated it (1437 bytes vs 1664), printed ``Error``, and exited 0 with
      ``Program exit via JMP 0`` on stderr. A 96%-failed run is
      indistinguishable from success at the process level.

    Success is asserted from ``stdout`` plus ``files_out``. Adding a nullable
    ``exit_code`` here re-creates exactly the trap the tool exists to remove,
    so :func:`_assert_no_exit_code_on_cpm` fails the import if anyone does.

    ``pass_`` serializes as ``"pass"``; ``pass`` is a Python keyword.
    """

    pass_: bool
    exit_reason: ExitReason
    wall_ms: int
    stdout: str
    stderr: str
    files_out: list[FileOut] = field(default_factory=list)
    list_output: StreamOut = field(default_factory=lambda: StreamOut(bytes=0))
    punch_output: StreamOut = field(default_factory=lambda: StreamOut(bytes=0))
    assertions: list[Assertion] = field(default_factory=list)
    diagnostics: CpmDiagnostics = field(default_factory=CpmDiagnostics)
    warnings: list[str] = field(default_factory=list)
    fidelity: Fidelity = field(default_factory=lambda: Fidelity(tier=Tier.HOSTED))
    sandbox: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Keys in SPEC.md 6.4 order. ``sandbox`` is ``null`` when the sandbox
        was reaped (``keep_sandbox:false``), never absent."""
        return {
            "pass": self.pass_,
            "exit_reason": self.exit_reason,
            "wall_ms": self.wall_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "files_out": [f.to_json() for f in self.files_out],
            "list_output": self.list_output.to_json(),
            "punch_output": self.punch_output.to_json(),
            "assertions": [a.to_json() for a in self.assertions],
            "diagnostics": self.diagnostics.to_json(),
            "warnings": list(self.warnings),
            "fidelity": self.fidelity.to_json(),
            "sandbox": self.sandbox,
        }


@dataclass(slots=True)
class DosRunResult:
    """``x80_dos_run``'s result. This one **does** carry an exit code.

    dosiz propagates the DOS AH=4Ch AL value; measured, ``DJ_PRINTF.exe``
    returns rc 7. ``exit_code`` is emitted even when ``null``, because
    ``null`` is the freedos answer and means "emu88 has no DOS and therefore no
    ERRORLEVEL", which a missing key would not say.

    ``exit_code_meaning`` exists because rc 1 is ambiguous between "the guest
    exited 1" and "dosiz failed to load the program"; it is decided from
    stderr, not from the number.
    """

    pass_: bool
    exit_code: int | None
    exit_code_meaning: ExitCodeMeaning
    wall_ms: int
    stdout: str
    stderr: str
    files_out: list[FileOut] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    diagnostics: DosDiagnostics = field(default_factory=DosDiagnostics)
    warnings: list[str] = field(default_factory=list)
    fidelity: Fidelity = field(default_factory=lambda: Fidelity(tier=Tier.HOSTED))
    sandbox: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "pass": self.pass_,
            "exit_code": self.exit_code,
            "exit_code_meaning": self.exit_code_meaning,
            "wall_ms": self.wall_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "files_out": [f.to_json() for f in self.files_out],
            "assertions": [a.to_json() for a in self.assertions],
            "diagnostics": self.diagnostics.to_json(),
            "warnings": list(self.warnings),
            "fidelity": self.fidelity.to_json(),
            "sandbox": self.sandbox,
        }


@dataclass(slots=True)
class FileDiff:
    """One entry of ``x80_diff_run`` -> ``diffs``.

    ``first_diff_offset`` is ``null`` when the files differ only in length.
    All five keys are always emitted: this is a fixed record and an agent
    should not have to test for key presence to read it.
    """

    name: str
    guest_bytes: int
    reference_bytes: int
    first_diff_offset: int | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "guest_bytes": self.guest_bytes,
            "reference_bytes": self.reference_bytes,
            "first_diff_offset": self.first_diff_offset,
            "note": self.note,
        }


@dataclass(slots=True)
class DiffResult:
    """``x80_diff_run``'s result.

    ``normalization_applied`` is echoed rather than assumed: SPEC.md 7.2
    measures 23/23 identical under ``["lowercase_names","pad_to_record"]`` and
    2 identical / 21 differing under ``lowercase_names`` alone, so a result
    without its declared normalization is uninterpretable.
    """

    pass_: bool
    normalization_applied: list[Normalization]
    files_compared: int
    identical: int
    differ: int
    only_in_guest: list[str] = field(default_factory=list)
    only_in_reference: list[str] = field(default_factory=list)
    diffs: list[FileDiff] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "pass": self.pass_,
            "normalization_applied": list(self.normalization_applied),
            "files_compared": self.files_compared,
            "identical": self.identical,
            "differ": self.differ,
            "only_in_guest": list(self.only_in_guest),
            "only_in_reference": list(self.only_in_reference),
            "diffs": [d.to_json() for d in self.diffs],
        }


@dataclass(slots=True)
class UnimplementedCall:
    """One entry of ``x80_probe`` -> ``unimplemented``."""

    layer: SyscallLayer | str
    func: int
    name: str
    count: int

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "func": self.func,
            "name": self.name,
            "count": self.count,
        }


@dataclass(slots=True)
class Escalation:
    """A literal next call the agent can make.

    SPEC.md 6.4: "It costs nothing and it is the difference between an agent
    that reruns on cpm3 and one that gives up."
    """

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": dict(self.arguments)}


@dataclass(slots=True)
class ProbeResult:
    """``x80_probe``'s result."""

    ran_on: str
    verdict: ProbeVerdict
    unimplemented: list[UnimplementedCall] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    recommended_profile: str = ""
    escalation: Escalation | None = None

    def to_json(self) -> dict[str, Any]:
        return compact({
            "ran_on": self.ran_on,
            "verdict": self.verdict,
            "unimplemented": [u.to_json() for u in self.unimplemented],
            "evidence": list(self.evidence),
            "recommended_profile": self.recommended_profile,
            "escalation": (
                self.escalation.to_json() if self.escalation is not None else UNSET
            ),
        })


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ImageRequirement:
    """One entry of ``profiles[].images.required``."""

    id: str
    sha256: str
    present: bool

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "sha256": self.sha256, "present": self.present}


@dataclass(slots=True)
class ProfileImages:
    """``profiles[].images``. A wrapper object, because SPEC.md 6.4 spells it
    ``images:{required:[...]}`` and phase 2 adds an ``optional`` sibling."""

    required: list[ImageRequirement] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"required": [i.to_json() for i in self.required]}


@dataclass(slots=True)
class Profile:
    """One entry of ``x80_profiles`` -> ``profiles``.

    ``blocked_by`` carries actionable strings, not booleans. SPEC.md 6.4 gives
    the measured example for ``mpm2`` on a stock macOS install: "mpm2_emu links
    /usr/local/lib/libqkz80.4.dylib which is not installed; set
    DYLD_LIBRARY_PATH or install libqkz80."

    ``backend_version`` and ``binary_path`` are emitted as ``null`` rather than
    omitted when the backend was not found, so a caller can tell "not probed"
    from "probed and absent" by reading ``ready`` and ``blocked_by``.
    """

    id: str
    family: Family
    tier: Tier
    backend: str
    os: str
    caps: list[str] = field(default_factory=list)
    consoles: int = 1
    images: ProfileImages = field(default_factory=ProfileImages)
    fidelity: Fidelity = field(default_factory=lambda: Fidelity(tier=Tier.HOSTED))
    ready: bool = False
    blocked_by: list[str] = field(default_factory=list)
    backend_version: str | None = None
    binary_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Keys in SPEC.md 6.4 order."""
        return {
            "id": self.id,
            "family": self.family,
            "tier": self.tier,
            "backend": self.backend,
            "backend_version": self.backend_version,
            "binary_path": self.binary_path,
            "os": self.os,
            "caps": list(self.caps),
            "consoles": self.consoles,
            "images": self.images.to_json(),
            "fidelity": self.fidelity.to_json(),
            "ready": self.ready,
            "blocked_by": list(self.blocked_by),
        }


@dataclass(slots=True)
class ExternalServerRec:
    """One entry of ``external_servers_recommended``.

    SPEC.md 6.4 points at altairsim for generic CP/M-on-S-100 and at Spice86
    for general DOS. ``for_`` serializes as ``"for"``; ``for`` is a keyword.
    """

    for_: str
    name: str
    url: str
    why: str

    def to_json(self) -> dict[str, Any]:
        return {"for": self.for_, "name": self.name, "url": self.url, "why": self.why}


@dataclass(slots=True)
class ProfilesResult:
    """``x80_profiles``'s result."""

    profiles: list[Profile]
    server_version: str
    protocol_version: str
    external_servers_recommended: list[ExternalServerRec] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "profiles": [p.to_json() for p in self.profiles],
            "server_version": self.server_version,
            "protocol_version": self.protocol_version,
            "external_servers_recommended": [
                e.to_json() for e in self.external_servers_recommended
            ],
        }


# ---------------------------------------------------------------------------
# Backend capabilities
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Caps:
    """What the MBP ``caps`` op returns. SPEC.md 5.3.

    ``ops`` is the full served set, required ops included. Everything above the
    adapter asks ``caps`` rather than testing the backend's class, which is the
    property that lets romwbw_emu move from a pty adapter to a native MBP
    backend when it grows ``--control=PATH`` without anything above the adapter
    changing.

    ``has_exit_code`` is separate from "the process has a return code": every
    process does. It means "this backend's return code says something about the
    guest". cpmemu: false (Invariant 4). dosiz: true (measured rc 7).

    ``has_idle_signal`` means a *real* one, from the backend, not a quiet
    timer. SPEC.md 1.4: never ship a heuristic in its place.
    """

    backend: str
    shape: AdapterShape
    family: Family
    tier: Tier
    ops: frozenset[str]
    cpus: tuple[str, ...] = ()
    consoles: int = 1
    has_exit_code: bool = False
    has_idle_signal: bool = False
    version: str | None = None
    binary_path: str | None = None
    divergences: tuple[str, ...] = ()

    def supports(self, op: str) -> bool:
        return str(op) in self.ops

    def to_json(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "shape": self.shape,
            "family": self.family,
            "tier": self.tier,
            "ops": sorted(self.ops),
            "cpus": list(self.cpus),
            "consoles": self.consoles,
            "has_exit_code": self.has_exit_code,
            "has_idle_signal": self.has_idle_signal,
            "version": self.version,
            "binary_path": self.binary_path,
            "divergences": list(self.divergences),
        }


# ---------------------------------------------------------------------------
# Process execution
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ExecResult:
    """What :mod:`eightymcp.sandbox` returns from one deadlined exec.

    SPEC.md 5.5: "Nothing in the family has a timeout ... Every launch is
    externally deadlined and killed by process group." ``timed_out`` therefore
    means *the supervisor's* deadline fired, and when it does ``rc`` is None
    and ``signal`` is the signal the process group was killed with.

    ``stdout`` and ``stderr`` are bytes. Decoding is the tool layer's job, via
    :func:`decode_guest`, so that the byte-level evidence survives as far as
    possible up the stack.
    """

    argv: list[str]
    cwd: str
    rc: int | None
    signal: int | None
    stdout: bytes
    stderr: bytes
    wall_ms: int
    timed_out: bool = False
    deadline_ms: int = 0

    @property
    def ok(self) -> bool:
        """The process exited normally with status 0. Says nothing about
        whether the *guest* succeeded -- see :class:`RunResult`."""
        return self.rc == 0 and not self.timed_out


# ---------------------------------------------------------------------------
# Import-time invariant checks
# ---------------------------------------------------------------------------

def _assert_no_exit_code_on_cpm() -> None:
    """SPEC.md 5.4 Invariant 4, enforced rather than documented.

    "x80_cpm_run has no exit_code field at all -- not a nullable one, not one
    guarded by if/then."
    """
    names = {f.name for f in fields(RunResult)}
    forbidden = names & {"exit_code", "exit_code_meaning", "rc", "returncode", "status"}
    if forbidden:
        raise AssertionError(
            "RunResult must not carry a process exit status "
            f"(SPEC.md 5.4 Invariant 4); found {sorted(forbidden)}"
        )
    emitted = RunResult(
        pass_=True,
        exit_reason=ExitReason.JMP_0,
        wall_ms=0,
        stdout="",
        stderr="",
    ).to_json()
    if "exit_code" in emitted:
        raise AssertionError("RunResult.to_json() emitted an exit_code key")


def _assert_dos_keeps_exit_code() -> None:
    """The other half of Invariant 4: dosiz is the exception and gets its own
    verb, which does carry exit_code -- including when it is null."""
    emitted = DosRunResult(
        pass_=False,
        exit_code=None,
        exit_code_meaning=ExitCodeMeaning.NONE_AVAILABLE,
        wall_ms=0,
        stdout="",
        stderr="",
    ).to_json()
    if "exit_code" not in emitted or emitted["exit_code"] is not None:
        raise AssertionError("DosRunResult must emit exit_code, null included")


_assert_no_exit_code_on_cpm()
_assert_dos_keeps_exit_code()
