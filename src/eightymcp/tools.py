"""The seven phase-1 tools, wired to the sandbox, the two one-shot backends,
the profile table and the diff engine.

SPEC.md 9, phase 1: ``x80_profiles``, ``x80_cpm_run``, ``x80_dos_run``,
``x80_diff_run``, ``x80_files`` (sandbox routes), ``x80_probe``, ``x80_images``.
Nothing here parses stderr, decides a fidelity divergence or synthesizes a
config; those live in the module that measured them. This file is the wiring
and the four things only the wiring can own:

1. **The assert block.** SPEC.md 6.4 puts ``assert`` in the input schema and
   ``assertions:[{kind,value,ok,actual?}]`` in the output, so the server
   evaluates them and reports each one. ``pass`` is the AND of them.
2. **Sandbox lifetime.** One sandbox per call, reaped in a ``finally`` unless
   ``keep_sandbox:true``, and its path reported either way (null when reaped).
3. **The reported manifest.** ``collect.normalize`` is "applied to the REPORTED
   sha256 and content only, never to the file on disk" (SPEC.md 6.4), which is
   a rule about this layer and not about the sandbox.
4. **Which backend serves which profile**, via ``Prober.require_ready`` -- so an
   absent backend is a structured ``profile_not_ready`` body naming what is
   missing, never a traceback and never a crash (SPEC.md 4.7).

Two decisions this file makes that SPEC.md does not state, both documented at
the point of decision below and both cheap to overrule:

* ``pass`` is the AND of the assertions *and* the run having completed. An
  empty AND is true, so a run that timed out or never loaded its program with
  no assert block would otherwise report ``pass:true``. See
  :func:`_verdict`.
* ``assertions[].actual`` for a failed ``stdout_contains`` is the closest
  line in stdout by difflib ratio, which is what reproduces SPEC.md 7.1's
  ``{"value":"23 file(s) extracted","actual":"1 file(s) extracted"}``. See
  :func:`_nearest_line`.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import __version__
from .backends import cpmemu as cpmemu_mod
from .backends import dosiz as dosiz_mod
from .jsonrpc import log
from .mbp import BootSpec, RunRequest
from .normalize import diff_trees, parse_normalizations, report_bytes
from .profiles import (
    CHEAPEST_PROFILE,
    Config,
    Prober,
    bdos_call,
    classify_probe,
    detect_family,
    int21_call,
    resolve_images,
)
from .sandbox import Collect, Sandbox, sha256_bytes
from .schemas import PHASE1_TOOLS
from .server import CallContext, Server, ToolResult
from .types import (
    Assertion,
    AssertionKind,
    CpmDiagnostics,
    DefaultMode,
    DosDiagnostics,
    DosRunResult,
    Escalation,
    ExitCodeMeaning,
    ExitReason,
    Family,
    FileIn,
    FileOut,
    Fidelity,
    ProbeResult,
    ReturnContent,
    RunResult,
    Since,
    StreamOut,
    ToolError,
    ToolExecutionError,
    Via,
    decode_guest,
)

__all__ = [
    "SANDBOX_MARKER",
    "build_server",
    "register_phase1_tools",
    "x80_profiles",
    "x80_cpm_run",
    "x80_dos_run",
    "x80_diff_run",
    "x80_files",
    "x80_probe",
    "x80_images",
]

#: Written into the root of a sandbox kept with ``keep_sandbox:true``, so that
#: ``x80_files{sandbox:...}`` can tell an 80mcp sandbox from an arbitrary host
#: directory an agent typed by mistake, and can answer questions about it (what
#: profile made it, which names were staged inputs rather than guest output)
#: after the process that made it is gone. It lives in the sandbox *root*,
#: never in ``guest/``, so it can never appear in a file manifest.
SANDBOX_MARKER = ".80mcp-sandbox.json"

#: cpmemu prints this on a successful load (``cpmemu.cc``, matched by
#: ``backends/cpmemu.py``'s ``_BANNER``). x80_probe needs "did the program load
#: at all" as a separate fact from "did it run", because ``classify_probe``
#: returns ``failed_to_load`` for one and a syscall verdict for the other.
_CPMEMU_LOADED = re.compile(r"^Loaded \d+ bytes from ", re.MULTILINE)

#: The same line ``backends/cpmemu.py`` deduplicates. x80_probe wants the count
#: per function, which the deduplicated list has thrown away, so it is counted
#: here from the same stderr rather than changing the adapter's contract.
_CPMEMU_BDOS = re.compile(r"^Unimplemented BDOS function (\d+)$", re.MULTILINE)

#: How much guest output goes into the human-readable ``content`` block before
#: it is elided with a pointer at ``structuredContent``, which always carries
#: all of it.
_TEXT_HEAD = 4096
_TEXT_TAIL = 2048


# ==========================================================================
# 1. Shared wiring
# ==========================================================================

def _prober(config: Config | None = None) -> Prober:
    """A fresh prober per call.

    Measured on this machine: a full 11-profile probe with dynamic-library
    resolution costs 66 ms, and one profile costs less. That is cheap enough
    to pay per call, and paying it per call is what makes ``x80_profiles``
    honest about a backend that was installed after the server started -- MCP
    clients keep a stdio server alive for a whole session.
    """
    return Prober(config)


def _files_in(arguments: Mapping[str, Any], key: str = "files_in") -> list[FileIn]:
    return FileIn.list_from_json(arguments.get(key) or [])


def _collect_of(arguments: Mapping[str, Any]) -> Collect:
    return Collect.from_json(arguments.get("collect"))


def _normalize_of(arguments: Mapping[str, Any]) -> tuple:
    """``collect.normalize``, which :class:`Collect` deliberately ignores."""
    block = arguments.get("collect") or {}
    return parse_normalizations(block.get("normalize"))


def _encode_stdin(text: str | None) -> bytes:
    """Console bytes for the guest.

    latin-1 first: every codepoint under 256 becomes exactly that byte, which
    is what a CP/M or DOS program reading the console expects and what makes
    ``\\x1a`` and the 128-255 range survive. utf-8 only for text latin-1 cannot
    hold, where any byte sequence is a guess and utf-8 is the least surprising
    one.
    """
    if not text:
        return b""
    try:
        return text.encode("latin-1")
    except UnicodeEncodeError:
        return text.encode("utf-8")


def _stdin_warning(data: bytes) -> list[str]:
    """LF-only console input is the quiet way to hang a guest on BDOS 10."""
    if b"\n" in data and b"\r" not in data:
        return [
            "stdin contains LF and no CR: both CP/M's BDOS 10 and DOS's INT 21h "
            "AH=0Ah terminate a console line on CR (0x0D), so a guest reading a "
            "line may never see one. Send \\r, not \\n."
        ]
    return []


def _nearest_line(haystack: str, needle: str) -> Any:
    """The line of ``haystack`` closest to ``needle``, for a failed assertion.

    SPEC.md 7.1's negative case reports
    ``{"kind":"stdout_contains","value":"23 file(s) extracted","ok":false,
    "actual":"1 file(s) extracted"}``. The value the agent needs is the line
    the guest printed *instead*, not the whole transcript, so the closest line
    by difflib ratio is reported when there is a plausible one (0.6, difflib's
    own "close enough" threshold) and a bounded tail of the output when there
    is not.
    """
    lines = [ln.strip() for ln in haystack.splitlines() if ln.strip()]
    if not lines:
        return ""
    best, ratio = "", 0.0
    for line in lines:
        r = SequenceMatcher(None, needle, line).ratio()
        if r > ratio:
            best, ratio = line, r
    if ratio >= 0.6:
        return best
    tail = haystack[-200:]
    return tail if len(haystack) <= 200 else "..." + tail


def _containing_line(haystack: str, needle: str) -> Any:
    for line in haystack.splitlines():
        if needle in line:
            return line.strip()
    return needle


@dataclass(slots=True)
class _Manifest:
    """``files_out``, indexed the two ways an assertion can name a file."""

    files: list[FileOut] = field(default_factory=list)

    def _index(self) -> dict[str, FileOut]:
        out: dict[str, FileOut] = {}
        for f in self.files:
            out.setdefault(f.guest_name.lower(), f)
            if f.host_name:
                out.setdefault(f.host_name.lower(), f)
        return out

    def find(self, name: str) -> FileOut | None:
        """Case-insensitively, against both names.

        SPEC.md 6.4: "host_name is reported separately from guest_name because
        cpmemu lowercases on create, so a caller writing case-sensitive
        assertions against host names will be wrong." An assertion that fails
        only on case would be exactly that trap re-opened one layer up.
        """
        return self._index().get(name.lower())

    def names(self) -> list[str]:
        return [f.guest_name for f in self.files]


def _assertions(
    block: Mapping[str, Any] | None,
    *,
    stdout: str,
    manifest: _Manifest,
    unimplemented_key: str,
    unimplemented: Sequence[Any],
) -> list[Assertion]:
    """Evaluate one ``assert`` block. Order follows the schema's own key order.

    A boolean assertion set to ``false`` produces no entry: the schema fills
    ``no_unimplemented_bdos`` with its ``false`` default whenever an assert
    block is present at all, and reporting "you asserted nothing, and nothing
    was what happened" as a passing assertion would pad every result.
    """
    out: list[Assertion] = []
    if not block:
        return out

    for value in block.get("stdout_contains", ()) or ():
        ok = value in stdout
        out.append(
            Assertion.passed(AssertionKind.STDOUT_CONTAINS, value)
            if ok
            else Assertion.failed(
                AssertionKind.STDOUT_CONTAINS, value, _nearest_line(stdout, value)
            )
        )
    for value in block.get("stdout_not_contains", ()) or ():
        ok = value not in stdout
        out.append(
            Assertion.passed(AssertionKind.STDOUT_NOT_CONTAINS, value)
            if ok
            else Assertion.failed(
                AssertionKind.STDOUT_NOT_CONTAINS, value, _containing_line(stdout, value)
            )
        )
    for value in block.get("files_created", ()) or ():
        hit = manifest.find(value)
        out.append(
            Assertion.passed(AssertionKind.FILES_CREATED, value)
            if hit is not None
            else Assertion.failed(
                AssertionKind.FILES_CREATED, value, manifest.names()
            )
        )
    for name, want in (block.get("file_sha256") or {}).items():
        hit = manifest.find(name)
        actual = hit.sha256 if hit is not None else None
        out.append(
            Assertion.passed(AssertionKind.FILE_SHA256, {name: want})
            if actual == want
            else Assertion.failed(AssertionKind.FILE_SHA256, {name: want}, actual)
        )
    if block.get(unimplemented_key):
        kind = (
            AssertionKind.NO_UNIMPLEMENTED_BDOS
            if unimplemented_key == "no_unimplemented_bdos"
            else AssertionKind.NO_UNIMPLEMENTED_INT21
        )
        out.append(
            Assertion.passed(kind, True)
            if not unimplemented
            else Assertion.failed(
                kind,
                True,
                [u if isinstance(u, int) else u.to_json() for u in unimplemented],
            )
        )
    return out


#: Three of SPEC.md 6.4's nine ``exit_reason`` values: the ones that say the
#: *supervisor or the emulator* ended the run. The other six -- ``jmp_0``,
#: ``bdos_0``, ``wboot``, ``instruction_limit``, ``eof_giveup``, ``ctrl_c`` --
#: are the guest reaching an end of its own, transcript and all, so an
#: assertion against them means something and they do not veto.
_INCOMPLETE = (
    ExitReason.TIMEOUT,
    ExitReason.BOOT_TIMEOUT,
    ExitReason.EMULATOR_ERROR,
)


def _verdict(
    assertions: Sequence[Assertion],
    *,
    exit_reason: ExitReason | None = None,
    warnings: list[str] | None = None,
) -> bool:
    """``pass``: the AND of the assertions, and of the run having happened.

    SPEC.md 6.4 defines ``pass`` as the verdict over the assert block, and a
    guest that correctly reported its own failure can still be ``pass:true``
    -- that is the whole point of asserting on stdout rather than on an exit
    code (Invariant 4). But an empty AND is true, so a call with no assert
    block whose program never loaded, or whose deadline fired, would report
    success for a run that did not happen. Those three exit reasons are the
    server's own report that there was nothing to assert about, so they veto,
    and the veto says so in ``warnings`` rather than silently.
    """
    ok = all(a.ok for a in assertions)
    if ok and exit_reason in _INCOMPLETE:
        if warnings is not None:
            warnings.append(
                f"pass is false because exit_reason is {exit_reason}: the run "
                f"did not complete, so the assertions (if any) were evaluated "
                f"against a partial transcript."
            )
        return False
    return ok


def _apply_reported_normalization(
    files: list[FileOut],
    guest_dir: Path,
    members: Sequence[Any],
    warnings: list[str],
) -> None:
    """``collect.normalize``: reported sha256/content only, never the disk.

    SPEC.md 6.4 scopes this to the report, so the files stay exactly as the
    guest wrote them and only the three numbers an assertion can be written
    against move. ``bytes`` moves with ``sha256`` deliberately: a padded hash
    beside an unpadded length would be two different files' worth of evidence
    in one record. When they disagree the on-disk size is named in a warning.
    """
    if not members:
        return
    for f in files:
        host = f.host_name or f.guest_name
        path = guest_dir / host
        try:
            raw = path.read_bytes()
        except OSError as exc:
            warnings.append(f"collect.normalize: cannot re-read {host}: {exc}")
            continue
        rep = report_bytes(raw, members)
        if rep.changed:
            warnings.append(
                f"collect.normalize changed the reported size of "
                f"{f.guest_name}: {rep.raw_bytes} bytes on disk, {rep.bytes} "
                f"reported (normalize={[str(m) for m in members]})"
            )
        f.bytes = rep.bytes
        f.sha256 = rep.sha256
        if f.content_b64 is not None:
            f.content_b64 = base64.b64encode(rep.data).decode("ascii")


def _stream(path: Path | None, sandbox: Sandbox, opts: Collect) -> StreamOut:
    """One of ``list_output`` / ``punch_output``.

    Appendix B item 2: the devices are routed to real files in the sandbox, so
    an absent path means the backend never created one, which is a zero-byte
    stream and not a missing key.
    """
    if path is None:
        return StreamOut(bytes=0)
    return sandbox.stream_out(Path(path), opts)


def _record_sandbox(
    sandbox: Sandbox,
    *,
    tool: str,
    profile: str,
    backend: str,
    family: Family,
    staged: Sequence[str],
) -> None:
    """Leave enough behind that ``x80_files{sandbox:...}`` can answer later.

    Only written when the tree survives the call. SPEC.md 4.3 gives a sandbox
    24 hours after the machine that made it is gone; by then this process is
    usually gone too, so what the next call knows about the tree is what is
    written in it.

    Two things are recorded, and the difference between them is the whole
    point:

    * ``created`` -- what the *run* wrote, taken from the sandbox manifest and
      **not** from ``files_out``. A caller's ``collect.exclude`` shapes the
      report, not the history: excluding ``*.INS`` from a result must not make
      a later ``x80_files{since:"start"}`` claim the guest never wrote them.
    * ``state`` -- ``relpath -> [size, mtime_ns]`` for the whole guest
      directory as it stood when the run ended. That is the baseline
      ``modified`` is measured against, and it is what lets a file that
      appeared afterwards (an ``x80_files{op:"to_guest"}``) be told apart from
      one the guest wrote.
    """
    if not sandbox.keep:
        return
    now = time.time_ns()
    created: list[str] = []
    for path in sandbox.created_since_snapshot():
        try:
            created.append(sandbox.relative(path))
        except ValueError:                        # not under guest_dir
            continue
    state: dict[str, list[int]] = {}
    for path in _walk_guest(sandbox.guest_dir):
        st = path.stat()
        state[path.relative_to(sandbox.guest_dir).as_posix()] = [st.st_size, st.st_mtime_ns]
    body = {
        "schema": 1,
        "server_version": __version__,
        "tool": tool,
        "profile": profile,
        "backend": backend,
        "family": str(family),
        "created_ns": now,
        "last_call_ns": now,
        "staged": list(staged),
        "created": created,
        "state": state,
    }
    try:
        sandbox.write_text(SANDBOX_MARKER, json.dumps(body, indent=2) + "\n")
    except Exception as exc:                      # never lose a result over this
        log(f"could not record {SANDBOX_MARKER} in {sandbox.root}: {exc}")


def _elide(text: str) -> str:
    if len(text) <= _TEXT_HEAD + _TEXT_TAIL:
        return text
    return (
        text[:_TEXT_HEAD]
        + f"\n...[{len(text) - _TEXT_HEAD - _TEXT_TAIL} bytes elided; "
          f"structuredContent carries all {len(text)}]...\n"
        + text[-_TEXT_TAIL:]
    )


def _assertion_lines(assertions: Sequence[Assertion]) -> list[str]:
    out = []
    for a in assertions:
        mark = "ok" if a.ok else "FAIL"
        line = f"  {mark}  {a.kind} {json.dumps(a.value, ensure_ascii=False)}"
        if not a.ok and a.actual is not None and str(a.actual) != "UNSET":
            line += f"  actual={json.dumps(a.actual, ensure_ascii=False)}"
        out.append(line)
    return out


# ==========================================================================
# 2. x80_profiles
# ==========================================================================

def x80_profiles(ctx: CallContext) -> ToolResult:
    """SPEC.md 6.4: the profile table, probed. Call this first."""
    a = ctx.arguments
    result = _prober().result(
        family=a.get("family", "all"),
        profile=a.get("profile"),
        only_ready=bool(a.get("only_ready", False)),
        probe=bool(a.get("probe", True)),
        # A legacy connection negotiated something older than 2026-07-28 and
        # the result must say so, or an agent reading protocol_version learns
        # the server's preference instead of this connection's fact.
        protocol_version=str(ctx.connection.revision),
    )
    body = result.to_json()

    lines = [f"{len(body['profiles'])} profile(s); server {body['server_version']}, "
             f"protocol {body['protocol_version']}"]
    for p in body["profiles"]:
        state = "ready" if p["ready"] else "BLOCKED"
        lines.append(
            f"  {p['id']:<12} {p['family']:<4} {p['tier']:<8} {p['backend']:<11} {state}"
        )
        for b in p["blocked_by"]:
            lines.append(f"      blocked_by: {b}")
    return ToolResult.ok(body, text="\n".join(lines))


# ==========================================================================
# 3. x80_cpm_run -- THE BATCH VERB
# ==========================================================================

def _cpm_backend(prober: Prober, profile) -> cpmemu_mod.CpmemuBackend:
    probe = prober.backend(profile.backend)
    return cpmemu_mod.CpmemuBackend(probe.path or profile.binary_path)


def x80_cpm_run(ctx: CallContext) -> ToolResult:
    """SPEC.md 6.4 / 7.1. One CP/M package, one sandbox, no exit code."""
    a = ctx.arguments
    prober = _prober()
    profile = prober.require_ready(a["profile"])
    backend = _cpm_backend(prober, profile)
    collect = _collect_of(a)
    members = _normalize_of(a)
    files_in = _files_in(a)
    stdin = _encode_stdin(a.get("stdin"))
    warnings: list[str] = _stdin_warning(stdin)

    sandbox = Sandbox(keep=bool(a.get("keep_sandbox", False)))
    try:
        boot = backend.boot(
            BootSpec(
                sandbox=sandbox,
                profile=profile.id,
                cpu=a.get("cpu"),
                boot_timeout_ms=int(a.get("boot_timeout_ms", 30000)),
                program=a["program"],
                args=list(a.get("args", [])),
                default_mode=DefaultMode(a.get("default_mode", "binary")),
                eol_convert=bool(a.get("eol_convert", False)),
            )
        )
        warnings.extend(boot.warnings)

        outcome = backend.run(
            RunRequest(
                sandbox=sandbox,
                program=a["program"],
                args=list(a.get("args", [])),
                stdin=stdin,
                timeout_ms=int(a.get("timeout_ms", 10000)),
                cpu=a.get("cpu"),
                default_mode=DefaultMode(a.get("default_mode", "binary")),
                eol_convert=bool(a.get("eol_convert", False)),
                files_in=files_in,
                env=dict(prober.backend(profile.backend).env),
                boot_timeout_ms=int(a.get("boot_timeout_ms", 30000)),
            )
        )
        warnings.extend(outcome.warnings)

        files_out = sandbox.collect(collect, guest_name=cpmemu_mod.guest_name_for)
        _apply_reported_normalization(files_out, sandbox.guest_dir, members, warnings)
        manifest = _Manifest(files_out)

        stdout = decode_guest(outcome.stdout)
        assertions = _assertions(
            a.get("assert"),
            stdout=stdout,
            manifest=manifest,
            unimplemented_key="no_unimplemented_bdos",
            unimplemented=outcome.unimplemented_bdos,
        )
        result = RunResult(
            pass_=_verdict(assertions, exit_reason=outcome.exit_reason, warnings=warnings),
            exit_reason=outcome.exit_reason,
            wall_ms=outcome.wall_ms,
            stdout=stdout,
            stderr=decode_guest(outcome.stderr),
            files_out=files_out,
            list_output=_stream(outcome.list_output_path, sandbox, collect),
            punch_output=_stream(outcome.punch_output_path, sandbox, collect),
            assertions=assertions,
            diagnostics=CpmDiagnostics(
                unimplemented_bdos=list(outcome.unimplemented_bdos),
                config_warnings=list(outcome.config_warnings),
            ),
            warnings=warnings,
            fidelity=Fidelity(
                tier=profile.fidelity.tier,
                divergences=list(profile.fidelity.divergences),
            ),
            sandbox=sandbox.reported_path,
        )
        _record_sandbox(
            sandbox,
            tool="x80_cpm_run",
            profile=profile.id,
            backend=profile.backend,
            family=profile.family,
            staged=[f.guest_name for f in files_in],
        )
        return ToolResult.ok(result.to_json(), text=_cpm_text(result))
    finally:
        sandbox.cleanup()


def _cpm_text(r: RunResult) -> str:
    head = (
        f"pass: {str(r.pass_).lower()}   exit_reason: {r.exit_reason}   "
        f"wall_ms: {r.wall_ms}   files_out: {len(r.files_out)}"
    )
    lines = [head]
    if r.assertions:
        lines.append("assertions:")
        lines.extend(_assertion_lines(r.assertions))
    if r.diagnostics.unimplemented_bdos:
        lines.append(f"unimplemented BDOS: {r.diagnostics.unimplemented_bdos}")
    if r.list_output.bytes or r.punch_output.bytes:
        lines.append(
            f"list_output: {r.list_output.bytes} bytes   "
            f"punch_output: {r.punch_output.bytes} bytes"
        )
    for w in r.warnings:
        lines.append(f"warning: {w}")
    lines.append("--- stdout ---")
    lines.append(_elide(r.stdout))
    lines.append("--- stderr ---")
    lines.append(_elide(r.stderr))
    return "\n".join(lines)


# ==========================================================================
# 4. x80_dos_run
# ==========================================================================

def _dos_backend(prober: Prober, profile) -> dosiz_mod.DosizBackend:
    probe = prober.backend(profile.backend)
    return dosiz_mod.DosizBackend(probe.path or profile.binary_path)


def x80_dos_run(ctx: CallContext) -> ToolResult:
    """SPEC.md 6.4 / 7.5. Like x80_cpm_run, but with a real exit code."""
    a = ctx.arguments
    prober = _prober()
    profile = prober.require_ready(a["profile"])
    backend = _dos_backend(prober, profile)
    collect = _collect_of(a)
    files_in = _files_in(a)
    stdin = _encode_stdin(a.get("stdin"))
    warnings: list[str] = _stdin_warning(stdin)

    sandbox = Sandbox(keep=bool(a.get("keep_sandbox", False)))
    try:
        boot = backend.boot(
            BootSpec(
                sandbox=sandbox,
                profile=profile.id,
                boot_timeout_ms=int(a.get("boot_timeout_ms", 30000)),
                program=a["program"],
                args=list(a.get("args", [])),
            )
        )
        warnings.extend(boot.warnings)

        outcome = backend.run(
            RunRequest(
                sandbox=sandbox,
                program=a["program"],
                args=list(a.get("args", [])),
                stdin=stdin,
                timeout_ms=int(a.get("timeout_ms", 20000)),
                files_in=files_in,
                env={**prober.backend(profile.backend).env, **(a.get("env") or {})},
                boot_timeout_ms=int(a.get("boot_timeout_ms", 30000)),
            )
        )
        warnings.extend(outcome.warnings)

        files_out = sandbox.collect(collect)
        manifest = _Manifest(files_out)

        stdout = decode_guest(outcome.stdout)
        # The two Crynwr/slirp lines print on every single dosiz run (SPEC.md
        # 5.5); they are removed by name and the removal is reported in
        # diagnostics.stderr_filtered rather than silently swallowed.
        stderr, _removed = dosiz_mod.filter_noise(decode_guest(outcome.stderr))

        assertions = _assertions(
            a.get("assert"),
            stdout=stdout,
            manifest=manifest,
            unimplemented_key="no_unimplemented_int21",
            unimplemented=outcome.unimplemented_int21,
        )
        if "expect_exit_code" in a:
            want = a["expect_exit_code"]
            got = outcome.exit_code
            assertions.append(
                Assertion.passed(AssertionKind.EXPECT_EXIT_CODE, want)
                if got == want
                else Assertion.failed(AssertionKind.EXPECT_EXIT_CODE, want, got)
            )

        result = DosRunResult(
            pass_=_verdict(assertions, exit_reason=outcome.exit_reason, warnings=warnings),
            exit_code=outcome.exit_code,
            exit_code_meaning=outcome.exit_code_meaning or ExitCodeMeaning.NONE_AVAILABLE,
            wall_ms=outcome.wall_ms,
            stdout=stdout,
            stderr=stderr,
            files_out=files_out,
            assertions=assertions,
            diagnostics=DosDiagnostics(
                unimplemented_int21=list(outcome.unimplemented_int21),
                stderr_filtered=list(outcome.stderr_filtered),
                pm_fault=outcome.pm_fault,
            ),
            warnings=warnings,
            fidelity=Fidelity(
                tier=profile.fidelity.tier,
                divergences=list(profile.fidelity.divergences),
            ),
            sandbox=sandbox.reported_path,
        )
        _record_sandbox(
            sandbox,
            tool="x80_dos_run",
            profile=profile.id,
            backend=profile.backend,
            family=profile.family,
            staged=[f.guest_name for f in files_in],
        )
        return ToolResult.ok(result.to_json(), text=_dos_text(result))
    finally:
        sandbox.cleanup()


def _dos_text(r: DosRunResult) -> str:
    lines = [
        f"pass: {str(r.pass_).lower()}   exit_code: {r.exit_code} "
        f"({r.exit_code_meaning})   wall_ms: {r.wall_ms}   "
        f"files_out: {len(r.files_out)}"
    ]
    if r.assertions:
        lines.append("assertions:")
        lines.extend(_assertion_lines(r.assertions))
    for u in r.diagnostics.unimplemented_int21:
        lines.append(f"unimplemented INT 21h AH={u.ah:02X}h")
    if r.diagnostics.pm_fault is not None:
        f = r.diagnostics.pm_fault
        lines.append(f"pm_fault cs={f.cs:#06x} eip={f.eip:#010x} err={f.err:#x}")
    for w in r.warnings:
        lines.append(f"warning: {w}")
    lines.append("--- stdout ---")
    lines.append(_elide(r.stdout))
    if r.stderr.strip():
        lines.append("--- stderr (filtered) ---")
        lines.append(_elide(r.stderr))
    return "\n".join(lines)


# ==========================================================================
# 5. x80_diff_run
# ==========================================================================

def _reference_argv(
    argv: Sequence[str], inputs: Sequence[Path], outdir: Path, warnings: list[str]
) -> list[str]:
    """Substitute ``{input}`` and ``{outdir}`` (SPEC.md 6.4).

    A token that is exactly ``{input}`` expands to every input, in order, so a
    reference taking a list of files needs no per-file loop. A token that only
    *contains* it takes the first input, which is unambiguous for the one-input
    case SPEC.md 7.2 measures and warned about otherwise.
    """
    out: list[str] = []
    for token in argv:
        if token == "{input}":
            out.extend(str(p) for p in inputs)
            continue
        if "{input}" in token and len(inputs) > 1:
            warnings.append(
                f"reference.argv token {token!r} embeds {{input}} and there are "
                f"{len(inputs)} inputs; only the first was substituted. A token "
                f"that is exactly \"{{input}}\" expands to all of them."
            )
        out.append(
            token.replace("{input}", str(inputs[0]) if inputs else "")
                 .replace("{outdir}", str(outdir))
        )
    return out


def x80_diff_run(ctx: CallContext) -> ToolResult:
    """SPEC.md 6.4 / 7.2. The same algorithm as a guest .COM and as host code."""
    a = ctx.arguments
    guest = a["guest"]
    reference = a["reference"]
    inputs = FileIn.list_from_json(a["inputs"])
    members = parse_normalizations(a.get("normalize"))
    deadline_ms = int(a.get("timeout_ms", 60000))

    if not any("{outdir}" in token for token in reference["argv"]):
        raise ToolExecutionError(
            ToolError.bad_argument(
                "reference.argv",
                "no {outdir} token: the server cannot know where the reference "
                "wrote its output, so there would be nothing to compare against",
                argv=list(reference["argv"]),
                hint='e.g. ["python3","-m","un80.cli","{input}","-o","{outdir}"]',
            )
        )

    prober = _prober()
    profile = prober.require_ready(guest["profile"])
    if profile.backend != cpmemu_mod.BACKEND_NAME:
        raise ToolExecutionError(
            ToolError.unsupported(
                "diff_run",
                profile.backend,
                f"x80_diff_run's guest half runs on the one-shot adapter; "
                f"{profile.id} is served by {profile.backend}, whose adapter "
                f"ships in a later phase (SPEC.md 9)",
                alternative_profile="cpm-hosted",
            )
        )
    backend = _cpm_backend(prober, profile)
    warnings: list[str] = []

    guest_sb = Sandbox()
    # A second tree, so the reference's inputs and outputs cannot land in the
    # directory the guest is being judged on, and so the reference gets the
    # same deadline and the same process-group kill as the guest.
    ref_sb = Sandbox()
    started = time.monotonic()
    try:
        backend.boot(BootSpec(sandbox=guest_sb, profile=profile.id, program=guest["program"]))
        outcome = backend.run(
            RunRequest(
                sandbox=guest_sb,
                program=guest["program"],
                args=list(guest.get("args", [])),
                stdin=_encode_stdin(guest.get("stdin")),
                timeout_ms=deadline_ms,
                cpu=guest.get("cpu"),
                default_mode=DefaultMode(guest.get("default_mode", "binary")),
                eol_convert=bool(guest.get("eol_convert", False)),
                files_in=inputs,
                env=dict(prober.backend(profile.backend).env),
            )
        )
        warnings.extend(outcome.warnings)
        if outcome.timed_out:
            raise ToolExecutionError(
                ToolError.timeout(
                    deadline_ms=deadline_ms,
                    phase="guest",
                    partial={
                        "stdout": decode_guest(outcome.stdout),
                        "exit_reason": str(outcome.exit_reason),
                    },
                )
            )

        staged = ref_sb.stage_all(inputs)
        outdir = ref_sb.root / "out"
        outdir.mkdir(mode=0o700, exist_ok=True)
        argv = _reference_argv(reference["argv"], staged, outdir, warnings)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        remaining = deadline_ms - elapsed_ms
        if remaining < 250:
            raise ToolExecutionError(
                ToolError.timeout(
                    deadline_ms=deadline_ms,
                    phase="reference",
                    partial={"guest_wall_ms": outcome.wall_ms},
                )
            )
        ref = ref_sb.exec(
            argv,
            cwd=Path(reference["cwd"]).expanduser() if reference.get("cwd") else None,
            env=reference.get("env") or {},
            timeout_ms=remaining,
        )
        if not ref.ok:
            raise ToolExecutionError(
                ToolError(
                    "reference_failed",
                    {
                        "argv": argv,
                        "cwd": ref.cwd,
                        "rc": ref.rc,
                        "signal": ref.signal,
                        "timed_out": ref.timed_out,
                        "stderr": decode_guest(ref.stderr)[-4000:],
                        "hint": (
                            "the host reference command did not exit 0, so there "
                            "is no reference output to compare against; the guest "
                            "half ran and its files are in the reaped sandbox"
                        ),
                    },
                )
            )

        # SPEC.md 7.2 is 46 "Only in" lines against 23 members precisely
        # because the staged input is still sitting in the guest directory;
        # excluding it by name is what makes files_compared 23 and not 24.
        result = diff_trees(
            guest_sb.guest_dir,
            outdir,
            normalize=members,
            compare=a.get("compare", "bytes"),
            exclude=[f.guest_name for f in inputs],
        )
        body = result.to_json()
        return ToolResult.ok(body, text=_diff_text(body, warnings, ref.wall_ms, outcome.wall_ms))
    finally:
        guest_sb.cleanup()
        ref_sb.cleanup()


def _diff_text(body: Mapping[str, Any], warnings: Sequence[str], ref_ms: int, guest_ms: int) -> str:
    lines = [
        f"pass: {str(body['pass']).lower()}   files_compared: {body['files_compared']}   "
        f"identical: {body['identical']}   differ: {body['differ']}",
        f"normalization_applied: {body['normalization_applied']}",
        f"guest {guest_ms} ms, reference {ref_ms} ms",
    ]
    if body["only_in_guest"]:
        lines.append(f"only_in_guest ({len(body['only_in_guest'])}): {body['only_in_guest'][:20]}")
    if body["only_in_reference"]:
        lines.append(
            f"only_in_reference ({len(body['only_in_reference'])}): {body['only_in_reference'][:20]}"
        )
    for d in body["diffs"][:20]:
        lines.append(
            f"  {d['name']}: guest {d['guest_bytes']} vs reference "
            f"{d['reference_bytes']}, first_diff_offset {d['first_diff_offset']}"
        )
        if d.get("note"):
            lines.append(f"      {d['note']}")
    for w in warnings:
        lines.append(f"warning: {w}")
    return "\n".join(lines)


# ==========================================================================
# 6. x80_files -- sandbox routes only in phase 1
# ==========================================================================

@dataclass(slots=True)
class _SandboxRef:
    """A kept sandbox tree, plus what the verb that made it recorded."""

    sandbox: Sandbox
    meta: dict[str, Any]

    @property
    def family(self) -> Family:
        try:
            return Family(self.meta.get("family", "z80"))
        except ValueError:
            return Family.Z80

    @property
    def drives(self) -> str:
        # SPEC.md 6.4: "CP/M profiles accept A-P; DOS profiles accept C-Z."
        return "ABCDEFGHIJKLMNOP" if self.family is Family.Z80 else "CDEFGHIJKLMNOPQRSTUVWXYZ"

    @property
    def guest_case(self) -> Callable[[str], str]:
        return cpmemu_mod.guest_name_for if self.family is Family.Z80 else (lambda s: s)


def _open_sandbox(path: str) -> _SandboxRef:
    """Adopt a sandbox this server made, or refuse with the reason.

    The marker file is the check. ``x80_files`` writes into whatever it is
    given, so "given" has to mean "a tree a batch verb produced", not "any
    directory on this host that happens to be named in an argument".
    """
    root = Path(path).expanduser().resolve()
    marker = root / SANDBOX_MARKER
    if not marker.is_file():
        raise ToolExecutionError(
            ToolError.bad_argument(
                "sandbox",
                f"not an 80mcp sandbox: no {SANDBOX_MARKER} in it. This op "
                f"writes into and reads out of whatever it is given, so "
                f"\"given\" has to mean a tree a batch verb produced",
                sandbox=str(root),
                exists=root.is_dir(),
                hint=(
                    "sandbox paths come from a batch verb called with "
                    "keep_sandbox:true, which reports the path in its result's "
                    "`sandbox` field; a reaped sandbox reports null"
                ),
            )
        )
    meta: dict[str, Any] = {}
    try:
        meta = json.loads(marker.read_text())
    except (OSError, ValueError) as exc:
        # The tree is still a sandbox; only the bookkeeping is unreadable, and
        # losing "which files were staged" is not worth refusing to list it.
        log(f"{marker}: unreadable ({exc}); treating as an unlabelled sandbox")
    return _SandboxRef(Sandbox.adopt(root), meta)


def _as_int(value: Any, default: int = 0) -> int:
    """A marker field that should be an integer, from a file on disk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _touch_last_call(ref: _SandboxRef) -> int:
    """Read ``last_call_ns``, then move it to now. ``since:"last_call"`` is a
    question about the previous call, so the answer has to be read before the
    bookkeeping for this one."""
    previous = _as_int(ref.meta.get("last_call_ns", ref.meta.get("created_ns", 0)))
    ref.meta["last_call_ns"] = time.time_ns()
    try:
        ref.sandbox.write_text(SANDBOX_MARKER, json.dumps(ref.meta, indent=2) + "\n")
    except Exception as exc:
        log(f"could not update {SANDBOX_MARKER}: {exc}")
    return previous


