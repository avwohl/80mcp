"""The profile table, backend discovery, image resolution and ``80mcp doctor``.

This module is the data layer behind four things: ``x80_profiles``,
``x80_images``, the routing half of ``x80_probe``, and the ``80mcp doctor``
CLI table. It runs no guest code and owns no sandbox; ``tools.py`` wires it to
the verbs.

Four rules govern everything here.

**A missing backend is a string, never an exception.** SPEC.md 6.4 shows the
shape: ``blocked_by`` for ``mpm2`` on a stock macOS install reads *"mpm2_emu
links /usr/local/lib/libqkz80.4.dylib which is not installed; set
DYLD_LIBRARY_PATH or install libqkz80."* An agent can act on that. A traceback,
or a bare ``ready:false``, cannot be acted on. Every probe path in this file
ends in a ``Profile`` with ``ready`` set and ``blocked_by`` populated.

**Backends are found, not vendored.** Discovery order is: an explicit config
file, then environment variables, then ``PATH``, then the known sibling
checkout paths. Nothing in this package ships a binary, and nothing here builds
one.

**Dynamic-library resolution is part of "present".** ``mpm2_emu`` is on disk,
executable, and still dead: ``otool -L`` gives the hard install name
``/usr/local/lib/libqkz80.4.dylib``, which macOS does not ship, and running it
without ``DYLD_LIBRARY_PATH`` is **RC 134, a dyld abort** (measured on this
machine, 2026-09-04, against ``mpm2/build/mpm2_emu``). A probe that stats the
file and stops is wrong. :func:`missing_libraries` reads the load commands and
resolves each dependency the way dyld would.

**Only ``x80_images`` touches the network, and only when asked.**
:func:`_fetch_url` is the single outbound call in this package; it is called
from exactly one place, guarded by ``allow_fetch``, and every other code path
in this module reads the filesystem. SPEC.md 6.4: *"Never called implicitly --
no tool call may trigger a download."*

Two conventions the callers need to know:

``caps`` carries two namespaces in one flat list, because SPEC.md 6.4 spells
the field ``caps:[string]``. Entries beginning ``op:`` are MBP op names
(SPEC.md 5.3) that this build actually serves on that profile; everything else
is a feature string (``batch_run``, ``exit_code``, ``multi_console``,
``drives:A-P``). "Actually serves" is the SPEC.md 4.7 test, so a hardware
profile in a phase-1 build reports no ``op:`` entries at all and says why in
``blocked_by``.

An empty ``sha256`` on an :class:`~eightymcp.types.ImageRequirement` means *no
pin is available for this image*, not *the hash is zero*. Two images the
``x80_images`` schema names -- ``mpm2_system`` and ``freedos_starter`` -- are
built by their own checkouts and are not in the pinned ``romwbw_disks``
catalog, so this server cannot pin them. See :data:`LOCAL_IMAGES`.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import __version__
from .jsonrpc import LATEST_REVISION
from .mbp import REQUIRED_OPS
from .schemas import CPM_PROFILES, PROFILE_IDS, PROFILE_TIER
from .types import (
    DOSIZ_STDERR_NOISE,
    Escalation,
    ExternalServerRec,
    Family,
    Fidelity,
    ImageRequirement,
    Profile,
    ProfileImages,
    ProfilesResult,
    ProbeVerdict,
    SyscallLayer,
    Tier,
    ToolError,
    ToolExecutionError,
    UnimplementedCall,
)

__all__ = [
    # configuration and discovery
    "Config",
    "BackendSpec",
    "BackendProbe",
    "BACKENDS",
    "missing_libraries",
    "NO_SIBLING_SEARCH_ENV",
    "shared_library_dependencies",
    # the profile table
    "ProfileSpec",
    "PROFILE_TABLE",
    "SERVER_PHASE",
    "ADAPTER_PHASE",
    "EXTERNAL_SERVERS",
    # images
    "CatalogImage",
    "ImageLocation",
    "ImageCatalog",
    "LOCAL_IMAGES",
    "DEFAULT_ROMWBW_VERSION",
    # the probe
    "Prober",
    "profiles_result",
    "doctor_report",
    # x80_probe routing data
    "CHEAPEST_PROFILE",
    "BDOS_FUNCTIONS",
    "XDOS_FUNCTIONS",
    "INT21_FUNCTIONS",
    "bdos_call",
    "int21_call",
    "classify_probe",
    "detect_family",
]


# ==========================================================================
# 0. Small process and filesystem helpers
# ==========================================================================

#: Wall-clock ceiling on any probe subprocess. A backend that has not printed
#: its version banner in three seconds is not a backend this server can use
#: inside a tool call, and `x80_profiles` must answer rather than hang.
PROBE_TIMEOUT_S: float = 3.0

#: Distinguishes "asked and got nothing" from "never asked" in the memos.
_UNPROBED: object = object()

_HASH_BLOCK = 1 << 20


#: Where a probe child's ``HOME`` and XDG roots point. Created on first use and
#: removed at interpreter exit; empty, because nothing is supposed to write
#: there and a probe that does is a bug this makes visible instead of silent.
_probe_home: Path | None = None


def _probe_home_dir() -> Path:
    global _probe_home
    if _probe_home is None:
        import atexit
        import tempfile as _tempfile

        root = Path(_tempfile.mkdtemp(prefix="80mcp-probe-"))
        for name in ("home", "xdg", "cache", "data", "state"):
            (root / name).mkdir(mode=0o700, exist_ok=True)
        atexit.register(shutil.rmtree, root, True)
        _probe_home = root
    return _probe_home


def _hermetic(env: Mapping[str, str] | None) -> dict[str, str]:
    """Invariant 1, applied to the probe path as well as to the run path.

    SPEC.md 5.4 Invariant 1 and Appendix B item 1 are about *every* launch, and
    a version probe is a launch: ``romwbw_emu``'s legacy-NVRAM fallback
    (``romwbw_emu.cc:1379-1390``, reached through ``get_legacy_nvram_path()``
    at ``:79-82``) is built from ``$HOME`` unconditionally and the migration it
    performs *writes*. ``x80_profiles`` is annotated ``readOnlyHint:true``, so
    a probe that can touch ``~/.config/romwbw_emu/nvram`` is the read-only tool
    mutating the developer's install.

    Measured on this machine: ``romwbw_emu --version`` v1.38 exits before it
    reaches that code and leaves the file alone, so this is the fence and not
    the fix for a live bug. It costs one ``mkdtemp`` per process.

    The caller's ``env`` is merged first -- backend probes legitimately pass a
    whole environment to carry ``DYLD_LIBRARY_PATH`` -- and the four hermetic
    keys are asserted last, so nothing a caller passes can undo them.
    """
    full = dict(os.environ)
    if env:
        full.update(env)
    home = _probe_home_dir()
    full["HOME"] = str(home / "home")
    full["XDG_CONFIG_HOME"] = str(home / "xdg")
    full["XDG_CACHE_HOME"] = str(home / "cache")
    full["XDG_DATA_HOME"] = str(home / "data")
    full["XDG_STATE_HOME"] = str(home / "state")
    return full


def _run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = PROBE_TIMEOUT_S,
) -> tuple[int | None, str, str]:
    """Run a probe command. Returns ``(rc, stdout, stderr)``; ``rc`` is None on
    timeout. Never raises for a missing or non-executable file.

    The child's environment is :func:`_hermetic`: whatever the caller passed,
    with ``HOME`` and the XDG roots pinned inside a throwaway directory.
    """
    full = _hermetic(env)
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            timeout=timeout,
            env=full,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            return None, "", "timed out after %.1fs" % timeout
        return None, "", str(exc)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_BLOCK), b""):
            h.update(chunk)
    return h.hexdigest()


def _xdg(var: str, default_rel: str, environ: Mapping[str, str]) -> Path:
    raw = environ.get(var)
    if raw:
        return Path(raw)
    return Path(environ.get("HOME", str(Path.home()))) / default_rel


def _is_exe(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


#: The directory this checkout's siblings live in, when 80mcp is being run out
#: of a source tree. ``__file__`` is ``<root>/src/eightymcp/profiles.py``, so
#: ``parents[3]`` is the directory holding the ``80mcp`` checkout. Harmless and
#: simply absent when installed into site-packages.
_CHECKOUT_SIBLING_ROOT: Path = Path(__file__).resolve().parents[3]


#: Set this to any non-empty value to switch the sibling-checkout search off
#: entirely, leaving config file / environment variable / PATH. Wanted by two
#: callers: a hermetic deployment that must never pick up a stray build tree,
#: and this package's own tests, which have to be able to assert what a machine
#: with nothing installed reports.
NO_SIBLING_SEARCH_ENV = "EIGHTYMCP_NO_SIBLING_SEARCH"


def _sibling_roots(environ: Mapping[str, str]) -> list[Path]:
    if environ.get(NO_SIBLING_SEARCH_ENV):
        return []
    home = Path(environ.get("HOME", str(Path.home())))
    roots: list[Path] = []
    for cand in (_CHECKOUT_SIBLING_ROOT, home / "src", home / "Projects", home):
        if cand not in roots:
            roots.append(cand)
    return roots


# ==========================================================================
# 1. Configuration
# ==========================================================================

#: Environment variable naming an explicit config file. Nothing else in this
#: package reads a dotfile.
CONFIG_ENV = "EIGHTYMCP_CONFIG"


@dataclass(slots=True)
class Config:
    """The optional ``80mcp`` config file, plus the environment it was read in.

    JSON, and every key is optional::

        {
          "backends": {"dosiz": {"path": "/opt/dosiz",
                                 "env": {"DYLD_LIBRARY_PATH": "/opt/lib"}}},
          "image_dirs": ["/srv/romwbw"],
          "catalog_dirs": ["/srv/romwbw_disks/catalog/v0"],
          "romwbw_version": "3.5.1"
        }

    Searched, in order: ``$EIGHTYMCP_CONFIG``, then
    ``$XDG_CONFIG_HOME/80mcp/config.json`` (default
    ``~/.config/80mcp/config.json``). A malformed file is a problem the caller
    is told about -- :attr:`problems` -- not an exception and not a silent
    default, because "the server ignored my config" is the hardest support
    question in this domain.
    """

    path: str | None = None
    backends: dict[str, dict[str, Any]] = field(default_factory=dict)
    image_dirs: list[str] = field(default_factory=list)
    catalog_dirs: list[str] = field(default_factory=list)
    romwbw_version: str | None = None
    problems: list[str] = field(default_factory=list)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Config":
        env = dict(os.environ if environ is None else environ)
        candidates: list[Path] = []
        if path is not None:
            candidates.append(Path(path))
        elif env.get(CONFIG_ENV):
            candidates.append(Path(env[CONFIG_ENV]))
        else:
            candidates.append(
                _xdg("XDG_CONFIG_HOME", ".config", env) / "80mcp" / "config.json"
            )

        cfg = cls(environ=env)
        for cand in candidates:
            if not cand.is_file():
                # An explicitly named file that is not there is a problem; the
                # default location simply not existing is not.
                if path is not None or env.get(CONFIG_ENV):
                    cfg.problems.append(
                        f"config file {cand} does not exist "
                        f"(named by {CONFIG_ENV} or the caller)"
                    )
                continue
            try:
                raw = json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                cfg.problems.append(f"config file {cand} could not be read: {exc}")
                continue
            if not isinstance(raw, dict):
                cfg.problems.append(
                    f"config file {cand} must hold a JSON object, "
                    f"found {type(raw).__name__}"
                )
                continue
            cfg.path = str(cand)
            backends = raw.get("backends") or {}
            if isinstance(backends, dict):
                cfg.backends = {
                    str(k): dict(v) for k, v in backends.items() if isinstance(v, dict)
                }
            else:
                cfg.problems.append(f"config file {cand}: 'backends' must be an object")
            for key, attr in (
                ("image_dirs", "image_dirs"),
                ("catalog_dirs", "catalog_dirs"),
            ):
                val = raw.get(key)
                if val is None:
                    continue
                if isinstance(val, list) and all(isinstance(x, str) for x in val):
                    setattr(cfg, attr, list(val))
                else:
                    cfg.problems.append(
                        f"config file {cand}: '{key}' must be an array of strings"
                    )
            ver = raw.get("romwbw_version")
            if isinstance(ver, str):
                cfg.romwbw_version = ver
            elif ver is not None:
                cfg.problems.append(
                    f"config file {cand}: 'romwbw_version' must be a string"
                )
            break
        return cfg

    # -- lookups ----------------------------------------------------------

    def backend_path(self, name: str) -> str | None:
        entry = self.backends.get(name) or {}
        val = entry.get("path")
        return str(val) if isinstance(val, str) and val else None

    def backend_env(self, name: str) -> dict[str, str]:
        entry = self.backends.get(name) or {}
        val = entry.get("env")
        if isinstance(val, dict):
            return {str(k): str(v) for k, v in val.items()}
        return {}


# ==========================================================================
# 2. Backend discovery
# ==========================================================================

@dataclass(frozen=True, slots=True)
class BackendSpec:
    """How to find one backend binary and how to ask it its version.

    ``version_argv`` is empty for the two backends that have no version flag,
    both measured on this machine on 2026-09-04:

    * ``cpmemu --version`` prints ``CPU mode: Z80`` then ``Cannot open
      --version: No such file or directory`` -- it takes the flag as the
      program to load.
    * ``mpm2_emu --version`` prints ``mpm2_emu: unrecognized option
      `--version'`` and the usage block.

    For those two the version comes from the checkout the binary was built in
    (``VERSION``, else the newest ``## [x.y.z]`` heading in ``CHANGELOG.md``),
    and :attr:`BackendProbe.version_source` says so.
    """

    name: str
    #: Executable leaf names to try on PATH, in order.
    leaves: tuple[str, ...]
    #: Environment variable naming the binary outright.
    env_var: str
    #: Paths relative to each sibling-checkout root.
    sibling_paths: tuple[str, ...]
    #: Argv after the binary that prints a version, or () when there is none.
    version_argv: tuple[str, ...] = ()
    #: Regex with one group, matched against stdout then stderr.
    version_pattern: str = r"v?([0-9]+\.[0-9][^\s,)]*)"
    #: Which upstream checkout carries this binary, for the fallback version
    #: read and for the "clone and build" hint.
    checkout: str = ""


BACKENDS: dict[str, BackendSpec] = {
    "cpmemu": BackendSpec(
        name="cpmemu",
        leaves=("cpmemu",),
        env_var="EIGHTYMCP_CPMEMU",
        sibling_paths=("cpmemu/src/cpmemu", "cpmemu/build/cpmemu"),
        version_argv=(),  # measured: no --version flag, see BackendSpec
        checkout="cpmemu",
    ),
    "dosiz": BackendSpec(
        name="dosiz",
        leaves=("dosiz",),
        env_var="EIGHTYMCP_DOSIZ",
        sibling_paths=(
            "qxDOS/build/dosiz",
            "qxDOS/build-dosiz/dosiz",
            "dosiz/build/dosiz",
        ),
        version_argv=("--version",),
        version_pattern=r"dosiz\s+(\S+)",
        checkout="qxDOS",
    ),
    "romwbw_emu": BackendSpec(
        name="romwbw_emu",
        leaves=("romwbw_emu",),
        env_var="EIGHTYMCP_ROMWBW_EMU",
        sibling_paths=("romwbw_emu/src/romwbw_emu", "romwbw_emu/build/romwbw_emu"),
        version_argv=("--version",),
        version_pattern=r"RomWBW Emulator v(\S+)",
        checkout="romwbw_emu",
    ),
    "mpm2_emu": BackendSpec(
        name="mpm2_emu",
        leaves=("mpm2_emu",),
        env_var="EIGHTYMCP_MPM2_EMU",
        sibling_paths=("mpm2/build/mpm2_emu", "mpm2/src/mpm2_emu"),
        version_argv=(),  # measured: unrecognized option `--version'
        checkout="mpm2",
    ),
    "emu88d": BackendSpec(
        name="emu88d",
        leaves=("emu88d",),
        env_var="EIGHTYMCP_EMU88D",
        sibling_paths=("qxDOS/build/emu88d", "qxDOS/build-emu88d/emu88d"),
        version_argv=("--version",),
        checkout="qxDOS",
    ),
}


