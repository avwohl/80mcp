"""Declared normalization and the differential comparison engine behind ``x80_diff_run``.

SPEC.md 6.3 defines ``Normalize`` as an **array of enum members**, never a
boolean, and SPEC.md 7.2 says why in measured numbers. Reproduced here on
``80un/tests/samples/arc/method9.arc`` (54842 bytes, 23 members), guest side
``cpmemu`` running ``80un.com``, reference side ``python3 -m un80.cli``:

* no normalization       -> ``diff -rq`` prints 46 "Only in" lines, 0 matches
* ``lowercase_names``    -> 23 paired, identical 2, differ 21
* ``+ pad_to_record``    -> 23 paired, identical 23, differ 0

The two that match unnormalized are ``-03MAR86`` at 0 bytes and ``TIME2.ASM``
at 14720 = 115*128: both already record-aligned. ``B5-TIME.INF`` is the shape
of the other 21 -- guest 1664, reference 1537, and 1664 == ceil(1537/128)*128.

Two rules this module enforces rather than documents:

1. **Normalization applies to the reported bytes and sha256 only, never to
   the file on disk.** Every filesystem access in this module goes through
   :func:`_read_bytes`, which opens ``"rb"``; nothing here opens for write,
   renames, or unlinks. :func:`diff_trees` additionally stats every file it
   touches before and after and raises if anything moved underneath it.
2. **The comparison is symmetric.** Neither side is privileged: the same
   content-level normalizations are applied to guest and reference alike, so
   swapping the arguments swaps ``only_in_*`` and changes nothing else.

``strip_cpm_eof`` and ``crlf_to_lf`` are ported, not imported, and the choice
matters enough to record. 80mcp declares no runtime dependencies
(pyproject.toml, ``dependencies = []``) and ``un80`` is a separate project
that is not one, so an import would have to be a ``try: import un80`` with a
fallback -- which is two implementations, taken on whether a checkout happens
to be on ``sys.path``, in the one place where a wrong answer looks exactly
like a right one. A differential engine whose semantics vary with the host's
import path is the failure this tool exists to catch.

So: ported behaviour-for-behaviour from ``un80.cpm`` (``src/un80/cpm.py:28-77``
in the 80un checkout), with the citation on each function, and
``tests/test_normalize.py::test_port_agrees_with_un80`` imports the real ones
when a checkout is present and asserts byte-for-byte agreement over 13 hand-
picked edge cases, 400 random byte strings drawn from {00,0A,0D,1A,41,FF} and
the 54842-byte ARC fixture itself. Drift is caught on any machine that has a
80un checkout, and CI never needs one.
"""

from __future__ import annotations

import fnmatch
import hashlib
import itertools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .types import (
    CompareMode,
    DiffResult,
    FileDiff,
    Normalization,
    ToolError,
    ToolExecutionError,
)