def _walk_guest(guest_dir: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(guest_dir):
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            out.append(p)
    return sorted(out)


def _name_matches(patterns: Sequence[str], relative: str, guest_name: str) -> bool:
    if not patterns:
        return True
    for pat in patterns:
        low = pat.lower()
        if (
            fnmatch.fnmatch(relative.lower(), low)
            or fnmatch.fnmatch(guest_name.lower(), low)
            or fnmatch.fnmatch(Path(relative).name.lower(), low)
        ):
            return True
    return False


#: SPEC.md 4.5: a 10-minute ``(handle, tool, idempotency_key) -> result``
#: cache, deduping only when a key is explicitly supplied. The sandbox path
#: stands in for the handle on the phase-1 routes.
_IDEMPOTENCY_TTL_S = 600.0
_idempotency: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _idempotent(key_parts: tuple[str, str, str]) -> dict[str, Any] | None:
    now = time.monotonic()
    for k, (stamp, _v) in list(_idempotency.items()):
        if now - stamp > _IDEMPOTENCY_TTL_S:
            del _idempotency[k]
    hit = _idempotency.get(key_parts)
    return hit[1] if hit else None


def _remember(key_parts: tuple[str, str, str], body: dict[str, Any]) -> None:
    _idempotency[key_parts] = (time.monotonic(), body)


def x80_files(ctx: CallContext) -> ToolResult:
    """SPEC.md 6.4. Sandbox routes; ``handle`` is phase 2, ``image`` phase 4."""
    a = ctx.arguments
    op = a["op"]

    if a.get("handle") and a.get("sandbox"):
        raise ToolExecutionError(
            ToolError.bad_argument(
                "handle",
                "`handle` and `sandbox` are mutually exclusive",
                handle=a["handle"],
                sandbox=a["sandbox"],
            )
        )
    if a.get("handle"):
        raise ToolExecutionError(
            ToolError.unsupported(
                f"files:{op}",
                "80mcp",
                "a `handle` names a live machine, which arrives with x80_open in "
                "phase 2 (SPEC.md 6.5). Phase 1 serves the sandbox route: call a "
                "batch verb with keep_sandbox:true and pass the `sandbox` it "
                "reports",
                escalation=Escalation(
                    tool="x80_cpm_run",
                    arguments={"profile": "cpm-hosted", "keep_sandbox": True},
                ),
            )
        )
    if op in ("handles", "resolve"):
        raise ToolExecutionError(
            ToolError.unsupported(
                f"files:{op}",
                "cpmemu",
                "SPEC.md 6.4: `handles` and `resolve` are phase 5. Both read "
                "CPMEmulator::open_files (src/cpmemu.cc:346), which is declared "
                "inside a 3429-line .cc with no header, so nothing outside the "
                "process can reach it until the backend speaks MBP on fd 3",
                escalation=Escalation(
                    tool="x80_files", arguments={"op": "list", "sandbox": a.get("sandbox", "")}
                ),
            )
        )
    if not a.get("sandbox"):
        raise ToolExecutionError(
            ToolError.bad_argument(
                "sandbox",
                "required on the phase-1 routes: this op needs either a `handle` "
                "(phase 2) or a `sandbox` path",
                hint="call a batch verb with keep_sandbox:true first",
            )
        )

    via = Via(a.get("via", "auto"))
    if via in (Via.HOSTFILE, Via.IMAGE):
        raise ToolExecutionError(
            ToolError.unsupported(
                f"files:{op}",
                "80mcp",
                f"via {via} needs a live machine: the hostfile route is HBIOS "
                f"0xE1-0xEA via R8/W8 (phase 4) and the image route is cpm_disk.py "
                f"against a stopped machine (phase 2). The sandbox route is the "
                f"one that ships today (SPEC.md 6.1)",
            )
        )

    ref = _open_sandbox(a["sandbox"])
    if a.get("drive"):
        letter = a["drive"]
        if letter not in ref.drives:
            raise ToolExecutionError(
                ToolError.bad_argument(
                    "drive",
                    f"profile {ref.meta.get('profile', '?')} has no drive {letter}:",
                    profile=ref.meta.get("profile"),
                    drives=[f"{c}:" for c in ref.drives],
                )
            )

    if op == "list":
        body = _files_list(ref, a)
    elif op == "to_guest":
        key = a.get("idempotency_key")
        parts = ("x80_files:to_guest", str(ref.sandbox.root), str(key))
        cached = _idempotent(parts) if key else None
        if cached is not None:
            body = cached
        else:
            body = _files_to_guest(ref, a)
            if key:
                _remember(parts, body)
    elif op == "from_guest":
        body = _files_from_guest(ref, a)
    else:                                        # unreachable: the enum is closed
        raise ToolExecutionError(
            ToolError.bad_argument("op", "unknown op", given=op)
        )
    return ToolResult.ok(body, text=json.dumps(body, indent=2)[:8000])


def _drive_rows(ref: _SandboxRef) -> list[dict[str, Any]]:
    """The one drive a hosted profile has: the sandbox guest directory.

    cpmemu maps its ``cd`` directory to the guest's current drive and dosiz
    builds ``C:`` out of the host path (bridge.cc:7109-7122), so the honest
    answer for the sandbox route is one letter backed by one directory, not
    the sixteen a real CP/M has.
    """
    letter = "A" if ref.family is Family.Z80 else "C"
    return [{"letter": letter, "backing": str(ref.sandbox.guest_dir), "writable": True}]


def _files_list(ref: _SandboxRef, a: Mapping[str, Any]) -> dict[str, Any]:
    since = Since(a.get("since", "never"))
    previous_ns = _touch_last_call(ref)
    # The marker is written by this server, but the argument names a directory
    # the caller chose, so every field out of it is treated as hostile input.
    # Measured before this guard: a marker with `"created": 5` or
    # `"state": "x"` came back as isError internal_error with a traceback on
    # stderr, which is the wrong channel for "your sandbox file is corrupt".
    created_raw = ref.meta.get("created")
    created_names = {
        str(n).lower() for n in created_raw if isinstance(n, (str, int))
    } if isinstance(created_raw, list) else set()
    state_raw = ref.meta.get("state")
    baseline: dict[str, list[int]] = {}
    if isinstance(state_raw, dict):
        for k, v in state_raw.items():
            if isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) for x in v):
                baseline[str(k).lower()] = v
    want_hash = bool(a.get("hash", False))
    patterns = list(a.get("names") or [])

    rows: list[dict[str, Any]] = []
    for path in _walk_guest(ref.sandbox.guest_dir):
        rel = path.relative_to(ref.sandbox.guest_dir).as_posix()
        guest_name = ref.guest_case(rel)
        if not _name_matches(patterns, rel, guest_name):
            continue
        st = path.stat()
        was = baseline.get(rel.lower())
        # Created: the run wrote it, or it appeared after the run ended (an
        # x80_files to_guest). Modified: it was there when the run ended and
        # its size or mtime has moved since.
        created = rel.lower() in created_names or was is None
        modified = (not created) and was != [st.st_size, st.st_mtime_ns]
        if since is Since.START and not (created or modified):
            continue
        if since is Since.LAST_CALL and st.st_mtime_ns <= previous_ns:
            continue
        row: dict[str, Any] = {
            "guest_name": guest_name,
            "host_name": rel,
            "bytes": st.st_size,
        }
        if want_hash:
            row["sha256"] = sha256_bytes(path.read_bytes())
        row["created"] = created
        row["modified"] = modified
        rows.append(row)
    return {"files": rows, "drives": _drive_rows(ref), "via": str(Via.SANDBOX)}