# -- dynamic library resolution --------------------------------------------

_OTOOL_DEP = re.compile(r"^\t(\S.*?)\s+\(compatibility version")
_OTOOL_RPATH = re.compile(r"^\s*path\s+(.+?)\s+\(offset \d+\)\s*$")
_LDD_MISSING = re.compile(r"^\s*(\S+)\s*=>\s*not found")


def shared_library_dependencies(binary: str | os.PathLike[str]) -> list[str]:
    """Every shared-library install name the binary records, verbatim.

    macOS: the ``otool -L`` dependency list, minus the binary's own line.
    Linux: parsed out of ``ldd``. Anywhere else, or with no tool installed,
    an empty list -- the caller treats that as "not checked", not as "clean".
    """
    binary = str(binary)
    if sys.platform == "darwin":
        if not shutil.which("otool"):
            return []
        rc, out, _ = _run(["otool", "-L", binary])
        if rc != 0:
            return []
        deps: list[str] = []
        for line in out.splitlines():
            m = _OTOOL_DEP.match(line)
            if m and m.group(1) != binary:
                deps.append(m.group(1))
        return deps
    if sys.platform.startswith("linux"):
        if not shutil.which("ldd"):
            return []
        rc, out, err = _run(["ldd", binary])
        deps = []
        for line in (out + err).splitlines():
            part = line.strip().split(" =>", 1)[0].strip()
            if part and not part.startswith("linux-vdso") and "statically" not in part:
                deps.append(part)
        return deps
    return []


def _rpaths(binary: str) -> list[str]:
    if sys.platform != "darwin" or not shutil.which("otool"):
        return []
    rc, out, _ = _run(["otool", "-l", binary])
    if rc != 0:
        return []
    paths: list[str] = []
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "cmd LC_RPATH":
            for follow in lines[i + 1 : i + 5]:
                m = _OTOOL_RPATH.match(follow)
                if m:
                    paths.append(m.group(1))
                    break
    return paths


def missing_libraries(
    binary: str | os.PathLike[str], env: Mapping[str, str] | None = None
) -> list[str]:
    """Dependencies dyld (or ld.so) would fail to find, as recorded install names.

    This is the check that separates ``mpm2_emu`` from a working ``mpm2_emu``.
    ``otool -L`` on it gives the hard install name
    ``/usr/local/lib/libqkz80.4.dylib``; macOS does not ship that file, and
    running the binary without ``DYLD_LIBRARY_PATH`` aborts under dyld with
    **RC 134** (measured 2026-09-04). Statting the executable and reading its
    mode bits reports it as present and it is not.

    Resolution mirrors dyld: ``DYLD_LIBRARY_PATH`` entries are searched by leaf
    name first and override the recorded directory, then the recorded path
    itself, then ``DYLD_FALLBACK_LIBRARY_PATH``. ``@loader_path`` and
    ``@executable_path`` resolve against the binary's directory;
    ``@rpath`` against the binary's ``LC_RPATH`` entries.
    """
    binary = str(binary)
    e = dict(os.environ if env is None else env)
    if sys.platform.startswith("linux"):
        if not shutil.which("ldd"):
            return []
        rc, out, err = _run(["ldd", binary], env=e)
        return [
            m.group(1)
            for m in (_LDD_MISSING.match(ln) for ln in (out + err).splitlines())
            if m
        ]
    if sys.platform != "darwin":
        return []

    def split(var: str) -> list[str]:
        return [p for p in e.get(var, "").split(":") if p]

    dyld_paths = split("DYLD_LIBRARY_PATH")
    fallback = split("DYLD_FALLBACK_LIBRARY_PATH") or [
        str(Path(e.get("HOME", "~")) / "lib"),
        "/usr/local/lib",
        "/usr/lib",
    ]
    bindir = str(Path(binary).resolve().parent)
    rpaths = _rpaths(binary)

    missing: list[str] = []
    for dep in shared_library_dependencies(binary):
        leaf = dep.rsplit("/", 1)[-1]
        cands: list[str] = []
        if dep.startswith("@rpath/"):
            tail = dep[len("@rpath/") :]
            for rp in rpaths:
                rp = rp.replace("@loader_path", bindir).replace(
                    "@executable_path", bindir
                )
                cands.append(str(Path(rp) / tail))
        elif dep.startswith("@loader_path") or dep.startswith("@executable_path"):
            cands.append(
                dep.replace("@loader_path", bindir).replace("@executable_path", bindir)
            )
        else:
            cands.extend(str(Path(d) / leaf) for d in dyld_paths)
            cands.append(dep)
            cands.extend(str(Path(d) / leaf) for d in fallback)
        # The dyld shared cache holds the system libraries with no file on
        # disk; anything under /usr/lib or /System is resolved by the cache.
        if dep.startswith("/usr/lib/") or dep.startswith("/System/"):
            continue
        if not any(Path(c).exists() for c in cands):
            missing.append(dep)
    return missing


def _library_hint(dep: str) -> str:
    """The stem an operator would install, e.g. ``libqkz80.4.dylib`` -> ``libqkz80``."""
    leaf = dep.rsplit("/", 1)[-1]
    for suffix in (".dylib", ".so"):
        if suffix in leaf:
            leaf = leaf.split(suffix, 1)[0]
            break
    return leaf.split(".", 1)[0]


