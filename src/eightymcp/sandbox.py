"""Hermetic launch, wall-clock deadlines, and the process-group kill.

This module is the one place the four launch invariants of SPEC.md 5.4 are
enforced, and the only thing standing between an agent and a spinning
emulator.

**Why the deadline lives here and nowhere else.** SPEC.md 5.5, first bullet:
"Nothing in the family has a timeout." ``dosiz``'s run loop has no instruction
counter and no wall clock -- a 2-byte ``EB FE`` spin ran until SIGKILL;
``cpmemu``'s only guard is a hardcoded 9e9-instruction watchdog that does not
fire on a guest blocked on an idle open pipe; ``romwbw_emu``'s is 10e9.
``mpm2_emu -t SECS`` is the family's only internal timeout and is passed as
belt-and-braces, never relied on. So: "Every launch is externally deadlined and
killed by process group."

**Why the kill is by group and not by pid.** A backend that forks -- a shell
wrapper, a helper, a slirp thread that re-execs -- leaves the child's children
running when the direct child is signalled, and those children hold the stdout
and stderr pipes open, so the supervisor cannot even tell the run is over.
:meth:`Sandbox.exec` starts every child with ``start_new_session=True``, which
calls ``setsid(2)`` and makes the child's pid its own process-group id, and
kills that whole group. ``tests/test_sandbox.py`` proves both halves against
``/bin/sh``.

**Invariant 1 is four things, none optional** (SPEC.md 5.4; Appendix B item 1
exists because every design in the round got this one wrong)::

    HOME=<session>/home  XDG_CONFIG_HOME=<session>/xdg \\
      romwbw_emu --no-config --boot=<explicit target>

``XDG_CONFIG_HOME`` plus ``--no-config`` is *not* sufficient, and that was
measured: with both set, stderr still said ``Loaded NVRAM setting 'C' from
/Users/wohl/.config/romwbw_emu/nvram``. The cause is a deliberate migration
fallback at ``romwbw_emu.cc:1379-1390`` -- when the XDG path holds no setting,
``load_nvram_setting(legacy_nvram_path)`` runs, and ``get_legacy_nvram_path()``
(``romwbw_emu.cc:79-82``) is built from ``$HOME`` unconditionally. Writes were
isolated; reads were not.

This module owns the two of the four that are environment: ``HOME`` and
``XDG_CONFIG_HOME``, re-asserted after any caller-supplied ``env`` is merged so
that a backend cannot drop them back to the developer's real home. The other
two -- ``--no-config`` and an explicit ``--boot=`` -- are argv, and belong to
the backend adapters.

**Invariant 2** (pace before every send) is a pty-adapter concern and lands in
phase 2. **Invariants 3 and 4** are cpmemu/tool concerns. Nothing here has an
opinion about exit codes: :class:`~eightymcp.types.ExecResult` reports the
*process* rc, which on every CP/M path is 0 and means nothing (SPEC.md 5.4,
Invariant 4).

**The LST: and PUN: files.** SPEC.md 5.5 and Appendix B item 2: ``printer =``
and ``aux_output =`` empty produce ``Warning: Cannot open printer file '': No
such file or directory`` on every single run, and ``/dev/null`` silences that
warning while silently destroying the byte streams, so a CP/M utility that
writes its report to the list device returns an empty stdout and looks like a
clean no-op. Every sandbox therefore has a real :attr:`Sandbox.lst_path` and
:attr:`Sandbox.pun_path`, created empty, reported through
:meth:`Sandbox.stream_out`.
"""

from __future__ import annotations

import base64
import errno
import fnmatch
import hashlib
import os
import selectors
import shutil
import signal
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .jsonrpc import log
from .types import (
    ExecResult,
    FileIn,
    FileMode,
    FileOut,
    ReturnContent,
    StreamOut,
    ToolError,
    ToolExecutionError,
)

__all__ = [
    "SANDBOX_PREFIX",
    "PASSTHROUGH_ENV",
    "PROTECTED_ENV",
    "KILL_GRACE_MS",
    "DRAIN_GRACE_MS",
    "MAX_CAPTURE_BYTES",
    "Collect",
    "Sandbox",
    "sha256_file",
    "sha256_bytes",
]


#: Temp-directory prefix. Deliberately short: SPEC.md 5.5 records dosiz
#: building ``argv[0]`` as ``"C:" + <uppercased backslashed host path>``, and a
#: DOS guest has an 8.3 path budget to spend.
SANDBOX_PREFIX = "80mcp-"

#: Host variables that survive the scrub, and why each one does.
#:
#: These decide *which binary runs* and how it links. They are installation
#: facts, not guest configuration, and dropping them breaks a working install
#: -- SPEC.md 6.4 records ``mpm2_emu`` needing
#: ``DYLD_LIBRARY_PATH`` for ``/usr/local/lib/libqkz80.4.dylib``. Everything
#: else in the parent environment is dropped: it is exactly the class of thing
#: that made the NVRAM leak invisible for so long.
PASSTHROUGH_ENV: tuple[str, ...] = (
    "PATH",
    "TZ",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
)

#: Used when the parent has no ``PATH`` at all.
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