def _files_to_guest(ref: _SandboxRef, a: Mapping[str, Any]) -> dict[str, Any]:
    files = FileIn.list_from_json(a.get("files") or [])
    if not files:
        raise ToolExecutionError(
            ToolError.bad_argument("files", "op:'to_guest' needs at least one file")
        )
    overwrite = bool(a.get("overwrite", False))
    letter = _drive_rows(ref)[0]["letter"]
    placed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for f in files:
        # Containment before existence, and not the other way round: a raw
        # `guest_dir / guest_name` join answers `.exists()` for any path on the
        # host, so the collision check itself became an oracle. See
        # Sandbox.guest_path.
        target = ref.sandbox.guest_path(f.guest_name, "files[].guest_name")
        if target.exists() and not overwrite:
            # SPEC.md 6.4: "There is no elicitation; this returns isError
            # instead of asking." Per-file, so one collision does not lose the
            # rest of the batch; the call itself still succeeded.
            skipped.append({
                "guest_name": f.guest_name,
                "reason": (
                    f"a file already exists at {target.name} and overwrite is "
                    f"false; pass overwrite:true to replace it"
                ),
            })
            continue
        written = ref.sandbox.stage(f)
        placed.append({
            "guest_name": f.guest_name,
            "via": str(Via.SANDBOX),
            "drive": letter,
            "bytes": written.stat().st_size,
            "host_path_used": str(written),
        })
    return {"placed": placed, "skipped": skipped}