def _find_library_on_disk(
    dep: str, environ: Mapping[str, str]
) -> str | None:
    """Where a missing dependency actually lives, if we can see it.

    Offered as a hint, never applied. SPEC.md 8 item 14 records that the real
    fix for ``mpm2_emu`` is an rpath or a static link; a server that silently
    guesses ``DYLD_LIBRARY_PATH`` hides the defect and makes the next install
    fail somewhere less obvious.
    """
    leaf = dep.rsplit("/", 1)[-1]
    for root in _sibling_roots(environ):
        for sub in ("cpmemu/src", "cpmemu/build", "lib", "usr/local/lib"):
            cand = root / sub / leaf
            if cand.exists():
                return str(cand.parent)
    return None


_CHANGELOG_VERSION = re.compile(r"^##\s*\[?v?([0-9][^\]\s]*)\]?", re.M)


def _version_from_checkout(binary: Path) -> tuple[str | None, str | None]:
    """``(version, source)`` read from the checkout the binary was built in.

    Walks up from the binary looking for a ``VERSION`` file, then for the
    newest ``## [x.y.z]`` heading in a ``CHANGELOG.md``. Used only for the two
    backends with no version flag.
    """
    for parent in list(binary.resolve().parents)[:4]:
        vf = parent / "VERSION"
        if vf.is_file():
            try:
                text = vf.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            except (OSError, IndexError):
                text = ""
            if text:
                return text, f"{vf}"
        cl = parent / "CHANGELOG.md"
        if cl.is_file():
            try:
                m = _CHANGELOG_VERSION.search(cl.read_text(encoding="utf-8"))
            except OSError:
                m = None
            if m:
                return m.group(1), f"{cl} (newest heading)"
    return None, None


@dataclass(slots=True)
class BackendProbe:
    """What discovery found for one backend binary."""

    name: str
    path: str | None = None
    found: bool = False
    version: str | None = None
    #: How :attr:`version` was obtained: the argv that printed it, a file path,
    #: or None when no version could be established.
    version_source: str | None = None
    #: Extra environment this backend needs. Only ever populated from the
    #: config file or an explicit environment variable, never guessed.
    env: dict[str, str] = field(default_factory=dict)
    #: Recorded install names that will not resolve. Empty also means "not
    #: checked" on a platform with no otool/ldd; :attr:`library_check` says which.
    missing_libraries: list[str] = field(default_factory=list)
    library_check: str = "not_checked"  # "ok" | "missing" | "not_checked"
    #: Actionable strings, ready to become `blocked_by` entries.
    problems: list[str] = field(default_factory=list)
    #: Every location tried, in order, so "why did it not find mine" is answerable.
    searched: list[str] = field(default_factory=list)
    #: How it was found: "config" | "env" | "path" | "sibling" | None.
    via: str | None = None

    @property
    def usable(self) -> bool:
        return self.found and not self.missing_libraries

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "found": self.found,
            "via": self.via,
            "version": self.version,
            "version_source": self.version_source,
            "env": dict(self.env),
            "library_check": self.library_check,
            "missing_libraries": list(self.missing_libraries),
            "problems": list(self.problems),
            "searched": list(self.searched),
        }


def _probe_backend(
    spec: BackendSpec, config: Config, environ: Mapping[str, str]
) -> BackendProbe:
    p = BackendProbe(name=spec.name)
    p.env.update(config.backend_env(spec.name))

    candidates: list[tuple[str, Path]] = []
    cfg_path = config.backend_path(spec.name)
    if cfg_path:
        candidates.append(("config", Path(cfg_path)))
    env_path = environ.get(spec.env_var)
    if env_path:
        candidates.append(("env", Path(env_path)))
    for leaf in spec.leaves:
        hit = shutil.which(leaf, path=environ.get("PATH"))
        if hit:
            candidates.append(("path", Path(hit)))
    for root in _sibling_roots(environ):
        for rel in spec.sibling_paths:
            candidates.append(("sibling", root / rel))

    for via, cand in candidates:
        p.searched.append(str(cand))
        if _is_exe(cand):
            p.path = str(cand)
            p.found = True
            p.via = via
            break
        if cand.is_file():
            p.problems.append(f"{cand} exists but is not executable")

    if not p.found:
        hint = f"; clone and build avwohl/{spec.checkout}" if spec.checkout else ""
        p.problems.append(
            f"{spec.name} was not found on PATH, in {spec.env_var}, "
            f"in the config file, or in a sibling checkout{hint}"
        )
        return p

    # Dynamic-library resolution, before any attempt to run it: an unresolved
    # dependency is why the version probe below would return RC 134 rather
    # than a version string.
    if sys.platform in ("darwin",) or sys.platform.startswith("linux"):
        deps = shared_library_dependencies(p.path)
        if deps:
            probe_env = dict(environ)
            probe_env.update(p.env)
            p.missing_libraries = missing_libraries(p.path, probe_env)
            p.library_check = "missing" if p.missing_libraries else "ok"
    for dep in p.missing_libraries:
        # SPEC.md 6.4 gives this string verbatim for the measured mpm2 case.
        p.problems.append(
            f"{spec.name} links {dep} which is not installed; "
            f"set DYLD_LIBRARY_PATH or install {_library_hint(dep)}."
            if sys.platform == "darwin"
            else f"{spec.name} links {dep} which is not installed; "
            f"set LD_LIBRARY_PATH or install {_library_hint(dep)}."
        )
        where = _find_library_on_disk(dep, environ)
        if where:
            var = (
                "DYLD_LIBRARY_PATH"
                if sys.platform == "darwin"
                else "LD_LIBRARY_PATH"
            )
            p.problems.append(
                f"{dep.rsplit('/', 1)[-1]} is present at {where}; "
                f"{var}={where} would resolve it. This server does not set it "
                f"for you -- put it in the config file's "
                f"backends.{spec.name}.env, because SPEC.md 8 item 14 records "
                f"the real fix as an rpath or a static link upstream."
            )

    if p.missing_libraries:
        return p

    if spec.version_argv:
        rc, out, err = _run(
            [p.path, *spec.version_argv], env={**environ, **p.env}
        )
        blob = out + "\n" + err
        m = re.search(spec.version_pattern, blob)
        if m:
            p.version = m.group(1)
            p.version_source = " ".join(spec.version_argv)
        elif rc is None:
            p.problems.append(
                f"{spec.name} {' '.join(spec.version_argv)} did not answer within "
                f"{PROBE_TIMEOUT_S:.0f}s"
            )
    if p.version is None:
        ver, src = _version_from_checkout(Path(p.path))
        if ver:
            p.version, p.version_source = ver, src
    return p


# ==========================================================================
# 3. Images: the pinned romwbw_disks catalog, plus two locally built images
# ==========================================================================

#: The catalog generation `romwbw_emu` is pinned to. `romwbw_emu --version`
#: prints "RomWBW compatibility: v3.5.1 (pinned)" and
#: `emu_validate_rom_hcb` refuses a mismatched ROM, so this is the default
#: rather than the newest catalog.
DEFAULT_ROMWBW_VERSION = "3.5.1"

#: Where `x80_images` writes fetched images, and the first place every other
#: tool looks for them.
def image_cache_dir(version: str, environ: Mapping[str, str] | None = None) -> Path:
    env = dict(os.environ if environ is None else environ)
    return _xdg("XDG_CACHE_HOME", ".cache", env) / "80mcp" / "images" / version


@dataclass(slots=True)
class CatalogImage:
    """One ROM or disk the catalog pins."""

    id: str
    kind: str  # "rom" | "disk"
    filename: str
    bytes: int
    sha256: str
    licence: str
    description: str
    url: str | None = None
    #: "catalog" for a pinned romwbw_disks entry, "local_build" for one that
    #: is built by its own checkout and cannot be pinned here.
    source: str = "catalog"
    #: Other filenames this image is known by on disk, e.g. the unversioned
    #: `hd1k_combo.img` that ships inside the romwbw_emu checkout.
    aliases: tuple[str, ...] = ()

    def candidate_names(self) -> tuple[str, ...]:
        return (self.filename, *self.aliases)


#: The two ids the `x80_images` schema names that the pinned catalog does not
#: carry. Both are produced by their own checkout's build script, so this
#: server has no sha256 to pin and says so with an empty `sha256`.
LOCAL_IMAGES: dict[str, CatalogImage] = {
    "mpm2_system": CatalogImage(
        id="mpm2_system",
        kind="disk",
        filename="mpm2_system.img",
        bytes=0,
        sha256="",
        licence="Mixed",
        description=(
            "MP/M II V2.1 system disk, built by the avwohl/mpm2 checkout "
            "(disks/mpm2_system.img). Not in the pinned romwbw_disks catalog, "
            "so this server cannot pin its sha256 and does not fetch it. "
            "Images built --tree=src from DRI source and images carrying "
            "original DRI binaries are separate artefacts in that repo."
        ),
        source="local_build",
        aliases=("mpm2_system.img", "mpm2.img"),
    ),
    "freedos_starter": CatalogImage(
        id="freedos_starter",
        kind="disk",
        filename="freedos_hd.img",
        bytes=0,
        sha256="",
        licence="GPL-2.0 (FreeDOS kernel) / Mixed",
        description=(
            "FreeDOS starter disk, built by qxDOS scripts/build_starter_disk.sh. "
            "Not in the pinned romwbw_disks catalog. SPEC.md 3.7: the freedos "
            "profile stays unavailable until one CI job builds this and boots "
            "it headless under emu88d."
        ),
        source="local_build",
        aliases=("freedos_hd.img", "freedos_starter.img"),
    ),
}


@dataclass(slots=True)
class ImageLocation:
    """The outcome of looking for one catalog image on this filesystem."""

    image_id: str
    path: str | None = None
    sha256: str | None = None
    #: True only when a file was found whose sha256 equals the catalog pin.
    verified: bool = False
    #: Files that carried the right name and the wrong bytes, with their hash.
    #: This is how a mutated working copy gets reported instead of silently
    #: used: `romwbw_emu/disks/hd1k_combo.img` on this machine hashes
    #: 723d04f9... against the pinned 0ca4ec60... (measured 2026-09-04).
    rejected: list[tuple[str, str]] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return self.verified