#: Keys the ``env`` argument to :meth:`Sandbox.exec` cannot change. This is
#: Invariant 1 having teeth rather than being a convention: a backend that
#: builds its own environment must not be able to hand the guest the
#: developer's real ``$HOME`` back.
PROTECTED_ENV: tuple[str, ...] = ("HOME", "XDG_CONFIG_HOME")

#: How long the process group gets between SIGTERM and SIGKILL. Paid only on
#: the timeout path.
KILL_GRACE_MS = 250

#: How long to keep reading the pipes after the group has been killed, so that
#: partial output survives a timeout. Bounded, because a grandchild that called
#: ``setsid`` itself escaped the group and would otherwise hold them forever.
DRAIN_GRACE_MS = 500

#: Ceiling on one staged input. Reading a host file into a ``bytes`` is the
#: only unbounded allocation on the staging path, and an input that large is a
#: mistake worth naming rather than an OOM worth surviving. Well clear of the
#: 51,380,224-byte combo image SPEC.md 5.5 clones.
MAX_STAGE_BYTES = 256 * 1024 * 1024

#: Per-stream capture cap. A backend can emit output faster than the deadline
#: expires; the supervisor keeps reading past this point (so the child never
#: blocks on a full pipe) but stops accumulating. Overflow is announced on
#: stderr, never on stdout: stdout is the guest transcript an agent asserts
#: against and stays verbatim.
MAX_CAPTURE_BYTES = 32 * 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Collect:
    """SPEC.md 6.3 ``Collect``, minus ``normalize``.

    ``normalize`` is in ``x80_cpm_run``'s inline copy of this shape and is
    deliberately *not* handled here: SPEC.md 6.4 says it is "Applied to the
    REPORTED sha256 and content only, never to the file on disk", which makes
    it :mod:`eightymcp.normalize`'s job, downstream of the manifest this module
    produces. :meth:`from_json` ignores the key rather than rejecting it, so
    the validated ``collect`` object can be passed straight through.
    """

    return_content: ReturnContent = ReturnContent.INLINE_IF_UNDER_KB
    max_kb: int = 64
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, obj: Mapping[str, Any] | None) -> "Collect":
        if obj is None:
            return cls()
        return cls(
            return_content=ReturnContent(
                obj.get("return_content", ReturnContent.INLINE_IF_UNDER_KB)
            ),
            max_kb=int(obj.get("max_kb", 64)),
            exclude=tuple(obj.get("exclude", ())),
        )

    def inline(self, size: int) -> bool:
        """Whether a file of ``size`` bytes gets a ``content_b64``.

        ``always`` means always: the caller asked for the bytes and the schema
        already caps ``max_kb`` at 4096.
        """
        if self.return_content is ReturnContent.NEVER:
            return False
        if self.return_content is ReturnContent.ALWAYS:
            return True
        return size <= self.max_kb * 1024

    def excluded(self, relative: str) -> bool:
        """Match ``exclude`` against the relative path and against the bare
        basename, case-insensitively.

        Case-insensitive because cpmemu lowercases on create (SPEC.md 6.4:
        ``B5-TIME.INF`` lands as ``b5-time.inf``), so an ``exclude`` written in
        the guest's uppercase would silently match nothing.
        """
        low = relative.lower()
        base = low.rsplit("/", 1)[-1]
        for pat in self.exclude:
            p = pat.lower()
            if fnmatch.fnmatchcase(low, p) or fnmatch.fnmatchcase(base, p):
                return True
        return False


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