def _files_from_guest(ref: _SandboxRef, a: Mapping[str, Any]) -> dict[str, Any]:
    patterns = list(a.get("names") or [])
    return_content = ReturnContent(a.get("return_content", "inline_if_under_kb"))
    max_kb = int(a.get("max_kb", 64))
    export_to = a.get("export_to")
    out_dir = None
    if export_to:
        out_dir = Path(export_to).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolExecutionError(
                ToolError.bad_argument(
                    "export_to", f"cannot create the host directory: {exc}",
                    export_to=str(out_dir),
                )
            ) from None

    rows: list[dict[str, Any]] = []
    truncated: list[str] = []
    name = ref.sandbox.root.name
    for path in _walk_guest(ref.sandbox.guest_dir):
        rel = path.relative_to(ref.sandbox.guest_dir).as_posix()
        guest_name = ref.guest_case(rel)
        if not _name_matches(patterns, rel, guest_name):
            continue
        data = path.read_bytes()
        inline = (
            return_content is ReturnContent.ALWAYS
            or (return_content is ReturnContent.INLINE_IF_UNDER_KB and len(data) <= max_kb * 1024)
        )
        row: dict[str, Any] = {
            "guest_name": guest_name,
            "bytes": len(data),
            "sha256": sha256_bytes(data),
        }
        if inline:
            row["content_b64"] = base64.b64encode(data).decode("ascii")
        else:
            truncated.append(guest_name)
        # SPEC.md 4.6 gives resource templates for a live machine's files. A
        # sandbox has no handle, so it is addressed by its own directory name;
        # resources/read arrives with x80_open in phase 2, which is when these
        # become fetchable rather than merely stable.
        row["resource_uri"] = f"80mcp://sandbox/{name}/files/{rel}"
        if out_dir is not None:
            dest = out_dir / Path(rel).name
            dest.write_bytes(data)
            row["host_path"] = str(dest)
        rows.append(row)
    return {"files": rows, "truncated": truncated}