class ImageCatalog:
    """The pinned ``romwbw_disks`` catalog for one RomWBW generation, plus
    :data:`LOCAL_IMAGES`, plus where each one actually is on this filesystem.

    Loading is filesystem-only. The catalog directory is searched in this
    order: the config file's ``catalog_dirs``, ``$EIGHTYMCP_CATALOG_DIR``,
    ``<sibling>/romwbw_disks/catalog/v0``, then the fetch cache. Nothing is
    downloaded to build one -- a machine with no ``romwbw_disks`` checkout gets
    an empty catalog and an actionable note, which is what "phase 1 is
    offline-safe" means.
    """

    def __init__(
        self,
        *,
        version: str = DEFAULT_ROMWBW_VERSION,
        images: Mapping[str, CatalogImage] | None = None,
        catalog_path: str | None = None,
        upstream_package_sha256: str | None = None,
        notes: Iterable[str] = (),
        image_dirs: Sequence[Path] = (),
        problems: Iterable[str] = (),
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.environ: dict[str, str] = dict(
            os.environ if environ is None else environ
        )
        self.version = version
        self.images: dict[str, CatalogImage] = dict(images or {})
        for iid, img in LOCAL_IMAGES.items():
            self.images.setdefault(iid, img)
        self.catalog_path = catalog_path
        self.upstream_package_sha256 = upstream_package_sha256
        self.notes: list[str] = list(notes)
        self.image_dirs: list[Path] = list(image_dirs)
        self.problems: list[str] = list(problems)
        self._located: dict[str, ImageLocation] = {}
        self._hashes: dict[tuple[str, int, int], str] = {}

    # -- construction -----------------------------------------------------

    @classmethod
    def load(
        cls,
        config: Config | None = None,
        *,
        version: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "ImageCatalog":
        cfg = config or Config.load(environ=environ)
        env = dict(cfg.environ if environ is None else environ)
        ver = version or cfg.romwbw_version or DEFAULT_ROMWBW_VERSION

        catalog_dirs: list[Path] = [Path(p) for p in cfg.catalog_dirs]
        for raw in env.get("EIGHTYMCP_CATALOG_DIR", "").split(os.pathsep):
            if raw:
                catalog_dirs.append(Path(raw))
        for root in _sibling_roots(env):
            catalog_dirs.append(root / "romwbw_disks" / "catalog" / "v0")
        catalog_dirs.append(
            _xdg("XDG_CACHE_HOME", ".cache", env) / "80mcp" / "catalog" / "v0"
        )

        problems: list[str] = []
        parsed: dict[str, CatalogImage] = {}
        catalog_path: str | None = None
        upstream: str | None = None
        notes: list[str] = []

        for d in catalog_dirs:
            cand = d / ver / "catalog.json"
            if not cand.is_file():
                # The release artefact carries the version in its name.
                cand = d / f"catalog-v0-{ver}.json"
            if not cand.is_file():
                continue
            try:
                raw = json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                problems.append(f"catalog {cand} could not be read: {exc}")
                continue
            catalog_path = str(cand)
            upstream = (raw.get("upstream") or {}).get("package_sha256")
            notes = list(raw.get("notes") or [])
            base = raw.get("base_url") or ""
            for kind, key in (("rom", "roms"), ("disk", "disks")):
                for e in raw.get(key) or []:
                    iid = e.get("id")
                    fn = e.get("filename")
                    if not iid or not fn:
                        continue
                    parsed[iid] = CatalogImage(
                        id=iid,
                        kind=kind,
                        filename=fn,
                        bytes=int(e.get("size") or 0),
                        sha256=str(e.get("sha256") or ""),
                        # The catalog spells it "license"; SPEC.md 6.4's output
                        # key is "licence" and the value is surfaced verbatim.
                        licence=str(e.get("license") or ""),
                        description=str(e.get("description") or ""),
                        url=(base + fn) if base else None,
                        aliases=(f"{iid}.{'rom' if kind == 'rom' else 'img'}",),
                    )
            break

        if catalog_path is None:
            problems.append(
                f"no romwbw_disks catalog for RomWBW {ver} was found. Looked in: "
                + ", ".join(str(d) for d in catalog_dirs)
                + ". Clone avwohl/romwbw_disks beside this checkout, set "
                "EIGHTYMCP_CATALOG_DIR, or call x80_images with "
                "dry_run:false and allow_fetch:true to fetch it."
            )

        image_dirs: list[Path] = [Path(p) for p in cfg.image_dirs]
        for raw in env.get("EIGHTYMCP_IMAGE_DIR", "").split(os.pathsep):
            if raw:
                image_dirs.append(Path(raw))
        image_dirs.append(image_cache_dir(ver, env))
        for root in _sibling_roots(env):
            image_dirs.extend(
                [
                    root / "romwbw_disks" / "build" / f"v0-romwbw-{ver}",
                    root / "romwbw_emu" / "roms",
                    root / "romwbw_emu" / "disks",
                    root / "mpm2" / "disks",
                    root / "qxDOS" / "images",
                    root / "qxDOS" / "build",
                ]
            )

        return cls(
            version=ver,
            images=parsed,
            catalog_path=catalog_path,
            upstream_package_sha256=upstream,
            notes=notes,
            image_dirs=image_dirs,
            problems=problems,
            environ=env,
        )

    # -- lookup -----------------------------------------------------------

    def _sha(self, path: Path) -> str:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key not in self._hashes:
            self._hashes[key] = sha256_file(path)
        return self._hashes[key]

    def locate(self, image_id: str) -> ImageLocation:
        """Find ``image_id`` on this filesystem and verify it against the pin.

        Memoized per catalog instance. A file whose name matches and whose
        bytes do not is recorded in :attr:`ImageLocation.rejected` rather than
        used, because a mutated disk image produces a transcript an agent will
        misread.
        """
        if image_id in self._located:
            return self._located[image_id]
        loc = ImageLocation(image_id=image_id)
        img = self.images.get(image_id)
        if img is None:
            loc.searched.append(f"(no catalog entry for {image_id!r})")
            self._located[image_id] = loc
            return loc
        for d in self.image_dirs:
            for name in img.candidate_names():
                cand = d / name
                loc.searched.append(str(cand))
                if not cand.is_file():
                    continue
                if img.bytes and cand.stat().st_size != img.bytes:
                    loc.rejected.append((str(cand), f"size {cand.stat().st_size}"))
                    continue
                got = self._sha(cand)
                if not img.sha256:
                    # LOCAL_IMAGES: nothing to compare against. Report the
                    # measured hash and treat the file as present, with the
                    # empty pin already saying it is unpinned.
                    loc.path, loc.sha256, loc.verified = str(cand), got, True
                    self._located[image_id] = loc
                    return loc
                if got == img.sha256:
                    loc.path, loc.sha256, loc.verified = str(cand), got, True
                    self._located[image_id] = loc
                    return loc
                loc.rejected.append((str(cand), got))
        self._located[image_id] = loc
        return loc

    def requirement(self, image_id: str) -> ImageRequirement:
        img = self.images.get(image_id)
        loc = self.locate(image_id)
        return ImageRequirement(
            id=image_id,
            sha256=(img.sha256 if img else ""),
            present=loc.present,
        )


# -- the one network call ---------------------------------------------------

#: The only tool permitted to reach the network, named here so grepping for it
#: finds the boundary. SPEC.md 6.4: every other tool is openWorldHint:false.
NETWORK_TOOL = "x80_images"


def _fetch_url(url: str, dest: Path, timeout_ms: int) -> int:
    """THE ONLY OUTBOUND NETWORK CALL IN THIS PACKAGE.

    Called from exactly one place: :func:`resolve_images`, behind
    ``dry_run:false`` **and** ``allow_fetch:true``. ``urllib`` is imported here
    rather than at module scope so that importing ``eightymcp.profiles`` cannot
    pull in the network stack at all.
    """
    import urllib.request  # noqa: PLC0415 - deliberately local, see docstring

    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with urllib.request.urlopen(url, timeout=timeout_ms / 1000.0) as resp:  # noqa: S310
        with open(dest, "wb") as fh:
            for chunk in iter(lambda: resp.read(_HASH_BLOCK), b""):
                fh.write(chunk)
                written += len(chunk)
    return written


def resolve_images(
    image_ids: Sequence[str] | None = None,
    *,
    romwbw_version: str | None = None,
    dry_run: bool = True,
    allow_fetch: bool = False,
    timeout_ms: int = 120000,
    config: Config | None = None,
    environ: Mapping[str, str] | None = None,
    catalog: ImageCatalog | None = None,
) -> dict[str, Any]:
    """``x80_images``'s result body. SPEC.md 6.4.

    ``image_ids`` omitted lists the whole catalog without fetching anything.
    A fetch needs both ``dry_run:false`` and ``allow_fetch:true``; without the
    second this raises :class:`ToolExecutionError`, which the server turns into
    an ``isError`` result, because SPEC.md 6.4 says there is no elicitation.
    """
    cat = catalog or ImageCatalog.load(
        config, version=romwbw_version, environ=environ
    )
    notes = list(cat.notes) + list(cat.problems)
    if cat.catalog_path:
        notes.append(f"catalog read from {cat.catalog_path} (no network access)")

    ids = list(image_ids) if image_ids else sorted(cat.images)
    unknown = [i for i in ids if i not in cat.images]
    if unknown:
        raise ToolExecutionError(
            ToolError.bad_argument(
                "image_ids",
                "unknown catalog id(s)",
                unknown=unknown,
                known=sorted(cat.images),
                catalog_version=cat.version,
            )
        )

    fetching = bool(image_ids) and not dry_run
    if fetching and not allow_fetch:
        raise ToolExecutionError(
            ToolError(
                error="fetch_not_allowed",
                fields={
                    "reason": (
                        "dry_run:false needs allow_fetch:true. x80_images is the "
                        "only tool in this server that touches the network and it "
                        "is never called implicitly."
                    ),
                    "image_ids": ids,
                    "hint": 'retry with {"dry_run": false, "allow_fetch": true}',
                },
            )
        )

    out: list[dict[str, Any]] = []
    for iid in ids:
        img = cat.images[iid]
        loc = cat.locate(iid)
        fetched = False
        if fetching and not loc.present:
            if img.source != "catalog" or not img.url:
                notes.append(
                    f"{iid} is built locally, not distributed: {img.description}"
                )
            else:
                dest = image_cache_dir(cat.version, cat.environ) / img.filename
                try:
                    _fetch_url(img.url, dest, timeout_ms)
                except Exception as exc:  # network, DNS, HTTP, disk
                    raise ToolExecutionError(
                        ToolError(
                            error="fetch_failed",
                            fields={
                                "image_id": iid,
                                "url": img.url,
                                "reason": f"{type(exc).__name__}: {exc}",
                            },
                        )
                    ) from exc
                got = sha256_file(dest)
                if img.sha256 and got != img.sha256:
                    dest.unlink(missing_ok=True)
                    raise ToolExecutionError(
                        ToolError(
                            error="sha256_mismatch",
                            fields={
                                "image_id": iid,
                                "url": img.url,
                                "expected": img.sha256,
                                "actual": got,
                                "reason": (
                                    "the download did not match the pinned hash "
                                    "and was deleted"
                                ),
                            },
                        )
                    )
                fetched = True
                cat._located.pop(iid, None)
                loc = cat.locate(iid)
        if not img.licence:
            notes.append(
                f"{iid} carries no licence field in the catalog; the value is "
                f"surfaced verbatim, so it is empty rather than guessed"
            )
        for path, got in loc.rejected:
            notes.append(
                f"{path} carries {iid}'s name and sha256 {got}, not the pinned "
                f"{img.sha256 or '(unpinned)'}; it was not used"
            )
        out.append(
            {
                "id": iid,
                "filename": img.filename,
                "bytes": img.bytes,
                "sha256": img.sha256,
                "licence": img.licence,
                "present": loc.present,
                "fetched": fetched,
                "path": loc.path,
                "description": img.description,
            }
        )

    return {
        "images": out,
        "catalog_version": cat.version,
        "upstream_package_sha256": cat.upstream_package_sha256,
        "notes": notes,
    }


# ==========================================================================
# 4. The profile table -- SPEC.md 3.2, all eleven rows
# ==========================================================================

#: Which release each backend's MBP adapter ships in. SPEC.md 9: phase 1 is
#: the one-shot adapter (cpmemu, dosiz) only; the romwbw_emu/mpm2_emu pty
#: adapter is phase 2/3 and emu88d's native MBP backend is phase 5.
#: SPEC.md 4.7 -- "a tool enters the list on the release where at least one
#: backend actually serves it" -- is the same rule applied to profiles: a
#: profile whose adapter has not shipped reports ready:false and says so.
ADAPTER_PHASE: dict[str, int] = {
    "cpmemu": 1,
    "dosiz": 1,
    "romwbw_emu": 2,
    "mpm2_emu": 3,
    "emu88d": 5,
}

#: What this build ships. Bumped by whoever lands the phase-2 pty adapter.
SERVER_PHASE: int = 1


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    """One static row of SPEC.md 3.2, before anything is probed."""

    id: str
    family: Family
    tier: Tier
    backend: str
    os: str
    #: Feature strings; the `op:` entries are added by the prober from what
    #: the shipped adapter actually serves.
    features: tuple[str, ...]
    consoles: int = 1
    #: Catalog image ids this profile cannot run without.
    images: tuple[str, ...] = ()
    #: fidelity.divergences, carried inline per SPEC.md 6.4.
    divergences: tuple[str, ...] = ()
    #: Reasons this profile can never be ready in any build, regardless of what
    #: is installed. SPEC.md 3.6 and 3.7.
    hard_blocks: tuple[str, ...] = ()
    #: The romwbw_emu --boot= target, for the phase-2 adapter and for doctor.
    boot_target: str | None = None


_CPM_HOSTED_DIVERGENCES = (
    # SPEC.md 6.4 and 7.1, verbatim from the measured run.
    "setup_command_line() never writes the FCB at 0x5C, so a program testing "
    "fcb(1)=' ' for its usage banner takes the wrong branch. 80un's usage path "
    "is unreachable here.",
    "Created filenames are lowercased on the host: TEST.TXT becomes test.txt, "
    "B5-TIME.INF becomes b5-time.inf. guest_name and host_name are reported "
    "separately for that reason.",
    "There is no exit status. cpmemu exit(0)s on every normal path including "
    "its runaway watchdog, so x80_cpm_run has no exit_code field at all "
    "(SPEC.md 5.4 Invariant 4).",
    "default_mode=auto or text TOGETHER WITH eol_convert=true rewrites the "
    "guest's bytes on write: the 23-member ARC extracted 1 of 23, printed "
    "'Error', truncated the one file it wrote to 1437 bytes against 1664, and "
    "exited 0 (measured across the full matrix). default_mode=binary is "
    "sufficient protection on its own, whatever eol_convert says, and running "
    "cpmemu with no config file at all gets the corrupting combination "
    "because those are its built-in defaults. The server always synthesizes a "
    ".cfg with binary and eol_convert=false.",
    "The guest writes whole 128-byte records, so a file is padded to the next "
    "record boundary with 0x1A against a host reference's exact size; compare "
    "under normalize:['pad_to_record'].",
)

_ROMWBW_DIVERGENCES = (
    "romwbw_emu always returns 0, so the process exit code says nothing about "
    "the guest (SPEC.md 5.4 Invariant 4).",
    "There is no character-cell buffer: romwbw_emu's DECISIONS.md section 3 "
    "rules that VT emulation is the host terminal's job, so screen_text comes "
    "from this server's own VT emulator and is a fourth divergent parser.",
    "The only interrupt facility is a random fuzzing injector "
    "(romwbw_emu.cc:131-143); there is no RTC tick and SYSGET_TIMER (0xD0) has "
    "no case in handleSYS.",
    "HBIOS reports one console (hbios_dispatch.cc:1673, SYSGET_CIOCNT returns "
    "1) and handleCIO ignores the unit.",
)

#: The two lines dosiz prints on every single run (SPEC.md 5.5), folded
#: into one divergence string. The tuple itself lives in types.py because
#: the dosiz adapter filters against it.
DOSIZ_STDERR_NOISE_JOIN: str = " / ".join(DOSIZ_STDERR_NOISE)

_DOS_HOSTED_DIVERGENCES = (
    "INT 21h/31h/67h are translated to the host filesystem; there is no BIOS "
    "and no video memory, so anything reading 0xB8000 or calling INT 10h "
    "belongs on the hardware tier.",
    "Two stderr lines print on every single run and are filtered by name into "
    "diagnostics.stderr_filtered rather than silently swallowed: "
    + DOSIZ_STDERR_NOISE_JOIN,
    "The program is launched chdir'd into the sandbox under a RELATIVE name. "
    "An absolute path gives rc 102 and 'C:\\PATH\\PROG.EXE: can't open', "
    "because argv[0] is built as 'C:' + the uppercased backslashed host path "
    "and DJGPP's go32 stub reopens it to load its COFF payload (measured).",
    "There is no wall clock and no instruction counter in the run loop: a "
    "2-byte EB FE spin ran until SIGKILL. Every launch is externally deadlined "
    "and killed by process group.",
)


PROFILE_TABLE: dict[str, ProfileSpec] = {
    "cpm-hosted": ProfileSpec(
        id="cpm-hosted",
        family=Family.Z80,
        tier=Tier.HOSTED,
        backend="cpmemu",
        os="CP/M 2.2 BDOS surface",
        features=(
            "batch_run",
            "files_sandbox",
            "list_device",
            "punch_device",
            "drives:A-P",
            "cpu:8080",
            "cpu:z80",
            "unimplemented_bdos_diagnostics",
        ),
        divergences=_CPM_HOSTED_DIVERGENCES,
    ),
    "cpm22": ProfileSpec(
        id="cpm22",
        family=Family.Z80,
        tier=Tier.HARDWARE,
        backend="romwbw_emu",
        os="CP/M 2.2",
        features=(
            "interactive",
            "debugger",
            "disk_images",
            "files_hostfile",
            "files_image",
            "drives:A-P",
            "cpu:z80",
        ),
        images=("emu_avw", "hd1k_combo"),
        divergences=_ROMWBW_DIVERGENCES,
        boot_target="2",
    ),
    "cpm3": ProfileSpec(
        id="cpm3",
        family=Family.Z80,
        tier=Tier.HARDWARE,
        backend="romwbw_emu",
        os="CP/M 3 (banked)",
        features=(
            "interactive",
            "debugger",
            "disk_images",
            "files_image",
            "banked_memory",
            "pager",
            "drives:A-P",
            "cpu:z80",
        ),
        images=("emu_avw", "hd1k_combo"),
        divergences=_ROMWBW_DIVERGENCES
        + (
            "CP/M 3 paginates and then blocks: DIR *.COM on slice 3 ends with "
            "'Press RETURN to Continue ' after 21 lines (measured, t2/stdout.txt), "
            "where CP/M 2.2 on the same disk ran to completion. Any capture verb "
            "without pager handling silently truncates and then hangs.",
            "r8.com/w8.com are on hd1k_combo slice 0 and hd1k_infocom, and NOT on "
            "the CP/M 3 slice, so the hostfile route is unavailable here; use "
            "via:'image' with the machine stopped.",
        ),
        boot_target="2.3",
    ),
    "zsdos": ProfileSpec(
        id="zsdos",
        family=Family.Z80,
        tier=Tier.HARDWARE,
        backend="romwbw_emu",
        os="ZSDOS",
        features=(
            "interactive",
            "debugger",
            "files_hostfile",
            "drives:A-P",
            "cpu:z80",
        ),
        images=("emu_avw",),
        divergences=_ROMWBW_DIVERGENCES
        + (
            "Demonstrated in the research pass and not re-run in verification: "
            "the evidence is a boot banner, nothing more (SPEC.md 3.2).",
        ),
        boot_target="Z",
    ),
    "zsystem": ProfileSpec(
        id="zsystem",
        family=Family.Z80,
        tier=Tier.HARDWARE,
        backend="romwbw_emu",
        os="Z-System",
        features=(
            "interactive",
            "debugger",
            "disk_images",
            "files_hostfile",
            "files_image",
            "drives:A-P",
            "cpu:z80",
        ),
        images=("emu_avw", "hd1k_combo"),
        divergences=_ROMWBW_DIVERGENCES
        + (
            "Demonstrated in the research pass and not re-run in verification: "
            "the evidence is a boot banner, nothing more (SPEC.md 3.2).",
        ),
        boot_target="2.1",
    ),
    "nzcom": ProfileSpec(
        id="nzcom",
        family=Family.Z80,
        tier=Tier.HARDWARE,
        backend="romwbw_emu",
        os="NZCOM / ZCPR3",
        features=(
            "interactive",
            "debugger",
            "disk_images",
            "files_hostfile",
            "files_image",
            "named_directories",
            "drives:A-P",
            "cpu:z80",
        ),
        images=("emu_avw", "hd1k_combo"),
        divergences=_ROMWBW_DIVERGENCES
        + (
            "Demonstrated in the research pass and not re-run in verification: "
            "boot banner plus a ZCPR3 named-directory DIR (SPEC.md 3.2).",
        ),
        boot_target="2.2",
    ),
    "mpm2": ProfileSpec(
        id="mpm2",
        family=Family.Z80,
        tier=Tier.HARDWARE,
        backend="mpm2_emu",
        os="MP/M II V2.1",
        features=(
            "interactive",
            "multi_console",
            "disk_images",
            "drives:A-P",
            "cpu:z80",
        ),
        consoles=4,
        images=("mpm2_system",),
        divergences=(
            "The -l local console has no console attribution and must never be "
            "used as a transport: src/main.cpp:258-266 sets set_local_mode(true) "
            "on all 8 consoles, so four consoles' output interleaves on one "
            "stdout. Measured at -t 6, -t 12 and -t 20 alike: exactly one each "
            "of '1A>', '2A>', '3A>' and zero '0A>', and a DIR typed at 1A> "
            "answered 'Directory for User 3:'. Consoles are N SSH clients.",
            "A console number cannot be requested. "
            "ConsoleManager::find_free() (src/console.cpp:72-80) assigns the "
            "HIGHEST free console first; the guest announces its own number "
            "in-band as 'MP/M II Console %d' (ssh_session_libssh.cpp:298).",
            "The emulator paces to a 60 Hz real-time tick rather than running "
            "flat out: 1,237,927 Z80 instructions in a -t 9 run.",
            "No host key ships in the checkout, so the server mints a throwaway "
            "RSA key per session; and port 0 is rejected on both -w and -p "
            "despite --help saying '0 to disable', so the server binds an "
            "ephemeral port itself and passes the real number.",
            "Four concurrent SSH consoles have never been run. Two were "
            "demonstrated (SPEC.md 3.4).",
        ),
    ),
    "dos-hosted": ProfileSpec(
        id="dos-hosted",
        family=Family.X86,
        tier=Tier.HOSTED,
        backend="dosiz",
        os="DOS 6.22 API on emu88's 386",
        features=(
            "batch_run",
            "files_sandbox",
            "exit_code",
            "dpmi",
            "drives:C-Z",
            "cpu:386",
            "unimplemented_int21_diagnostics",
        ),
        divergences=_DOS_HOSTED_DIVERGENCES,
    ),
    "freedos": ProfileSpec(
        id="freedos",
        family=Family.X86,
        tier=Tier.HARDWARE,
        backend="emu88d",
        os="FreeDOS 1.4 / MS-DOS 4.0",
        features=("interactive", "disk_images", "screen", "drives:C-Z", "cpu:386"),
        images=("freedos_starter",),
        divergences=(
            "emu88 has no DOS and therefore no ERRORLEVEL, so a batch run needs "
            "a result-file convention (a guest batch file writing a result file, "
            "dos_io::lpt_output, or serial_tx) and x80_dos_run reports "
            "exit_code:null with exit_code_meaning:'none_available'.",
        ),
        # SPEC.md 3.7, verbatim.
        hard_blocks=("freedos boot has never been demonstrated headless",),
    ),
    "z80-bare": ProfileSpec(
        id="z80-bare",
        family=Family.Z80,
        tier=Tier.HARDWARE,
        backend="romwbw_emu",
        os="none",
        features=("interactive", "debugger", "rom_monitor", "cpu:z80"),
        images=("emu_avw",),
        divergences=_ROMWBW_DIVERGENCES,
        # SPEC.md 3.6, verified against romwbw_emu --help on v1.38 on this
        # machine: the option list is --strict-io --trace= --symbols= --escape=
        # --boot= --boot=none --disk0/1= --config/--no-config/--save-config.
        hard_blocks=(
            "romwbw_emu has no --start=ADDR flag (verified against --help on "
            "v1.38), so a bare machine is --boot=none to the ROM menu plus "
            "'sim> pc ADDR' over the pty, which has never been run",
        ),
        boot_target="none",
    ),
    "x86-bare": ProfileSpec(
        id="x86-bare",
        family=Family.X86,
        tier=Tier.HARDWARE,
        backend="emu88d",
        os="none",
        features=("interactive", "screen", "cpu:8088", "cpu:186", "cpu:286", "cpu:386"),
        divergences=(),
        hard_blocks=(
            "emu88d needs the ~600-line headless runner; the only thing that "
            "ever booted it, read 0xB8000 and injected keys was a ~60-line "
            "throwaway prototype committed nowhere",
        ),
    ),
}

# The table and the schema enums must not drift apart.
assert tuple(PROFILE_TABLE) == PROFILE_IDS, (
    "PROFILE_TABLE disagrees with schemas.PROFILE_IDS: "
    f"{tuple(PROFILE_TABLE)} != {PROFILE_IDS}"
)
assert all(
    PROFILE_TABLE[p].tier is PROFILE_TIER[p] for p in PROFILE_IDS
), "PROFILE_TABLE tier disagrees with schemas.PROFILE_TIER"
assert len(PROFILE_TABLE) == 11, "SPEC.md 3.2 has eleven rows"


#: SPEC.md 6.4: "external_servers_recommended points at altairsim for generic
#: CP/M-on-S-100 and at Spice86 for general DOS."
EXTERNAL_SERVERS: tuple[ExternalServerRec, ...] = (
    ExternalServerRec(
        for_="generic CP/M on S-100 hardware",
        name="altairsim",
        url="https://github.com/deltecent/altairsim",
        why=(
            "deltecent/altairsim already owns an agent that writes, assembles, "
            "runs and single-steps a CP/M program on period 8080/Z80 hardware: "
            "31 live tools, 61/61 ctest, three CP/M flavours out of 36 disk "
            "images tracked in git, CP/M 2.2 booting in 0.04 s. If the machine "
            "you want is an Altair or S-100 rather than a RomWBW SBC, register "
            "it alongside this server. Two things to know: it speaks protocol "
            "revision 2024-11-05, and its tool names are unprefixed, so an "
            "aggregating client should disambiguate by server id."
        ),
    ),
    ExternalServerRec(
        for_="general DOS and PC hardware",
        name="Spice86",
        url="https://github.com/OpenRakis/Spice86",
        why=(
            "Spice86 exposes 65+ tools including read_dos_psp, "
            "read_dos_mcb_chain, EMS/XMS, VGA and breakpoints, and six "
            "independent DOSBox-X servers cover the same ground. This server's "
            "x86 half is deliberately narrow: a DOS-API translator "
            "(dos-hosted) plus an 8088/386 board (freedos, x86-bare). Anything "
            "wanting VGA, sound, or a real PC BIOS belongs there."
        ),
    ),
)


# ==========================================================================
# 5. The prober: static table + measured machine -> list[Profile]
# ==========================================================================

class Prober:
    """Turns :data:`PROFILE_TABLE` plus this machine into ``x80_profiles`` output.

    One instance memoizes its backend probes and its image hashes, so a
    long-lived server pays for ``otool`` and sha256 once. Construct a fresh one
    when the caller wants a fresh look at the filesystem.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        catalog: ImageCatalog | None = None,
        server_phase: int = SERVER_PHASE,
    ) -> None:
        self.environ: dict[str, str] = dict(
            os.environ if environ is None else environ
        )
        self.config = config or Config.load(environ=self.environ)
        self.server_phase = server_phase
        self._catalog = catalog
        self._backends: dict[str, BackendProbe] = {}
        self._romwbw_pin: str | None | object = _UNPROBED

    # -- pieces -----------------------------------------------------------

    @property
    def catalog(self) -> ImageCatalog:
        if self._catalog is None:
            self._catalog = ImageCatalog.load(self.config, environ=self.environ)
        return self._catalog

    def backend(self, name: str) -> BackendProbe:
        if name not in self._backends:
            spec = BACKENDS.get(name)
            if spec is None:
                p = BackendProbe(name=name)
                p.problems.append(f"no discovery rule for backend {name!r}")
                self._backends[name] = p
            else:
                self._backends[name] = _probe_backend(spec, self.config, self.environ)
        return self._backends[name]

    def served_ops(self, backend: str) -> tuple[str, ...]:
        """The MBP ops this build serves on ``backend``.

        Phase 1 ships the one-shot adapter, which serves the seven required ops
        (SPEC.md 5.3) and none of the fifteen optional ones: ``boot`` is a
        no-op, ``run`` is exec-and-capture, and there is no ``regs`` and no
        ``step``. A backend whose adapter has not shipped serves nothing.
        """
        phase = ADAPTER_PHASE.get(backend, 99)
        if phase > self.server_phase:
            return ()
        return tuple(sorted(REQUIRED_OPS))

    # -- the ROM/disk pairing check ---------------------------------------

    def romwbw_pinned_version(self) -> str | None:
        """The RomWBW generation ``romwbw_emu`` was compiled against.

        Measured on this machine: ``romwbw_emu --version`` prints
        ``RomWBW compatibility: v3.5.1 (pinned)`` on its second line.
        Memoized, because five profiles ask for it.
        """
        if self._romwbw_pin is not _UNPROBED:
            return self._romwbw_pin
        self._romwbw_pin = None
        bp = self.backend("romwbw_emu")
        if bp.usable and bp.path:
            _rc, out, err = _run([bp.path, "--version"], env=self.environ)
            m = re.search(r"RomWBW compatibility:\s*v?(\S+?)\s*\(pinned\)", out + err)
            if m:
                self._romwbw_pin = m.group(1)
        return self._romwbw_pin

    def pairing_problem(self, spec: ProfileSpec) -> str | None:
        """The SPEC.md 5.5 ROM/disk pairing rule, applied to this install.

        "ROM and disk are pinned as a pair. The C++ HBIOS emulates RomWBW
        v3.5.1 exactly; a mismatched slice prints '*** WARNING: HBIOS/CBIOS
        Version Mismatch ***' into a transcript an agent will misread."

        Both halves come out of one catalog generation, so within a loaded
        catalog the pair is consistent by construction and the only thing left
        to check is that the emulator agrees with the generation:
        ``emu_validate_rom_hcb`` refuses a mismatched ROM. A file that carries
        a catalog name and the wrong bytes is handled one layer up -- it is
        never used, and it blocks only if no verified copy was found anywhere.
        """
        if spec.backend != "romwbw_emu" or not spec.images:
            return None
        pinned = self.romwbw_pinned_version()
        if pinned and pinned != self.catalog.version:
            return (
                f"ROM/disk pairing: romwbw_emu is pinned to RomWBW {pinned} and "
                f"the loaded catalog is {self.catalog.version}. "
                f"emu_validate_rom_hcb refuses a mismatched ROM, and a mismatched "
                f"slice prints '*** WARNING: HBIOS/CBIOS Version Mismatch ***' "
                f"into the transcript. Set romwbw_version in the config file, or "
                f"pass romwbw_version to x80_images."
            )
        return None

    # -- one profile ------------------------------------------------------

    def profile(self, profile_id: str, *, probe: bool = True) -> Profile:
        spec = PROFILE_TABLE[profile_id]
        blocked: list[str] = list(spec.hard_blocks)
        caps: list[str] = list(spec.features)
        images = ProfileImages()
        version: str | None = None
        binary: str | None = None

        adapter_phase = ADAPTER_PHASE.get(spec.backend, 99)
        if adapter_phase > self.server_phase:
            blocked.append(
                f"the {spec.backend} adapter ships in phase {adapter_phase}; "
                f"this build is phase {self.server_phase} and ships the one-shot "
                f"adapter only (SPEC.md 9)"
            )
        caps.extend(f"op:{op}" for op in self.served_ops(spec.backend))
        caps.sort()

        if not probe:
            # The cheap static answer the schema's probe:false asks for: the
            # table, with nothing stat'd, hashed, or executed.
            return Profile(
                id=spec.id,
                family=spec.family,
                tier=spec.tier,
                backend=spec.backend,
                os=spec.os,
                caps=caps,
                consoles=spec.consoles,
                images=ProfileImages(
                    required=[
                        ImageRequirement(
                            id=i,
                            sha256=(
                                ""
                                if i in LOCAL_IMAGES
                                else "(not probed)"
                            ),
                            present=False,
                        )
                        for i in spec.images
                    ]
                ),
                fidelity=Fidelity(tier=spec.tier, divergences=list(spec.divergences)),
                ready=False,
                blocked_by=blocked + ["not probed: x80_profiles was called with probe:false"],
                backend_version=None,
                binary_path=None,
            )

        bp = self.backend(spec.backend)
        version, binary = bp.version, bp.path
        blocked.extend(bp.problems)

        for iid in spec.images:
            req = self.catalog.requirement(iid)
            images.required.append(req)
            if not req.present:
                loc = self.catalog.locate(iid)
                img = self.catalog.images.get(iid)
                if img is not None and img.source == "local_build":
                    blocked.append(
                        f"image {iid} is built by its own checkout and is not in "
                        f"the pinned romwbw_disks catalog, so it cannot be "
                        f"fetched or sha-pinned here; {img.description}"
                    )
                elif loc.rejected:
                    blocked.append(
                        f"image {iid} was found but does not match its pin: "
                        + "; ".join(f"{p} hashes {h}" for p, h in loc.rejected)
                    )
                else:
                    blocked.append(
                        f"image {iid} is missing; fetch it with "
                        f'x80_images {{"image_ids":["{iid}"],"dry_run":false,'
                        f'"allow_fetch":true}} or point EIGHTYMCP_IMAGE_DIR at it'
                    )

        pairing = self.pairing_problem(spec)
        if pairing:
            blocked.append(pairing)

        if self.config.problems:
            blocked.extend(self.config.problems)

        ready = not blocked
        return Profile(
            id=spec.id,
            family=spec.family,
            tier=spec.tier,
            backend=spec.backend,
            os=spec.os,
            caps=caps,
            consoles=spec.consoles,
            images=images,
            fidelity=Fidelity(tier=spec.tier, divergences=list(spec.divergences)),
            ready=ready,
            blocked_by=blocked,
            backend_version=version,
            binary_path=binary,
        )

    # -- the whole table --------------------------------------------------

    def profiles(
        self,
        *,
        family: str = "all",
        profile: str | None = None,
        only_ready: bool = False,
        probe: bool = True,
    ) -> list[Profile]:
        if profile is not None:
            if profile not in PROFILE_TABLE:
                raise ToolExecutionError(
                    ToolError.bad_argument(
                        "profile",
                        "unknown profile id",
                        given=profile,
                        known=list(PROFILE_IDS),
                    )
                )
            ids = [profile]
        else:
            ids = [
                p
                for p in PROFILE_IDS
                if family == "all" or PROFILE_TABLE[p].family == family
            ]
        out = [self.profile(p, probe=probe) for p in ids]
        if only_ready:
            out = [p for p in out if p.ready]
        return out

    def result(
        self,
        *,
        family: str = "all",
        profile: str | None = None,
        only_ready: bool = False,
        probe: bool = True,
        protocol_version: str | None = None,
    ) -> ProfilesResult:
        return ProfilesResult(
            profiles=self.profiles(
                family=family, profile=profile, only_ready=only_ready, probe=probe
            ),
            server_version=__version__,
            protocol_version=str(protocol_version or LATEST_REVISION),
            external_servers_recommended=list(EXTERNAL_SERVERS),
        )

    def require_ready(self, profile_id: str) -> Profile:
        """The profile, or a structured refusal naming exactly what is missing.

        The one call ``tools.py`` makes before touching a backend. An absent
        backend must be an actionable ``isError`` body, never a traceback and
        never a bare "not supported": SPEC.md 4.7 is explicit that an agent can
        act on a structured reason and cannot act on a bare one.
        """
        if profile_id not in PROFILE_TABLE:
            raise ToolExecutionError(
                ToolError.bad_argument(
                    "profile",
                    "unknown profile id",
                    given=profile_id,
                    known=list(PROFILE_IDS),
                )
            )
        prof = self.profile(profile_id)
        if prof.ready:
            return prof
        spec = PROFILE_TABLE[profile_id]
        alternatives = [
            p.id
            for p in self.profiles(family=str(spec.family), only_ready=True)
        ]
        raise ToolExecutionError(
            ToolError(
                error="profile_not_ready",
                fields={
                    "profile": profile_id,
                    "backend": spec.backend,
                    "reason": "; ".join(prof.blocked_by) or "unknown",
                    "blocked_by": list(prof.blocked_by),
                    "ready_profiles": alternatives,
                    "escalation": Escalation(
                        tool="x80_profiles",
                        arguments={"profile": profile_id, "probe": True},
                    ).to_json(),
                },
            )
        )

    def ready_ids(self) -> set[str]:
        return {p.id for p in self.profiles() if p.ready}


def profiles_result(
    *,
    family: str = "all",
    profile: str | None = None,
    only_ready: bool = False,
    probe: bool = True,
    protocol_version: str | None = None,
    config: Config | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProfilesResult:
    """``x80_profiles``'s result, from a one-shot prober."""
    return Prober(config, environ=environ).result(
        family=family,
        profile=profile,
        only_ready=only_ready,
        probe=probe,
        protocol_version=protocol_version,
    )


# ==========================================================================
# 6. x80_probe routing: which OS a package actually needs, from its syscalls
# ==========================================================================

#: The profile x80_probe runs a package on first, per family. Both are the
#: hosted tier: instant, no image, and the only two that report unimplemented
#: syscalls by number (SPEC.md 6.4).
CHEAPEST_PROFILE: dict[Family, str] = {
    Family.Z80: "cpm-hosted",
    Family.X86: "dos-hosted",
}

#: CP/M BDOS function names. 0-40 are CP/M 2.2; 41-49 are CP/M 3 additions and
#: are the boundary that turns "unimplemented" into "rerun under cpm3".
BDOS_FUNCTIONS: dict[int, str] = {
    0: "P_TERMCPM", 1: "C_READ", 2: "C_WRITE", 3: "A_READ", 4: "A_WRITE",
    5: "L_WRITE", 6: "C_RAWIO", 7: "GET_IOBYTE", 8: "SET_IOBYTE",
    9: "C_WRITESTR", 10: "C_READSTR", 11: "C_STAT", 12: "S_BDOSVER",
    13: "DRV_ALLRESET", 14: "DRV_SET", 15: "F_OPEN", 16: "F_CLOSE",
    17: "F_SFIRST", 18: "F_SNEXT", 19: "F_DELETE", 20: "F_READ", 21: "F_WRITE",
    22: "F_MAKE", 23: "F_RENAME", 24: "DRV_LOGINVEC", 25: "DRV_GET",
    26: "F_DMAOFF", 27: "DRV_ALLOCVEC", 28: "DRV_SETRO", 29: "DRV_ROVEC",
    30: "F_ATTRIB", 31: "DRV_DPB", 32: "F_USERNUM", 33: "F_READRAND",
    34: "F_WRITERAND", 35: "F_SIZE", 36: "F_RANDREC", 37: "DRV_RESET",
    38: "DRV_ACCESS", 39: "DRV_FREE", 40: "F_WRITEZF",
    41: "F_TESTWRITE", 42: "F_LOCK", 43: "F_UNLOCK", 44: "F_MULTISEC",
    45: "F_ERRMODE", 46: "DRV_SPACE", 47: "P_CHAIN", 48: "DRV_FLUSH",
    49: "S_SCB", 50: "S_BIOS", 59: "P_LOAD", 60: "F_TRUNCATE",
    98: "F_PARSE", 99: "DRV_RESETALL", 105: "T_GET", 104: "T_SET",
    107: "F_PASSWD", 109: "C_MODE", 110: "C_DELIMIT", 111: "C_WRITEBLK",
    112: "L_WRITEBLK", 152: "F_PARSE",
}

#: MP/M XDOS functions live above 128 and are the boundary that turns
#: "unimplemented" into "rerun under mpm2".
XDOS_FUNCTIONS: dict[int, str] = {
    128: "M_ALLOC", 129: "M_FREE", 130: "DEV_POLL", 131: "DEV_FLAG_WAIT",
    132: "DEV_FLAG_SET", 133: "Q_MAKE", 134: "Q_OPEN", 135: "Q_DELETE",
    136: "Q_READ", 137: "Q_CREAD", 138: "Q_WRITE", 139: "Q_CWRITE",
    140: "P_DELAY", 141: "P_DISPATCH", 142: "P_TERM", 143: "P_CREATE",
    144: "P_PRIORITY", 145: "C_ATTACH", 146: "C_DETACH", 147: "C_SET",
    148: "C_ASSIGN", 149: "P_CLI", 150: "P_RPL", 151: "F_PARSE",
    153: "C_GET", 154: "S_SYSDAT", 155: "T_GET", 156: "P_ABORT",
    157: "L_ATTACH", 158: "L_DETACH", 159: "L_SET", 160: "L_CATTACH",
    161: "C_MODE", 162: "S_SYSVAR", 163: "P_PDADR", 164: "P_ABSOLUTE",
    165: "P_RELATIVE",
}

#: INT 21h function names, by AH. Everything above 0x57 is DOS 3.x+ territory
#: and is what turns "unimplemented" into "this needs a real DOS kernel".
INT21_FUNCTIONS: dict[int, str] = {
    0x00: "TERMINATE", 0x01: "CHAR_INPUT", 0x02: "CHAR_OUTPUT",
    0x06: "DIRECT_CONIO", 0x07: "DIRECT_CHAR_INPUT", 0x08: "CHAR_INPUT_NOECHO",
    0x09: "PRINT_STRING", 0x0A: "BUFFERED_INPUT", 0x0B: "CHECK_INPUT_STATUS",
    0x0C: "FLUSH_AND_INPUT", 0x0D: "DISK_RESET", 0x0E: "SET_DEFAULT_DRIVE",
    0x19: "GET_DEFAULT_DRIVE", 0x1A: "SET_DTA", 0x25: "SET_INTERRUPT_VECTOR",
    0x2A: "GET_DATE", 0x2C: "GET_TIME", 0x2F: "GET_DTA", 0x30: "GET_DOS_VERSION",
    0x33: "GET_SET_CTRL_BREAK", 0x35: "GET_INTERRUPT_VECTOR",
    0x36: "GET_FREE_DISK_SPACE", 0x38: "GET_SET_COUNTRY",
    0x39: "MKDIR", 0x3A: "RMDIR", 0x3B: "CHDIR", 0x3C: "CREATE_FILE",
    0x3D: "OPEN_FILE", 0x3E: "CLOSE_FILE", 0x3F: "READ_FILE",
    0x40: "WRITE_FILE", 0x41: "DELETE_FILE", 0x42: "SEEK_FILE",
    0x43: "GET_SET_ATTRIBUTES", 0x44: "IOCTL", 0x45: "DUP_HANDLE",
    0x46: "FORCE_DUP_HANDLE", 0x47: "GET_CWD", 0x48: "ALLOCATE_MEMORY",
    0x49: "FREE_MEMORY", 0x4A: "RESIZE_MEMORY_BLOCK", 0x4B: "EXEC",
    0x4C: "TERMINATE_WITH_CODE", 0x4D: "GET_RETURN_CODE",
    0x4E: "FIND_FIRST", 0x4F: "FIND_NEXT", 0x54: "GET_VERIFY_FLAG",
    0x56: "RENAME_FILE", 0x57: "GET_SET_FILE_DATETIME",
    0x58: "GET_SET_ALLOCATION_STRATEGY", 0x59: "GET_EXTENDED_ERROR",
    0x5A: "CREATE_TEMP_FILE", 0x5B: "CREATE_NEW_FILE",
    0x5C: "LOCK_UNLOCK_FILE", 0x5D: "SERVER_FUNCTIONS",
    0x5E: "NETWORK_FUNCTIONS", 0x5F: "NETWORK_REDIRECTION",
    0x62: "GET_PSP_ADDRESS", 0x63: "GET_DBCS_LEAD_TABLE",
    0x65: "GET_EXTENDED_COUNTRY_INFO", 0x66: "GET_SET_CODE_PAGE",
    0x67: "SET_HANDLE_COUNT", 0x68: "COMMIT_FILE", 0x6C: "EXTENDED_OPEN",
}


def bdos_call(func: int, count: int = 1) -> UnimplementedCall:
    """One ``unimplemented`` entry from a cpmemu ``Unimplemented BDOS function N``."""
    layer = SyscallLayer.XDOS if func >= 128 else SyscallLayer.BDOS
    table = XDOS_FUNCTIONS if func >= 128 else BDOS_FUNCTIONS
    return UnimplementedCall(
        layer=layer, func=func, name=table.get(func, f"BDOS_{func}"), count=count
    )


def int21_call(ah: int, count: int = 1) -> UnimplementedCall:
    """One ``unimplemented`` entry from a dosiz ``unimplemented INT 21h AH=XXh``."""
    return UnimplementedCall(
        layer=SyscallLayer.INT21,
        func=ah,
        name=INT21_FUNCTIONS.get(ah, f"AH_{ah:02X}"),
        count=count,
    )


def detect_family(program: str | os.PathLike[str]) -> Family:
    """Which family a host file belongs to, for ``x80_probe``'s ``family:"auto"``.

    Read from the bytes, not the extension: a ``.COM`` is a ``.COM`` on both
    sides of this server. An MZ header is x86; a ``.exe`` is x86; otherwise
    z80, which is also the safe default, because ``cpm-hosted`` reports
    unimplemented BDOS by number and a wrong guess costs one cheap run.
    """
    p = Path(program)
    try:
        with p.open("rb") as fh:
            head = fh.read(2)
    except OSError:
        head = b""
    if head in (b"MZ", b"ZM"):
        return Family.X86
    if p.suffix.lower() in (".exe", ".sys", ".dll"):
        return Family.X86
    return Family.Z80


def classify_probe(
    *,
    ran_on: str,
    unimplemented: Sequence[UnimplementedCall],
    loaded: bool,
    ready_profiles: Iterable[str] = (),
    program: str = "",
    args: Sequence[str] = (),
    extra_evidence: Iterable[str] = (),
) -> tuple[ProbeVerdict, str, list[str], Escalation | None]:
    """``(verdict, recommended_profile, evidence, escalation)`` for ``x80_probe``.

    Pure: it takes what the cheap run observed and returns where to go next.
    ``tools.py`` owns the run; this owns the routing, so the escalation an
    agent gets is the same table ``x80_profiles`` reports from.

    The escalation is a literal next call. When the recommended profile is not
    ready on this install, the escalation points at ``x80_profiles`` for that
    profile instead of at a verb that would fail -- an agent handed a call that
    cannot work is worse off than one handed the reason.
    """
    ready = set(ready_profiles)
    evidence: list[str] = list(extra_evidence)
    z80 = ran_on == "cpm-hosted"

    if not loaded:
        evidence.append(f"{ran_on} could not load the program at all")
        rec = "cpm22" if z80 else "freedos"
        verdict = ProbeVerdict.FAILED_TO_LOAD
    elif not unimplemented:
        evidence.append(
            f"the run completed on {ran_on} with no unimplemented syscalls"
        )
        return (
            ProbeVerdict.SUFFICIENT,
            ran_on,
            evidence,
            Escalation(
                tool="x80_cpm_run" if z80 else "x80_dos_run",
                arguments={
                    "profile": ran_on,
                    "program": program,
                    **({"args": list(args)} if args else {}),
                },
            ),
        )
    else:
        worst = max(int(u.func) for u in unimplemented)
        for u in unimplemented:
            evidence.append(
                f"{u.layer} {u.func} ({u.name}) x{u.count} is unimplemented on {ran_on}"
            )
        if z80:
            if worst >= 128:
                rec, verdict = "mpm2", ProbeVerdict.NEEDS_RICHER_OS
                evidence.append(
                    "a call above 127 is XDOS: this is an MP/M program, not a "
                    "CP/M one"
                )
            elif worst >= 41:
                rec, verdict = "cpm3", ProbeVerdict.NEEDS_RICHER_OS
                evidence.append(
                    "BDOS 41-49 are CP/M 3 additions; cpm-hosted implements the "
                    "CP/M 2.2 surface only"
                )
            else:
                rec, verdict = "cpm22", ProbeVerdict.NEEDS_HARDWARE_TIER
                evidence.append(
                    "the missing calls are inside the CP/M 2.2 range, so a real "
                    "CP/M 2.2 on emulated hardware serves them"
                )
        else:
            if worst > 0x57:
                rec, verdict = "freedos", ProbeVerdict.NEEDS_RICHER_OS
                evidence.append(
                    "INT 21h above AH=57h is DOS 3.x and later; dos-hosted "
                    "translates the DOS 6.22 API subset dosiz implements"
                )
            else:
                rec, verdict = "freedos", ProbeVerdict.NEEDS_HARDWARE_TIER

    if rec in ready:
        escalation = Escalation(
            tool="x80_cpm_run" if rec in CPM_PROFILES else "x80_dos_run",
            arguments={
                "profile": rec,
                "program": program,
                **({"args": list(args)} if args else {}),
            },
        )
    else:
        evidence.append(
            f"{rec} is not ready on this install; x80_profiles says why"
        )
        escalation = Escalation(
            tool="x80_profiles", arguments={"profile": rec, "probe": True}
        )
    return verdict, rec, evidence, escalation


# ==========================================================================
# 7. 80mcp doctor
# ==========================================================================

def _columns(rows: Sequence[Sequence[str]]) -> str:
    """Left-aligned columns, two spaces apart. No rules and no frame: a drawn
    border comes along when the reader selects the text out of a terminal."""
    if not rows:
        return ""
    width = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for r in rows:
        lines.append(
            "  ".join(c.ljust(width[i]) for i, c in enumerate(r)).rstrip()
        )
    return "\n".join(lines)


def doctor_report(
    prober: Prober | None = None,
    *,
    config: Config | None = None,
    environ: Mapping[str, str] | None = None,
    verbose: bool = False,
) -> tuple[str, int]:
    """``(text, exit_code)`` for ``80mcp doctor``.

    SPEC.md 9 phase 1: "a table of every backend -- found/not found, version,
    pinned sha256, ROM/disk pairing, dynamic-library resolution -- exiting
    nonzero on any problem." SPEC.md 5.5 adds that a mismatched install must
    refuse to start, which is what the nonzero exit is for.

    Exit codes: 0 clean, 1 at least one profile is blocked, 2 nothing at all is
    runnable on this machine.
    """
    prober = prober or Prober(config, environ=environ)
    out: list[str] = []
    problems = 0

    out.append(f"80mcp {__version__}  phase {prober.server_phase}  "
               f"protocol {LATEST_REVISION}")
    out.append(f"python {platform.python_version()} on {platform.platform()}")
    out.append("")

    # -- backends ---------------------------------------------------------
    out.append("backends")
    rows: list[list[str]] = [["  name", "status", "version", "path"]]
    for name in BACKENDS:
        bp = prober.backend(name)
        if not bp.found:
            status = "not found"
        elif bp.missing_libraries:
            status = "dyld FAIL"
        else:
            status = "ok"
        rows.append(
            [
                "  " + name,
                status,
                bp.version or "-",
                bp.path or "-",
            ]
        )
    out.append(_columns(rows))
    for name in BACKENDS:
        bp = prober.backend(name)
        for prob in bp.problems:
            out.append(f"    {name}: {prob}")
        if verbose:
            out.append(f"    {name}: library check {bp.library_check}")
            for s in bp.searched:
                out.append(f"      tried {s}")
    out.append("")

    # -- images -----------------------------------------------------------
    cat = prober.catalog
    out.append(f"images  catalog {cat.version}"
               + (f"  from {cat.catalog_path}" if cat.catalog_path else "  NOT FOUND"))
    if cat.upstream_package_sha256:
        out.append(f"  upstream RomWBW package sha256 {cat.upstream_package_sha256}")
    needed: list[str] = []
    for spec in PROFILE_TABLE.values():
        for iid in spec.images:
            if iid not in needed:
                needed.append(iid)
    rows = [["  id", "present", "pinned sha256", "path"]]
    for iid in needed:
        loc = cat.locate(iid)
        img = cat.images.get(iid)
        rows.append(
            [
                "  " + iid,
                "yes" if loc.present else "no",
                (img.sha256[:16] + "..." if img and img.sha256 else "(unpinned)"),
                loc.path or "-",
            ]
        )
    out.append(_columns(rows))
    for iid in needed:
        for path, got in cat.locate(iid).rejected:
            problems += 1
            out.append(f"    {iid}: {path} hashes {got}, not the pin -- not used")
    for prob in cat.problems:
        out.append(f"    {prob}")
    out.append("")

    # -- profiles ---------------------------------------------------------
    profiles = prober.profiles()
    out.append("profiles")
    rows = [["  id", "family", "tier", "backend", "consoles", "ready"]]
    for p in profiles:
        rows.append(
            [
                "  " + p.id,
                str(p.family),
                str(p.tier),
                p.backend,
                str(p.consoles),
                "yes" if p.ready else "no",
            ]
        )
    out.append(_columns(rows))
    out.append("")
    for p in profiles:
        if p.ready:
            continue
        problems += 1
        out.append(f"  {p.id} blocked:")
        for b in p.blocked_by:
            out.append(f"    - {b}")
    for prob in prober.config.problems:
        problems += 1
        out.append(f"  config: {prob}")

    ready = [p.id for p in profiles if p.ready]
    out.append("")
    out.append(
        f"{len(ready)} of {len(profiles)} profiles ready"
        + (": " + ", ".join(ready) if ready else "")
    )

    if not ready:
        return "\n".join(out), 2
    return "\n".join(out), (1 if problems else 0)