class Sandbox:
    """One run's private directory tree, plus the supervisor that launches into
    it.

    Layout::

        <root>/          the value reported as `sandbox` in a tool result
        <root>/guest/    the guest's cwd; the only directory the manifest tracks
        <root>/home/     Invariant 1's HOME
        <root>/xdg/      Invariant 1's XDG_CONFIG_HOME
        <root>/tmp/      TMPDIR, so a backend's scratch files stay inside
        <root>/.lst      CP/M LST: device  (never empty, never /dev/null)
        <root>/.pun      CP/M PUN: device  (never empty, never /dev/null)

    A synthesized config (:meth:`write_text`) lands in ``<root>``, not in
    ``<root>/guest``, so that supervisor-owned files can never appear in the
    guest's file manifest.

    Satisfies :class:`eightymcp.mbp.SandboxProtocol` structurally; that is
    asserted in ``tests/test_sandbox.py`` rather than by inheriting, so the
    backends can be written against a stub.
    """

    __slots__ = (
        "root", "guest_dir", "home_dir", "xdg_dir", "tmp_dir",
        "lst_path", "pun_path", "keep", "_snapshot", "_staged",
        "_live_groups", "_cleaned",
    )

    def __init__(
        self,
        *,
        base_dir: str | os.PathLike[str] | None = None,
        keep: bool = False,
        prefix: str = SANDBOX_PREFIX,
    ) -> None:
        self.root = Path(tempfile.mkdtemp(prefix=prefix, dir=base_dir)).resolve()
        self.guest_dir = self.root / "guest"
        self.home_dir = self.root / "home"
        self.xdg_dir = self.root / "xdg"
        self.tmp_dir = self.root / "tmp"
        for d in (self.guest_dir, self.home_dir, self.xdg_dir, self.tmp_dir):
            d.mkdir(mode=0o700)
        # XDG_CACHE_HOME / XDG_DATA_HOME / XDG_STATE_HOME are created lazily by
        # whoever writes into them; only the two Invariant-1 paths have to
        # exist up front.
        self.lst_path = self.root / ".lst"
        self.pun_path = self.root / ".pun"
        self.lst_path.touch(mode=0o600)
        self.pun_path.touch(mode=0o600)

        #: keep_sandbox. When true, :meth:`cleanup` leaves the tree on disk and
        #: :attr:`reported_path` is the path instead of null.
        self.keep = keep
        self._snapshot: dict[str, tuple[int, int, int]] = {}
        self._staged: list[Path] = []
        self._live_groups: set[int] = set()
        self._cleaned = False

    @classmethod
    def adopt(cls, root: str | os.PathLike[str], *, keep: bool = True) -> "Sandbox":
        """Wrap a sandbox tree that already exists on disk.

        ``x80_files``'s sandbox route (SPEC.md 6.4) is handed the path a batch
        verb returned under ``keep_sandbox:true`` and then has to stage into
        it, list it and hash it -- the same operations as a live run, on a tree
        this process did not create and may not have created in this
        invocation at all. Adopting is how that caller gets *one*
        implementation of staging, including the ``mode:"text"`` transform
        documented on :meth:`stage`, instead of a second one that can drift.

        ``keep`` defaults to **true**, the opposite of :meth:`__init__`: the
        tree is on disk because a caller asked for it to survive, so a
        :meth:`cleanup` that removed it would destroy the thing the call was
        about. SPEC.md 4.3 gives such a tree 24 hours, not the length of one
        tool call.

        The missing pieces of the layout are created rather than demanded:
        ``.lst``/``.pun`` are absent on a tree a dosiz run never wrote to
        (dosiz creates them lazily on the first byte), and refusing to adopt
        over that would fail the common case.
        """
        base = Path(root).expanduser().resolve()
        if not base.is_dir():
            raise ToolExecutionError(
                ToolError.bad_argument(
                    "sandbox",
                    "not a directory on this host",
                    sandbox=str(base),
                )
            )
        self = object.__new__(cls)
        self.root = base
        self.guest_dir = base / "guest"
        self.home_dir = base / "home"
        self.xdg_dir = base / "xdg"
        self.tmp_dir = base / "tmp"
        for d in (self.guest_dir, self.home_dir, self.xdg_dir, self.tmp_dir):
            d.mkdir(mode=0o700, exist_ok=True)
        self.lst_path = base / ".lst"
        self.pun_path = base / ".pun"
        for f in (self.lst_path, self.pun_path):
            if not f.exists():
                f.touch(mode=0o600)
        self.keep = keep
        self._snapshot = {}
        self._staged = []
        self._live_groups = set()
        self._cleaned = False
        return self

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.cleanup()

    def __repr__(self) -> str:
        return f"<Sandbox {self.root} keep={self.keep}>"

    @property
    def reported_path(self) -> str | None:
        """The ``sandbox`` field of a tool result.

        SPEC.md 6.4: the key is always emitted, null when reaped. So: the path
        when the tree survives this call, ``None`` when it does not.
        """
        return str(self.root) if self.keep else None

    def cleanup(self) -> None:
        """Remove the tree, unless :attr:`keep`. Idempotent, and safe after a
        killed run.

        Two things make this less trivial than ``rmtree``: a run that was
        killed by process group may have left a partially written file, and a
        backend may have dropped a mode-0400 file in the tree. So any process
        group still alive is killed first, then a plain ``rmtree``, then -- if
        that raised -- a chmod pass and a second ``rmtree`` that ignores what
        it cannot remove. A sandbox that survives is logged, never raised: the
        run's result is worth more than the temp directory.
        """
        if self._cleaned:
            return
        self._cleaned = True
        self._kill_live_groups()
        if self.keep:
            return
        try:
            shutil.rmtree(self.root)
            return
        except FileNotFoundError:
            return
        except OSError:
            pass
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                p = os.path.join(dirpath, name)
                try:
                    os.chmod(p, stat.S_IRWXU, follow_symlinks=False)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)
        if self.root.exists():
            log(f"sandbox {self.root} could not be removed; left on disk")

    def _kill_live_groups(self) -> None:
        for pgid in sorted(self._live_groups):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        self._live_groups.clear()

    # -- paths ------------------------------------------------------------

    def relative(self, path: str | os.PathLike[str]) -> str:
        """``path`` as a POSIX-relative name under :attr:`guest_dir`."""
        return Path(path).resolve().relative_to(self.guest_dir).as_posix()

    @staticmethod
    def _readable_source(path: Path, argument: str, **fields: Any) -> int:
        """Refuse a host source that cannot be read to an end, and say why.

        Measured: ``files_in:[{"host_path":"<a fifo>"}]`` hung the whole
        server. ``Path.read_bytes`` blocks in ``open(2)`` on a FIFO with no
        writer, the staging path is upstream of every deadline in this module
        -- ``timeout_ms`` covers the exec, not the setup -- and the request
        loop is serial, so one such call takes the connection with it: no
        reply, and no later request is even read. ``/dev/zero`` is the same
        hazard with allocation instead of blocking.

        ``stat`` does not block on any of them, so the check is free. Returns
        the size, having already refused anything that is not a regular file
        of a sane length.
        """
        try:
            st = path.stat()
        except FileNotFoundError:
            raise ToolExecutionError(
                ToolError.bad_argument(argument, f"no such file: {path}", **fields)
            ) from None
        except OSError as e:
            raise ToolExecutionError(
                ToolError.bad_argument(
                    argument, f"cannot stat {path}: {e.strerror or e}", **fields
                )
            ) from None
        if stat.S_ISDIR(st.st_mode):
            raise ToolExecutionError(
                ToolError.bad_argument(
                    argument, f"is a directory, not a file: {path}", **fields
                )
            )
        if not stat.S_ISREG(st.st_mode):
            kind = {
                stat.S_IFIFO: "a FIFO",
                stat.S_IFCHR: "a character device",
                stat.S_IFBLK: "a block device",
                stat.S_IFSOCK: "a socket",
            }.get(stat.S_IFMT(st.st_mode), "not a regular file")
            raise ToolExecutionError(
                ToolError.bad_argument(
                    argument,
                    f"{path} is {kind}; only a regular file can be staged, "
                    f"because reading one of these has no end and the staging "
                    f"step runs before any deadline",
                    **fields,
                )
            )
        if st.st_size > MAX_STAGE_BYTES:
            raise ToolExecutionError(
                ToolError.bad_argument(
                    argument,
                    f"{path} is {st.st_size} bytes; the staging ceiling is "
                    f"{MAX_STAGE_BYTES}",
                    **fields,
                )
            )
        return st.st_size

    def _inside(self, base: Path, name: str, argument: str) -> Path:
        """Resolve ``name`` under ``base``, refusing anything that escapes.

        A ``guest_name`` of ``../../etc/passwd`` is not a CP/M 8.3 name and is
        not a mistake worth guessing about; it gets a structured
        ``bad_argument`` an agent can read.
        """
        if not name or name in (".", ".."):
            raise ToolExecutionError(
                ToolError.bad_argument(argument, f"empty or invalid name: {name!r}")
            )
        candidate = Path(name)
        if candidate.is_absolute():
            raise ToolExecutionError(
                ToolError.bad_argument(
                    argument,
                    f"must be a name relative to the sandbox, not an absolute path: {name!r}",
                )
            )
        target = (base / candidate).resolve()
        if target != base and base not in target.parents:
            raise ToolExecutionError(
                ToolError.bad_argument(
                    argument,
                    f"resolves outside the sandbox: {name!r}",
                    sandbox=str(self.root),
                )
            )
        return target

    def guest_path(self, guest_name: str, argument: str = "guest_name") -> Path:
        """Where ``guest_name`` would land under :attr:`guest_dir`.

        The containment check of :meth:`_inside`, exposed because a caller
        sometimes has to know the path *before* it writes -- ``x80_files``'s
        ``to_guest`` route checks for a collision first, and doing that on an
        unvalidated join turned the tool into a host-filesystem existence
        oracle: ``guest_name:"../../../../etc/passwd"`` came back as
        ``skipped: a file already exists``, while a name that resolved nowhere
        came back as the containment error. Same information, two answers.
        Resolve first, answer second.
        """
        return self._inside(self.guest_dir, guest_name, argument)

    # -- staging ----------------------------------------------------------

    def stage(self, f: FileIn) -> Path:
        """Materialise one :class:`~eightymcp.types.FileIn` into
        :attr:`guest_dir` and return the host path written.

        ``mode`` is the only transform, and it is narrow:

        * ``binary`` -- bytes verbatim. The default everywhere, per SPEC.md 5.4
          Invariant 3.
        * ``text`` -- line endings normalised to CRLF, the on-disk convention
          of both guests. SPEC.md does not define this transform, so it is
          stated here rather than inferred elsewhere: the cpmemu config the
          batch verb synthesizes carries ``eol_convert = false`` (SPEC.md 7.1,
          verbatim from the demonstrated run), so nothing downstream converts,
          and a host file with LF endings would reach a CP/M or DOS program
          unterminated. The conversion is idempotent -- CRLF and lone CR both
          collapse to LF first -- so staging an already-CRLF file does not
          double it.

        Call order matters: :meth:`snapshot` comes *after* staging, so that
        input files do not appear in the manifest as things the guest created.
        SPEC.md 7.1: "mktemp a sandbox; synthesize a .cfg; stage M9.ARC;
        snapshot the directory; exec".
        """
        target = self._inside(self.guest_dir, f.guest_name, "files_in[].guest_name")
        if f.host_path is not None:
            src = Path(f.host_path).expanduser()
            self._readable_source(
                src, "files_in[].host_path", guest_name=f.guest_name
            )
            try:
                data = src.read_bytes()
            except FileNotFoundError:
                raise ToolExecutionError(
                    ToolError.bad_argument(
                        "files_in[].host_path",
                        f"no such file: {src}",
                        guest_name=f.guest_name,
                    )
                ) from None
            except IsADirectoryError:
                raise ToolExecutionError(
                    ToolError.bad_argument(
                        "files_in[].host_path",
                        f"is a directory, not a file: {src}",
                        guest_name=f.guest_name,
                    )
                ) from None
            except OSError as e:
                raise ToolExecutionError(
                    ToolError.bad_argument(
                        "files_in[].host_path",
                        f"cannot read {src}: {e.strerror or e}",
                        guest_name=f.guest_name,
                    )
                ) from None
        else:
            assert f.content_b64 is not None  # FileIn.__post_init__ guarantees it
            try:
                data = base64.b64decode(f.content_b64, validate=True)
            except (ValueError, TypeError) as e:
                raise ToolExecutionError(
                    ToolError.bad_argument(
                        "files_in[].content_b64",
                        f"not valid base64: {e}",
                        guest_name=f.guest_name,
                    )
                ) from None
        if f.mode is FileMode.TEXT:
            data = _to_guest_text(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o600)
        self._staged.append(target)
        return target

    def stage_all(self, files: Iterable[FileIn]) -> list[Path]:
        return [self.stage(f) for f in files]

    def stage_program(
        self, host_path: str | os.PathLike[str], guest_name: str | None = None
    ) -> Path:
        """Copy a guest executable into :attr:`guest_dir`, executable bit and
        all, and return its path.

        SPEC.md 5.5 makes this mandatory for dosiz rather than stylistic:
        ``./build/dosiz /abs/path/tests/DJ_PRINTF.exe`` gave **rc 102** and
        ``C:\\PRIVATE\\TMP\\...\\DJ_PRINTF.EXE: can't open``, because
        ``argv[0]`` is built as ``"C:"`` + the host path, uppercased with
        backslashes, and DJGPP's go32 stub reopens it to load its COFF payload.
        The program has to be *in* the sandbox so it can be named relatively.
        """
        src = Path(host_path).expanduser()
        self._readable_source(src, "program")
        name = guest_name if guest_name is not None else src.name
        target = self._inside(self.guest_dir, name, "program")
        try:
            shutil.copyfile(src, target)
        except FileNotFoundError:
            raise ToolExecutionError(
                ToolError.bad_argument("program", f"no such file: {src}")
            ) from None
        except OSError as e:
            raise ToolExecutionError(
                ToolError.bad_argument("program", f"cannot copy {src}: {e.strerror or e}")
            ) from None
        os.chmod(target, 0o700)
        self._staged.append(target)
        return target

    def write_text(self, relative: str, text: str) -> Path:
        """Write a supervisor-owned file into the sandbox **root** -- the
        synthesized cpmemu ``.cfg`` of SPEC.md 7.1 -- and return its path.

        Root, not ``guest/``, precisely so it cannot turn up in
        :meth:`created_since_snapshot`.
        """
        target = self._inside(self.root, relative, "sandbox file")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        os.chmod(target, 0o600)
        return target

    # -- the before/after manifest ---------------------------------------

    def snapshot(self) -> None:
        """Record :attr:`guest_dir` so :meth:`created_since_snapshot` can
        answer "what did this run make?" -- the question no emulator in the
        family reports today (SPEC.md 6.4, ``x80_files.since``).

        Call it after staging and before exec.
        """
        self._snapshot = self._scan()

    def _scan(self) -> dict[str, tuple[int, int, int]]:
        out: dict[str, tuple[int, int, int]] = {}
        for dirpath, dirnames, filenames in os.walk(self.guest_dir):
            dirnames.sort()
            for name in sorted(filenames):
                p = Path(dirpath) / name
                try:
                    st = p.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                rel = p.relative_to(self.guest_dir).as_posix()
                out[rel] = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        return out

    def created_since_snapshot(self) -> list[Path]:
        """Host paths under :attr:`guest_dir` created or modified since
        :meth:`snapshot`, sorted.

        "Modified" is ``(size, mtime_ns, ctime_ns)`` changing. A guest that
        rewrote a file to the same length within the same nanosecond tick would
        be missed; on APFS that has not been observed and there is no cheaper
        exact answer that does not hash every input up front.
        """
        now = self._scan()
        changed = [
            self.guest_dir / rel
            for rel, meta in now.items()
            if self._snapshot.get(rel) != meta
        ]
        return sorted(changed)

    def collect(
        self,
        opts: Collect | None = None,
        *,
        guest_name: Callable[[str], str] | None = None,
    ) -> list[FileOut]:
        """The ``files_out`` manifest: every file the run created or modified,
        with size and sha256, under the ``Collect`` shape.

        ``guest_name`` maps a host name back to the name the guest used. It is
        a parameter and not a rule because the convention is the backend's:
        cpmemu lowercases on create, so ``cpmemu.py`` passes ``str.upper`` and
        ``B5-TIME.INF`` is reported with ``host_name: "b5-time.inf"``
        (SPEC.md 6.4: "host_name is reported separately from guest_name because
        cpmemu lowercases on create, so a caller writing case-sensitive
        assertions against host names will be wrong"). Default is identity, and
        :class:`~eightymcp.types.FileOut` omits ``host_name`` when the two
        agree.

        No normalization is applied here. SPEC.md 6.4 scopes ``normalize`` to
        "the REPORTED sha256 and content only", which is
        :mod:`eightymcp.normalize` acting on this list.
        """
        o = opts if opts is not None else Collect()
        out: list[FileOut] = []
        for path in self.created_since_snapshot():
            rel = path.relative_to(self.guest_dir).as_posix()
            if o.excluded(rel):
                continue
            try:
                data = path.read_bytes()
            except OSError as e:
                log(f"collect: cannot read {path}: {e.strerror or e}")
                continue
            gname = guest_name(rel) if guest_name is not None else rel
            out.append(
                FileOut(
                    guest_name=gname,
                    bytes=len(data),
                    sha256=sha256_bytes(data),
                    host_name=rel if rel != gname else None,
                    content_b64=(
                        base64.b64encode(data).decode("ascii")
                        if o.inline(len(data))
                        else None
                    ),
                )
            )
        return out

    def stream_out(self, path: Path, opts: Collect | None = None) -> StreamOut:
        """One of ``list_output`` / ``punch_output``.

        SPEC.md 7.1 shows ``"list_output":{"bytes":0}`` for an untouched
        stream, so an empty file gets a bare byte count and no hash: there is
        nothing to hash and a sha256 of b"" would read as evidence.
        """
        o = opts if opts is not None else Collect()
        try:
            data = path.read_bytes()
        except OSError:
            return StreamOut(bytes=0)
        if not data:
            return StreamOut(bytes=0)
        return StreamOut(
            bytes=len(data),
            sha256=sha256_bytes(data),
            content_b64=(
                base64.b64encode(data).decode("ascii") if o.inline(len(data)) else None
            ),
        )

    def list_output(self, opts: Collect | None = None) -> StreamOut:
        return self.stream_out(self.lst_path, opts)

    def punch_output(self, opts: Collect | None = None) -> StreamOut:
        return self.stream_out(self.pun_path, opts)

    # -- environment ------------------------------------------------------

    def base_env(self) -> dict[str, str]:
        """The scrubbed environment every child gets.

        Allowlist, not denylist. A denylist is how ``$HOME`` survived into
        ``get_legacy_nvram_path()`` in the first place.
        """
        env: dict[str, str] = {}
        for key in PASSTHROUGH_ENV:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        env.setdefault("PATH", DEFAULT_PATH)
        # Invariant 1, the two halves this module owns.
        env["HOME"] = str(self.home_dir)
        env["XDG_CONFIG_HOME"] = str(self.xdg_dir)
        # The other XDG roots for the same reason HOME is here: a migration
        # fallback reads whichever one the author happened to use.
        env["XDG_CACHE_HOME"] = str(self.root / "xdg-cache")
        env["XDG_DATA_HOME"] = str(self.root / "xdg-data")
        env["XDG_STATE_HOME"] = str(self.root / "xdg-state")
        env["TMPDIR"] = str(self.tmp_dir)
        env["TMP"] = str(self.tmp_dir)
        env["TEMP"] = str(self.tmp_dir)
        # Deterministic output. A backend that wants a real locale or terminal
        # can pass one through `env`; only PROTECTED_ENV is immovable.
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        env["TERM"] = "dumb"
        return env

    def build_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """:meth:`base_env` with ``extra`` merged on top, then
        :data:`PROTECTED_ENV` re-asserted.

        The re-assert is the point. ``env`` *adds to* the hermetic base; it
        cannot drop ``HOME`` or ``XDG_CONFIG_HOME`` and reintroduce the NVRAM
        leak, whether by accident or by a caller passing a whole
        ``os.environ``.
        """
        env = self.base_env()
        if extra:
            for k, v in extra.items():
                env[str(k)] = str(v)
        for key in PROTECTED_ENV:
            forced = str(self.home_dir) if key == "HOME" else str(self.xdg_dir)
            if env.get(key) != forced:
                if extra and key in extra:
                    log(
                        f"ignoring {key}={extra[key]!r} from the backend: "
                        f"SPEC.md 5.4 Invariant 1 pins it to {forced}"
                    )
                env[key] = forced
        return env

    # -- exec -------------------------------------------------------------

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

        * ``cwd`` defaults to :attr:`guest_dir`, the directory the manifest
          tracks. Every backend should run there, and dosiz's requirement
          (SPEC.md 5.5) is satisfied by it: what matters is that ``argv[0]`` is
          *relative*, not which directory it is relative to.
        * ``stdin`` empty means ``/dev/null``, i.e. immediate EOF. SPEC.md 7.1
          launches cpmemu that way, and cpmemu's
          ``[Exiting: 1024 console reads past end of input]`` sentinel is what
          an EOF-driven run terminates on.
        * stdout and stderr are separate pipes, never merged. All three
          backends separate guest output from diagnostics (SPEC.md 5.2) and
          merging them would destroy the sentinel parsing every
          ``exit_reason`` depends on.
        * The deadline is wall clock, measured from just before ``fork``, and
          when it fires the process **group** is signalled: SIGTERM, then
          SIGKILL after :data:`KILL_GRACE_MS`. Output that arrived before the
          kill is kept; :data:`DRAIN_GRACE_MS` more is read after it.

        Never raises on a guest failure. It raises
        :class:`~eightymcp.types.ToolExecutionError` only when the *launch*
        could not happen -- a missing or unexecutable binary, a cwd that is not
        a directory -- because that is a structured, actionable answer and a
        traceback is not.
        """
        import subprocess  # local: nothing else in this module needs it

        argv = [str(a) for a in argv]
        if not argv:
            raise ToolExecutionError(
                ToolError.bad_argument("argv", "empty argv cannot be executed")
            )
        run_cwd = Path(cwd) if cwd is not None else self.guest_dir
        if not run_cwd.is_dir():
            # Popen raises FileNotFoundError for a missing cwd and for a
            # missing argv[0] alike, and the except clause below can only guess
            # -- measured, a diff_run with reference.cwd="/no/such/dir" was
            # reported as `backend_missing: no such executable: /bin/sh`, which
            # sends the agent to fix a binary that was never the problem. The
            # NotADirectoryError branch only fires when a path *component* is a
            # file, never when the directory is simply absent.
            raise ToolExecutionError(
                ToolError.bad_argument(
                    "cwd",
                    f"not a directory on this host: {run_cwd}",
                    argv0=argv[0],
                )
            )
        child_env = self.build_env(env)
        child_env["PWD"] = str(run_cwd)

        devnull = None
        if stdin:
            stdin_arg: Any = subprocess.PIPE
        else:
            devnull = os.open(os.devnull, os.O_RDONLY)
            stdin_arg = devnull

        started = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is a list, shell=False
                argv,
                cwd=str(run_cwd),
                env=child_env,
                stdin=stdin_arg,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                close_fds=True,
                # setsid(2): the child's pid becomes its own process-group id,
                # so one killpg reaches everything it forks. SPEC.md 5.5.
                start_new_session=True,
            )
        except FileNotFoundError:
            raise ToolExecutionError(
                ToolError.backend_missing(
                    os.path.basename(argv[0]),
                    f"no such executable: {argv[0]}",
                    hint="x80_profiles reports which backend binaries this installation can find",
                )
            ) from None
        except PermissionError:
            raise ToolExecutionError(
                ToolError.backend_missing(
                    os.path.basename(argv[0]),
                    f"not executable: {argv[0]}",
                    hint="chmod +x the backend binary, or point the profile at the built one",
                )
            ) from None
        except NotADirectoryError:
            raise ToolExecutionError(
                ToolError.bad_argument("cwd", f"not a directory: {run_cwd}")
            ) from None
        except OSError as e:
            raise ToolExecutionError(
                ToolError.backend_missing(
                    os.path.basename(argv[0]),
                    f"cannot exec {argv[0]}: {e.strerror or e}",
                )
            ) from None
        finally:
            if devnull is not None:
                os.close(devnull)

        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            # Already gone. start_new_session makes pgid == pid regardless.
            pgid = proc.pid
        self._live_groups.add(pgid)

        try:
            return self._supervise(
                proc, pgid, argv, run_cwd, stdin, timeout_ms, started
            )
        finally:
            self._live_groups.discard(pgid)

    def _supervise(
        self,
        proc: Any,
        pgid: int,
        argv: list[str],
        run_cwd: Path,
        stdin: bytes,
        timeout_ms: int,
        started: float,
    ) -> ExecResult:
        deadline = started + max(timeout_ms, 0) / 1000.0
        cap = _Capture()
        sel = selectors.DefaultSelector()
        streams: list[Any] = []

        try:
            if proc.stdin is not None:
                os.set_blocking(proc.stdin.fileno(), False)
                sel.register(proc.stdin, selectors.EVENT_WRITE, "in")
                streams.append(proc.stdin)
            for name, fh in (("out", proc.stdout), ("err", proc.stderr)):
                if fh is None:  # pragma: no cover - both are always PIPEs here
                    continue
                os.set_blocking(fh.fileno(), False)
                sel.register(fh, selectors.EVENT_READ, name)
                streams.append(fh)

            written = 0
            timed_out = False
            while True:
                if not sel.get_map() and proc.poll() is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if sel.get_map():
                    written = self._pump(sel, cap, stdin, written, min(remaining, 0.1))
                else:
                    # Pipes closed but the process is still running. Poll for
                    # exit rather than block, so the deadline still binds.
                    time.sleep(min(remaining, 0.01))

            ended = time.monotonic()
            wall_ms = int(round((ended - started) * 1000))

            sig_sent: int | None = None
            if timed_out:
                sig_sent = self._kill_group(pgid, proc, sel, cap, stdin, written)

            rc: int | None
            status = proc.poll()
            if status is None:
                try:
                    status = proc.wait(timeout=2.0)
                except Exception:  # subprocess.TimeoutExpired and anything odd
                    status = None
            if status is None:
                rc, sig = None, sig_sent
            elif status < 0:
                rc, sig = None, -status
            else:
                rc, sig = status, None
            if timed_out:
                # SPEC.md's ExecResult contract: on a supervisor timeout rc is
                # None and signal is what the group was killed with.
                rc = None
                sig = sig if sig is not None else sig_sent

            return ExecResult(
                argv=argv,
                cwd=str(run_cwd),
                rc=rc,
                signal=sig,
                stdout=cap.stdout(),
                stderr=cap.stderr(),
                wall_ms=wall_ms,
                timed_out=timed_out,
                deadline_ms=timeout_ms,
            )
        finally:
            sel.close()
            for fh in streams:
                try:
                    fh.close()
                except OSError:
                    pass
            if proc.poll() is None:  # pragma: no cover - belt and braces
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass

    @staticmethod
    def _pump(
        sel: selectors.BaseSelector,
        cap: "_Capture",
        stdin: bytes,
        written: int,
        select_timeout: float,
    ) -> int:
        """One select round. Returns the new stdin write offset."""
        for key, _events in sel.select(timeout=select_timeout):
            which = key.data
            if which == "in":
                chunk = stdin[written : written + 65536]
                try:
                    n = os.write(key.fd, chunk)
                except (BrokenPipeError, ConnectionResetError):
                    _drop(sel, key.fileobj)
                    continue
                except BlockingIOError:
                    continue
                except OSError as e:
                    if e.errno in (errno.EPIPE, errno.EBADF):
                        _drop(sel, key.fileobj)
                        continue
                    raise
                written += n
                if written >= len(stdin):
                    _drop(sel, key.fileobj)
                continue
            try:
                data = os.read(key.fd, 65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                data = b""
            if not data:
                _drop(sel, key.fileobj)
            else:
                cap.add(which, data)
        return written

    def _kill_group(
        self,
        pgid: int,
        proc: Any,
        sel: selectors.BaseSelector,
        cap: "_Capture",
        stdin: bytes,
        written: int,
    ) -> int | None:
        """SIGTERM the group, give it :data:`KILL_GRACE_MS`, then SIGKILL it.

        The group, not the pid, and the SIGKILL is unconditional if anything is
        still in the group. A direct child that has already exited is not proof
        the run is over: whatever it forked inherited the pipes and is what the
        deadline is actually for.
        """
        sent: int | None = None
        if _signal_group(pgid, signal.SIGTERM):
            sent = int(signal.SIGTERM)
        grace_end = time.monotonic() + KILL_GRACE_MS / 1000.0
        while time.monotonic() < grace_end:
            if sel.get_map():
                written = self._pump(sel, cap, stdin, written, 0.02)
            else:
                time.sleep(0.01)
            proc.poll()  # reap the direct child so it stops counting as group
            if not _group_alive(pgid):
                break
        if _group_alive(pgid):
            if _signal_group(pgid, signal.SIGKILL):
                sent = int(signal.SIGKILL)
            drain_end = time.monotonic() + DRAIN_GRACE_MS / 1000.0
            while sel.get_map() and time.monotonic() < drain_end:
                written = self._pump(sel, cap, stdin, written, 0.02)
        return sent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _drop(sel: selectors.BaseSelector, fileobj: Any) -> None:
    try:
        sel.unregister(fileobj)
    except (KeyError, ValueError):
        return
    try:
        fileobj.close()
    except OSError:
        pass


def _signal_group(pgid: int, sig: int) -> bool:
    """True when the signal was delivered to a group that existed."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not reachable for our own children
        return True
    except OSError:  # pragma: no cover
        return False


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover
        return True
    except OSError:  # pragma: no cover
        return False