# ==========================================================================
# 7. x80_probe
# ==========================================================================

def x80_probe(ctx: CallContext) -> ToolResult:
    """SPEC.md 6.4. Run it on the cheapest profile; report what it needs."""
    a = ctx.arguments
    program = a["program"]
    args = list(a.get("args", []))
    requested = a.get("family", "auto")
    family = Family(requested) if requested != "auto" else detect_family(program)

    prober = _prober()
    profile_id = CHEAPEST_PROFILE[family]
    profile = prober.require_ready(profile_id)
    files_in = _files_in(a)
    stdin = _encode_stdin(a.get("stdin"))
    timeout_ms = int(a.get("timeout_ms", 15000))

    sandbox = Sandbox()
    try:
        if family is Family.Z80:
            backend = _cpm_backend(prober, profile)
            backend.boot(BootSpec(sandbox=sandbox, profile=profile.id, program=program))
            outcome = backend.run(
                RunRequest(
                    sandbox=sandbox, program=program, args=args, stdin=stdin,
                    timeout_ms=timeout_ms, files_in=files_in,
                    env=dict(prober.backend(profile.backend).env),
                )
            )
            stderr = decode_guest(outcome.stderr)
            loaded = bool(_CPMEMU_LOADED.search(stderr))
            counts: dict[int, int] = {}
            for found in _CPMEMU_BDOS.findall(stderr):
                counts[int(found)] = counts.get(int(found), 0) + 1
            unimplemented = [
                bdos_call(func, counts.get(func, 1)) for func in outcome.unimplemented_bdos
            ]
        else:
            backend = _dos_backend(prober, profile)
            backend.boot(BootSpec(sandbox=sandbox, profile=profile.id, program=program))
            outcome = backend.run(
                RunRequest(
                    sandbox=sandbox, program=program, args=args, stdin=stdin,
                    timeout_ms=timeout_ms, files_in=files_in,
                    env=dict(prober.backend(profile.backend).env),
                )
            )
            loaded = outcome.exit_code_meaning is not ExitCodeMeaning.LOADER_FAILURE
            unimplemented = [int21_call(u.ah) for u in outcome.unimplemented_int21]

        stdout = decode_guest(outcome.stdout)
        first_line = next((ln.strip() for ln in stdout.splitlines() if ln.strip()), "")
        extra = [
            f"exit_reason {outcome.exit_reason} after {outcome.wall_ms} ms on {profile.id}",
        ]
        if first_line:
            extra.append(f"first line of output: {first_line[:160]}")
        if outcome.timed_out:
            extra.append(
                f"the {timeout_ms} ms deadline fired, so the syscall list is "
                f"whatever the program reached before it was killed"
            )

        verdict, recommended, evidence, escalation = classify_probe(
            ran_on=profile.id,
            unimplemented=unimplemented,
            loaded=loaded,
            ready_profiles=prober.ready_ids(),
            program=program,
            args=args,
            extra_evidence=extra,
        )
        result = ProbeResult(
            ran_on=profile.id,
            verdict=verdict,
            unimplemented=unimplemented,
            evidence=evidence,
            recommended_profile=recommended,
            escalation=escalation,
        )
        body = result.to_json()
        lines = [f"verdict: {verdict}   ran_on: {profile.id}   recommended: {recommended}"]
        lines += [f"  {e}" for e in evidence]
        for u in unimplemented:
            lines.append(f"  unimplemented {u.layer} {u.func} {u.name} x{u.count}")
        if escalation is not None:
            lines.append("next call: " + json.dumps(escalation.to_json(), ensure_ascii=False))
        return ToolResult.ok(body, text="\n".join(lines))
    finally:
        sandbox.cleanup()