__all__ = [
    "RECORD_SIZE",
    "CPM_EOF",
    "SCHEMA_ORDER",
    "CONTENT_ORDER",
    "SUGGESTION_ORDER",
    "NAME_NORMALIZATIONS",
    "CONTENT_NORMALIZATIONS",
    "crlf_to_lf",
    "strip_cpm_eof",
    "pad_to_record",
    "parse_normalizations",
    "normalize_name",
    "normalize_bytes",
    "sha256_hex",
    "Reported",
    "report_bytes",
    "collect_tree",
    "diff_files",
    "diff_trees",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECORD_SIZE = 128
"""The CP/M record. cpmemu writes whole records; a host reference writes the
exact size out of the archive header. Measured: guest B5-TIME.INF 1664 bytes,
reference 1537, 1664 == ceil(1537/128)*128."""

CPM_EOF = 0x1A
"""Ctrl-Z. ``un80.cpm.CPM_EOF`` (src/un80/cpm.py:25) carries the same value,
and it is the byte cpmemu pads the tail of a short record with (measured: all
127 pad bytes of B5-TIME.INF)."""

_CPM_EOF_BYTE = bytes([CPM_EOF])

SCHEMA_ORDER: tuple[Normalization, ...] = (
    Normalization.LOWERCASE_NAMES,
    Normalization.PAD_TO_RECORD,
    Normalization.STRIP_CPM_EOF,
    Normalization.CRLF_TO_LF,
)
"""Declaration order of the ``Normalize`` enum in SPEC.md 6.3. ``normalize``
is a set (``uniqueItems: true``) with no order semantics, so the echo in
``normalization_applied`` is emitted in this order and not in call order --
``["pad_to_record","lowercase_names"]`` echoes as
``["lowercase_names","pad_to_record"]``, which is the form SPEC.md 7.2 shows."""

NAME_NORMALIZATIONS: frozenset[Normalization] = frozenset({Normalization.LOWERCASE_NAMES})
"""Applied when pairing the two sides by name."""

CONTENT_NORMALIZATIONS: frozenset[Normalization] = frozenset(
    {
        Normalization.CRLF_TO_LF,
        Normalization.STRIP_CPM_EOF,
        Normalization.PAD_TO_RECORD,
    }
)
"""Applied to the bytes before they are compared or hashed."""

CONTENT_ORDER: tuple[Normalization, ...] = (
    Normalization.CRLF_TO_LF,
    Normalization.STRIP_CPM_EOF,
    Normalization.PAD_TO_RECORD,
)
"""The pipeline order, which is fixed and is **not** the caller's array order.

``crlf_to_lf`` changes length, so it has to run before anything that aligns to
a record boundary. ``strip_cpm_eof`` and ``pad_to_record`` are the pair that
actually needs an order decided: strip-then-pad takes both sides to the same
canonical record form (guest 1664 -> 1537 -> 1664, reference 1537 -> 1537 ->
1664) and is idempotent, while pad-then-strip would undo the padding and
collapse the combination to a plain strip. Strip first."""

SUGGESTION_ORDER: tuple[Normalization, ...] = (
    Normalization.PAD_TO_RECORD,
    Normalization.CRLF_TO_LF,
    Normalization.STRIP_CPM_EOF,
)
"""Preference order for the "adding X would have made these equal" hint:
least destructive first. ``pad_to_record`` only appends 0x1A up to a record
boundary, ``crlf_to_lf`` rewrites a two-byte sequence, ``strip_cpm_eof``
deletes bytes and is the only one of the three that can equate two files by
throwing away content. This is a hint order, not a pipeline order; the
pipeline is :data:`CONTENT_ORDER`."""

assert set(SUGGESTION_ORDER) == CONTENT_NORMALIZATIONS
assert set(CONTENT_ORDER) == CONTENT_NORMALIZATIONS
assert set(SCHEMA_ORDER) == set(Normalization)
assert NAME_NORMALIZATIONS | CONTENT_NORMALIZATIONS == set(Normalization)


# ---------------------------------------------------------------------------
# The four normalizations
# ---------------------------------------------------------------------------

def crlf_to_lf(data: bytes) -> bytes:
    """CR LF -> LF.

    Ported from ``un80.cpm.crlf_to_lf`` (src/un80/cpm.py:64-77), whose whole
    body is ``return data.replace(b'\\r\\n', b'\\n')``. A lone CR and a lone LF
    are both left alone, which matters: a CP/M text file that ends mid-record
    can carry a bare CR, and rewriting it would manufacture a difference.
    """
    return data.replace(b"\r\n", b"\n")


def strip_cpm_eof(data: bytes, *, aggressive: bool = False) -> bytes:
    """Drop the trailing run of 0x1A.

    Ported from ``un80.cpm.strip_cpm_eof`` (src/un80/cpm.py:28-61). Both of
    that function's branches strip exactly the trailing run and nothing else:
    the ``aggressive`` branch is ``data.rstrip(bytes([CPM_EOF]))``, and the
    default branch walks backwards from the end and ``break``s at the first
    byte that is not 0x1A, so its "is everything after this also 0x1A?" test
    can never be false. The ``aggressive`` keyword is kept here so the
    cross-check test can call both spellings against the original; it does not
    change the result, and
    ``tests/test_normalize.py::test_aggressive_flag_is_a_no_op`` pins that.

    An embedded 0x1A followed by real data is untouched, on either branch.
    """
    if not data:
        return data
    return data.rstrip(_CPM_EOF_BYTE)


def pad_to_record(data: bytes, *, record: int = RECORD_SIZE, pad: int = CPM_EOF) -> bytes:
    """Pad up to the next ``record`` boundary with ``pad``.

    Applied to **both** sides, each to its own next boundary, which is what
    SPEC.md 6.3's "pad the shorter side to the next 128-byte boundary" means
    in the case it was measured on: the guest writes whole records, so the
    longer side is already aligned and only the shorter one moves (reference
    1537 -> 1664, guest 1664 unchanged). Doing it symmetrically is the only
    version that gives the same verdict when the arguments are swapped.

    A zero-length file stays zero-length -- ``ceil(0/128)*128 == 0`` -- which
    is why ``-03MAR86`` at 0 bytes is one of the two members that match with
    no normalization at all.

    The padding is not a licence to ignore the pad bytes: the comparison after
    this is a plain byte compare, so a guest tail of anything other than 0x1A
    still reports a difference at the offset where it starts.
    """
    if record <= 0:
        raise ValueError(f"record must be positive, got {record}")
    remainder = len(data) % record
    if remainder == 0:
        return data
    return data + bytes([pad]) * (record - remainder)


# ---------------------------------------------------------------------------
# Parsing and application
# ---------------------------------------------------------------------------

def parse_normalizations(values: Iterable[Any] | None) -> tuple[Normalization, ...]:
    """Coerce the ``normalize`` array to enum members, in :data:`SCHEMA_ORDER`.

    Duplicates collapse (the schema says ``uniqueItems``, and a caller that
    sends a duplicate anyway meant it once). An unknown member is a caller
    error, not a silent skip: a run that quietly dropped ``pad_to_record``
    would report ``identical:2 differ:21`` and look like a real result.
    """
    if values is None:
        return ()
    seen: set[Normalization] = set()
    unknown: list[Any] = []
    for value in values:
        if isinstance(value, Normalization):
            seen.add(value)
            continue
        try:
            seen.add(Normalization(value))
        except ValueError:
            unknown.append(value)
    if unknown:
        raise ToolExecutionError(
            ToolError.bad_argument(
                "normalize",
                "not a member of the Normalize enum",
                unknown=unknown,
                allowed=[n.value for n in SCHEMA_ORDER],
            )
        )
    return tuple(n for n in SCHEMA_ORDER if n in seen)


def normalize_name(name: str, normalize: Sequence[Normalization] = ()) -> str:
    """The pairing key for one side's name.

    ``lowercase_names`` exists because cpmemu lowercases on create: SPEC.md
    6.4 states it as a ``cpm-hosted`` fidelity divergence, and it is measured
    as ``B5-TIME.INF -> b5-time.inf``. Without it every member is "Only in" on
    both sides (46 lines for 23 members).

    ``str.lower`` and not ``str.casefold``: these are CP/M 8.3 names, and
    casefold would fold non-ASCII pairs that cpmemu's byte-wise ``tolower``
    does not.
    """
    if Normalization.LOWERCASE_NAMES in normalize:
        return name.lower()
    return name


def normalize_bytes(data: bytes, normalize: Sequence[Normalization] = ()) -> bytes:
    """Apply the content-level normalizations, in :data:`CONTENT_ORDER`.

    ``lowercase_names`` is a name-level member and is ignored here.
    """
    for member in CONTENT_ORDER:
        if member not in normalize:
            continue
        if member is Normalization.CRLF_TO_LF:
            data = crlf_to_lf(data)
        elif member is Normalization.STRIP_CPM_EOF:
            data = strip_cpm_eof(data)
        elif member is Normalization.PAD_TO_RECORD:
            data = pad_to_record(data)
    return data


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class Reported:
    """What ``collect.normalize`` changes, and the boundary of what it changes.

    ``x80_cpm_run``'s ``collect.normalize`` is documented in SPEC.md 6.3 as
    "Applied to the REPORTED sha256 and content only, never to the file on
    disk". This carries the reported view; the file the caller can still fetch
    with ``x80_files`` is untouched, and ``raw_bytes`` says how big it is.
    """

    data: bytes
    bytes: int
    sha256: str
    raw_bytes: int
    changed: bool

    def to_json(self) -> dict[str, Any]:
        return {"bytes": self.bytes, "sha256": self.sha256}


def report_bytes(raw: bytes, normalize: Sequence[Normalization] = ()) -> Reported:
    """Reported bytes and sha256 for one file. Never writes anything."""
    data = normalize_bytes(raw, normalize)
    return Reported(
        data=data,
        bytes=len(data),
        sha256=sha256_hex(data),
        raw_bytes=len(raw),
        changed=data != raw,
    )


# ---------------------------------------------------------------------------
# Reading a tree, read-only
# ---------------------------------------------------------------------------

def _read_bytes(path: Path) -> bytes:
    """The only filesystem read in this module, and it is the only one there
    should be: rule 1 in the module docstring is enforced by there being no
    other ``open`` here."""
    with open(path, "rb") as handle:
        return handle.read()


def _witness(path: Path) -> tuple[int, int, int]:
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _excluded(name: str, patterns: Sequence[str], normalize: Sequence[Normalization]) -> bool:
    """Match ``name`` against ``patterns``, exactly or as an fnmatch glob.

    The patterns are normalized the same way the names are, so an exclusion of
    ``M9.ARC`` still excludes the file cpmemu will have left named ``m9.arc``
    under ``lowercase_names``.
    """
    key = normalize_name(name, normalize)
    base = key.rsplit("/", 1)[-1]
    for pattern in patterns:
        pat = normalize_name(pattern, normalize)
        if key == pat or base == pat:
            return True
        if fnmatch.fnmatchcase(key, pat) or fnmatch.fnmatchcase(base, pat):
            return True
    return False


def collect_tree(
    root: str | os.PathLike[str],
    *,
    exclude: Sequence[str] = (),
    normalize: Sequence[Normalization] = (),
) -> dict[str, Path]:
    """Map relative POSIX name -> path for every regular file under ``root``.

    Symlinks are not followed and are not reported: a differential test that
    compared a symlink's target would be comparing something the guest never
    wrote. Directories are traversed but are not themselves entries, so an
    empty directory on one side is not a difference -- CP/M has none.

    ``exclude`` is how the staged inputs stay out of the count, and it is not
    cosmetic. Measured: with the staged ``M9.ARC`` still in the guest
    directory, ``diff -rq`` reports **47** "Only in" lines; excluded, **46**,
    which is the number in SPEC.md 7.2.
    """
    base = Path(root)
    out: dict[str, Path] = {}
    # followlinks=False is what stops a symlinked directory being descended
    # into; the per-entry islink check below is what stops a symlinked file
    # being read.
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames.sort()
        for filename in sorted(filenames):
            full = Path(dirpath) / filename
            if full.is_symlink() or not full.is_file():
                continue
            name = full.relative_to(base).as_posix()
            if exclude and _excluded(name, exclude, normalize):
                continue
            out[name] = full
    return out


# ---------------------------------------------------------------------------
# The diff engine
# ---------------------------------------------------------------------------

def _pair(
    guest_names: Iterable[str],
    reference_names: Iterable[str],
    normalize: Sequence[Normalization],
) -> tuple[list[tuple[str, str, str]], list[str], list[str]]:
    """Pair by normalized name. Returns (pairs, only_in_guest, only_in_reference).

    ``pairs`` entries are ``(key, guest_name, reference_name)``; the two names
    can differ, which is the whole point of ``lowercase_names``.
    """
    guest_map = _keyed(guest_names, normalize, "guest")
    reference_map = _keyed(reference_names, normalize, "reference")

    pairs = [
        (key, guest_map[key], reference_map[key])
        for key in sorted(guest_map.keys() & reference_map.keys())
    ]
    only_guest = sorted(guest_map[k] for k in guest_map.keys() - reference_map.keys())
    only_reference = sorted(reference_map[k] for k in reference_map.keys() - guest_map.keys())
    return pairs, only_guest, only_reference


def _keyed(
    names: Iterable[str], normalize: Sequence[Normalization], side: str
) -> dict[str, str]:
    """Normalized name -> original name, refusing a collision.

    Two files on one side that normalize to the same key would make the
    verdict depend on dict ordering. That is reported, not resolved: silently
    dropping one of ``FOO.TXT``/``foo.txt`` is how a differential test passes
    on a file it never looked at.
    """
    keyed: dict[str, str] = {}
    collisions: list[dict[str, str]] = []
    for name in names:
        key = normalize_name(name, normalize)
        if key in keyed:
            collisions.append({"key": key, "names": f"{keyed[key]}, {name}"})
            continue
        keyed[key] = name
    if collisions:
        raise ToolExecutionError(
            ToolError(
                error="normalization_collision",
                fields={
                    "side": side,
                    "normalize": [n.value for n in normalize],
                    "collisions": collisions,
                    "hint": (
                        "two files on the "
                        + side
                        + " side normalize to the same name; drop "
                        '"lowercase_names" or rename one of them'
                    ),
                },
            )
        )
    return keyed


def _first_diff_offset(left: bytes, right: bytes) -> int | None:
    """Offset of the first differing byte, or None if one is a prefix of the
    other (a pure length difference has no differing byte)."""
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index
    return None


def _suggestion(raw_guest: bytes, raw_reference: bytes, applied: Sequence[Normalization]) -> str | None:
    """"Adding X would have made these equal", when that is true.

    At most seven extra normalizations on a pair already known to differ, and
    it is the single most useful thing this tool can say: it is how a caller
    gets from the 21 differing ARC members to ``pad_to_record`` without
    reading SPEC.md 7.2 first.

    Searched in :data:`SUGGESTION_ORDER`, smallest set first, so the answer is
    the least destructive one that works and not merely the first that
    happens to. On the measured ARC both ``pad_to_record`` and
    ``strip_cpm_eof`` make all 21 identical -- the guest tail is 0x1A either
    way -- and the two are not interchangeable in general: padding adds bytes
    and can only ever equate a short file with a record-aligned one, while
    stripping deletes bytes and will also equate a guest file whose content
    genuinely ends in 0x1A with a reference file that has no marker at all.
    SPEC.md 6.3 names ``pad_to_record`` as the one that is REQUIRED for LBR
    and ARC members, so it is the one this points at.
    """
    missing = [m for m in SUGGESTION_ORDER if m not in applied]
    for count in range(1, len(missing) + 1):
        for combo in itertools.combinations(missing, count):
            candidate = tuple(applied) + combo
            if normalize_bytes(raw_guest, candidate) == normalize_bytes(raw_reference, candidate):
                added = ", ".join(f'"{m.value}"' for m in combo)
                return f"adding {added} to normalize makes these two identical"
    return None


def _note(
    guest: bytes,
    reference: bytes,
    raw_guest: bytes,
    raw_reference: bytes,
    normalize: Sequence[Normalization],
    compare: CompareMode,
) -> str | None:
    parts: list[str] = []
    if len(raw_guest) != len(guest) or len(raw_reference) != len(reference):
        parts.append(
            f"sizes are after normalization; on disk guest {len(raw_guest)}, "
            f"reference {len(raw_reference)}"
        )
    if len(guest) != len(reference):
        longer, shorter = sorted((len(guest), len(reference)), reverse=True)
        if shorter and longer == -(-shorter // RECORD_SIZE) * RECORD_SIZE:
            parts.append(
                f"{longer} == ceil({shorter}/{RECORD_SIZE})*{RECORD_SIZE}: one side "
                "wrote whole CP/M records"
            )
    suggestion = _suggestion(raw_guest, raw_reference, normalize)
    if suggestion:
        parts.append(suggestion)
    if compare is CompareMode.SHA256:
        parts.append('compare:"sha256" does not locate the first differing byte')
    return "; ".join(parts) if parts else None


def _compare_pair(
    name: str,
    raw_guest: bytes,
    raw_reference: bytes,
    normalize: Sequence[Normalization],
    compare: CompareMode,
) -> FileDiff | None:
    """None when the pair is identical under ``normalize``, else the FileDiff.

    ``guest_bytes``/``reference_bytes`` are the **normalized** sizes, because
    they are the numbers the verdict is about: a pair reported as differing
    with equal sizes and ``first_diff_offset:1537`` says "same length after
    padding, the pad bytes disagree", which the raw sizes 1664/1537 would
    hide. When normalization moved a length the note carries the on-disk
    sizes too.
    """
    guest = normalize_bytes(raw_guest, normalize)
    reference = normalize_bytes(raw_reference, normalize)

    if compare is CompareMode.SHA256:
        identical = sha256_hex(guest) == sha256_hex(reference)
        offset = None
    else:
        identical = guest == reference
        offset = None if identical else _first_diff_offset(guest, reference)

    if identical:
        return None
    return FileDiff(
        name=name,
        guest_bytes=len(guest),
        reference_bytes=len(reference),
        first_diff_offset=offset,
        note=_note(guest, reference, raw_guest, raw_reference, normalize, compare),
    )


def diff_files(
    guest: Mapping[str, bytes],
    reference: Mapping[str, bytes],
    *,
    normalize: Iterable[Any] = (),
    compare: CompareMode | str = CompareMode.BYTES,
) -> DiffResult:
    """Compare two in-memory name -> bytes mappings.

    The pure core of :func:`diff_trees`, and the form the unit tests use: it
    cannot touch a filesystem, so "normalization never reaches the disk" is
    true of it by construction.
    """
    members = parse_normalizations(normalize)
    mode = CompareMode(compare)

    pairs, only_guest, only_reference = _pair(guest.keys(), reference.keys(), members)

    diffs: list[FileDiff] = []
    for _key, guest_name, reference_name in pairs:
        entry = _compare_pair(
            guest_name, guest[guest_name], reference[reference_name], members, mode
        )
        if entry is not None:
            diffs.append(entry)

    return _result(members, pairs, diffs, only_guest, only_reference)


def diff_trees(
    guest_dir: str | os.PathLike[str],
    reference_dir: str | os.PathLike[str],
    *,
    normalize: Iterable[Any] = (),
    compare: CompareMode | str = CompareMode.BYTES,
    exclude: Sequence[str] = (),
) -> DiffResult:
    """Compare two directory trees. Reads both; writes to neither.

    ``exclude`` drops names from **both** sides -- the staged inputs, which
    the guest directory has and the reference output directory does not.

    Every file read is stat'd before and after and the engine refuses to
    return a verdict if any of them changed size, mtime or inode underneath
    it. That is the runtime half of rule 1 in the module docstring: a passing
    ``x80_diff_run`` has to mean the bytes on disk are the bytes that were
    compared.
    """
    members = parse_normalizations(normalize)
    mode = CompareMode(compare)

    guest_paths = collect_tree(guest_dir, exclude=exclude, normalize=members)
    reference_paths = collect_tree(reference_dir, exclude=exclude, normalize=members)

    pairs, only_guest, only_reference = _pair(
        guest_paths.keys(), reference_paths.keys(), members
    )

    before: dict[Path, tuple[int, int, int]] = {}
    diffs: list[FileDiff] = []
    for _key, guest_name, reference_name in pairs:
        guest_path = guest_paths[guest_name]
        reference_path = reference_paths[reference_name]
        before[guest_path] = _witness(guest_path)
        before[reference_path] = _witness(reference_path)
        entry = _compare_pair(
            guest_name,
            _read_bytes(guest_path),
            _read_bytes(reference_path),
            members,
            mode,
        )
        if entry is not None:
            diffs.append(entry)

    moved = sorted(str(p) for p, w in before.items() if _stat_or_none(p) != w)
    if moved:
        raise ToolExecutionError(
            ToolError(
                error="input_changed_during_compare",
                fields={
                    "paths": moved,
                    "reason": (
                        "size, mtime or inode changed while the comparison was "
                        "running; the verdict would not describe what is on disk"
                    ),
                    "hint": "make sure the guest and the reference command have both exited",
                },
            )
        )

    return _result(members, pairs, diffs, only_guest, only_reference)


def _stat_or_none(path: Path) -> tuple[int, int, int] | None:
    try:
        return _witness(path)
    except OSError:
        return None


def _result(
    members: Sequence[Normalization],
    pairs: Sequence[tuple[str, str, str]],
    diffs: Sequence[FileDiff],
    only_guest: Sequence[str],
    only_reference: Sequence[str],
) -> DiffResult:
    """``pass`` is true only when nothing differs **and** nothing is missing.

    A run where the guest produced 1 of 23 files and every one of the 1
    matched is not a pass; SPEC.md 7.1's negative case is exactly that shape.
    """
    compared = len(pairs)
    differ = len(diffs)
    return DiffResult(
        pass_=differ == 0 and not only_guest and not only_reference,
        normalization_applied=list(members),
        files_compared=compared,
        identical=compared - differ,
        differ=differ,
        only_in_guest=list(only_guest),
        only_in_reference=list(only_reference),
        diffs=list(diffs),
    )