def _to_guest_text(data: bytes) -> bytes:
    """LF and lone CR both become CRLF. Idempotent on already-CRLF input."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")


@dataclass(slots=True)
class _Capture:
    """Bounded per-stream accumulation.

    Past :data:`MAX_CAPTURE_BYTES` the supervisor keeps reading -- a child
    blocked on a full pipe would never reach its own deadline behaviour -- and
    stops accumulating. The overflow notice goes on stderr even when it was
    stdout that overflowed, because stdout is the guest transcript an agent
    writes ``stdout_contains`` against and must stay verbatim.
    """

    out: list[bytes] = field(default_factory=list)
    err: list[bytes] = field(default_factory=list)
    out_len: int = 0
    err_len: int = 0
    out_dropped: int = 0
    err_dropped: int = 0

    def add(self, which: str, data: bytes) -> None:
        if which == "out":
            room = MAX_CAPTURE_BYTES - self.out_len
            if room > 0:
                take = data[:room]
                self.out.append(take)
                self.out_len += len(take)
                data = data[len(take):]
            self.out_dropped += len(data)
        else:
            room = MAX_CAPTURE_BYTES - self.err_len
            if room > 0:
                take = data[:room]
                self.err.append(take)
                self.err_len += len(take)
                data = data[len(take):]
            self.err_dropped += len(data)

    def stdout(self) -> bytes:
        return b"".join(self.out)

    def stderr(self) -> bytes:
        s = b"".join(self.err)
        notes = []
        if self.out_dropped:
            notes.append(
                f"80mcp: stdout truncated at {MAX_CAPTURE_BYTES} bytes; "
                f"{self.out_dropped} more bytes were read and discarded".encode()
            )
        if self.err_dropped:
            notes.append(
                f"80mcp: stderr truncated at {MAX_CAPTURE_BYTES} bytes; "
                f"{self.err_dropped} more bytes were read and discarded".encode()
            )
        if notes:
            s = s + b"\n" + b"\n".join(notes) + b"\n"
        return s