# ==========================================================================
# 8. x80_images -- the only tool that touches the network
# ==========================================================================

def x80_images(ctx: CallContext) -> ToolResult:
    """SPEC.md 6.4. Never called implicitly; a fetch needs both flags."""
    a = ctx.arguments
    body = resolve_images(
        a.get("image_ids"),
        romwbw_version=a.get("romwbw_version"),
        dry_run=bool(a.get("dry_run", True)),
        allow_fetch=bool(a.get("allow_fetch", False)),
        timeout_ms=int(a.get("timeout_ms", 120000)),
    )
    lines = [f"catalog {body['catalog_version']}  {len(body['images'])} image(s)"]
    for img in body["images"]:
        state = "present" if img["present"] else "absent"
        if img["fetched"]:
            state = "fetched"
        lines.append(f"  {img['id']:<18} {state:<8} {img['bytes'] or '?':>12}  {img['licence']}")
    lines += [f"note: {n}" for n in body["notes"]]
    return ToolResult.ok(body, text="\n".join(lines))


# ==========================================================================
# 9. Registration
# ==========================================================================

#: Name -> handler, in SPEC.md 6.1 order. ``ToolRegistry.ordered()`` sorts by
#: that table anyway, so this order is documentation rather than mechanism.
HANDLERS: dict[str, Any] = {
    "x80_profiles": x80_profiles,
    "x80_cpm_run": x80_cpm_run,
    "x80_dos_run": x80_dos_run,
    "x80_diff_run": x80_diff_run,
    "x80_files": x80_files,
    "x80_probe": x80_probe,
    "x80_images": x80_images,
}

assert tuple(HANDLERS) == tuple(PHASE1_TOOLS), (
    f"phase-1 handler set drifted from schemas.PHASE1_TOOLS: "
    f"{tuple(HANDLERS)} != {tuple(PHASE1_TOOLS)}"
)


def register_phase1_tools(server: Server) -> Server:
    """Register the seven. Descriptions, schemas and annotations all come from
    :mod:`eightymcp.schemas`, so SPEC.md 6.2's annotation table is the only
    place they are written down -- the defaults bite (``destructiveHint`` and
    ``openWorldHint`` both default *true*) and a second copy here would be a
    second thing to get wrong."""
    for name, handler in HANDLERS.items():
        server.register_spec_tool(name, handler)
    return server


def build_server(**kwargs: Any) -> Server:
    """A :class:`~eightymcp.server.Server` with the phase-1 surface on it."""
    return register_phase1_tools(Server(**kwargs))
