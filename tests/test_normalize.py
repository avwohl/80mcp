"""Tests for eightymcp.normalize: the four normalizations and the diff engine.

Three layers, deliberately:

* pure unit tests of the four members and the pipeline, which need nothing;
* a shape test over the **measured** 23-member size table from
  ``80un/tests/samples/arc/method9.arc``, which needs nothing either and
  pins the counting -- 46 "Only in" lines with no normalization, 2 identical
  under ``lowercase_names`` alone, 23 under both;
* the real acceptance, which runs ``cpmemu`` and ``python3 -m un80.cli`` and
  reproduces the same three numbers from actual bytes. It skips when the
  backend or the checkout is not installed, because SPEC.md's rule is that an
  absent backend is a skip and never a crash.

Two more, which exist because getting them wrong is silent:

* ``test_port_agrees_with_un80`` -- the ported ``strip_cpm_eof`` and
  ``crlf_to_lf`` against the originals in ``un80.cpm``, when a 80un checkout
  is present;
* ``test_diff_trees_does_not_touch_the_disk`` plus a source audit -- SPEC.md
  6.3 says normalization applies to the reported bytes and sha256 only.
"""

from __future__ import annotations

import os
import pathlib
import random
import shutil
import subprocess
import sys

import pytest

from eightymcp.normalize import (
    CONTENT_ORDER,
    CPM_EOF,
    RECORD_SIZE,
    SCHEMA_ORDER,
    SUGGESTION_ORDER,
    Reported,
    collect_tree,
    crlf_to_lf,
    diff_files,
    diff_trees,
    normalize_bytes,
    normalize_name,
    pad_to_record,
    parse_normalizations,
    report_bytes,
    sha256_hex,
    strip_cpm_eof,
)
from eightymcp.types import CompareMode, Normalization, ToolExecutionError

L = Normalization.LOWERCASE_NAMES
P = Normalization.PAD_TO_RECORD
S = Normalization.STRIP_CPM_EOF
C = Normalization.CRLF_TO_LF


# ---------------------------------------------------------------------------
# The measured corpus, from a real run recorded in this repo's evidence
# ---------------------------------------------------------------------------

# 80un.com under cpmemu vs `python3 -m un80.cli`, on
# 80un/tests/samples/arc/method9.arc (54842 bytes, 23 members).
# (reference name, guest bytes, reference bytes). Measured, not derived:
# guest == reference + 0x1A padding for all 23, and guest bytes ==
# ceil(reference bytes / 128) * 128 for all 23.
ARC_MEMBERS: tuple[tuple[str, int, int], ...] = (
    ("-03MAR86", 0, 0),
    ("B5-TIME.INF", 1664, 1537),
    ("B5C-2805.INS", 2816, 2689),
    ("B5C-5832.INS", 3072, 2954),
    ("B5C-BBII.INS", 2816, 2693),
    ("B5C-COMP.INS", 3968, 3845),
    ("B5C-CPM3.INS", 4608, 4484),
    ("B5C-CW.INS", 3200, 3075),
    ("B5C-DCH2.INS", 7808, 7688),
    ("B5C-H8F2.INS", 2048, 1931),
    ("B5C-KCT.INS", 1920, 1795),
    ("B5C-KP4.INS", 2688, 2561),
    ("B5C-KPRO.INS", 5248, 5125),
    ("B5C-LEG2.INS", 4480, 4357),
    ("B5C-MORE.INS", 8064, 7955),
    ("B5C-MTN2.INS", 4992, 4866),
    ("B5C-OKI1.INS", 5120, 5003),
    ("B5C-QX11.INS", 3712, 3675),
    ("B5C-SS1.INS", 2944, 2819),
    ("B5C-XERO.INS", 2560, 2437),
    ("B5C-ZCLK.INS", 5248, 5144),
    ("TIME2.ASM", 14720, 14720),
    ("ZTIM-S3.CPM", 1152, 1035),
)

# The two that match with no content normalization at all, both because they
# are already record-aligned: 0 == 0*128 and 14720 == 115*128.
ALREADY_ALIGNED = ("-03MAR86", "TIME2.ASM")

ARC_FIXTURE = "tests/samples/arc/method9.arc"


# ---------------------------------------------------------------------------
# The four normalizations
# ---------------------------------------------------------------------------

def test_crlf_to_lf_leaves_lone_cr_and_lone_lf_alone():
    assert crlf_to_lf(b"a\r\nb\r\n") == b"a\nb\n"
    # A CP/M text file that ends mid-record can carry a bare CR; rewriting it
    # would manufacture a difference.
    assert crlf_to_lf(b"a\rb\nc") == b"a\rb\nc"
    assert crlf_to_lf(b"") == b""
    assert crlf_to_lf(b"\r\n\r\n") == b"\n\n"


def test_strip_cpm_eof_strips_the_trailing_run_only():
    assert strip_cpm_eof(b"") == b""
    assert strip_cpm_eof(b"abc") == b"abc"
    assert strip_cpm_eof(b"abc\x1a") == b"abc"
    assert strip_cpm_eof(b"abc" + bytes([CPM_EOF]) * 125) == b"abc"
    assert strip_cpm_eof(bytes([CPM_EOF]) * 128) == b""
    # An embedded 0x1A followed by real data is data, not padding.
    assert strip_cpm_eof(b"a\x1ab") == b"a\x1ab"
    assert strip_cpm_eof(b"a\x1ab\x1a\x1a") == b"a\x1ab"


def test_aggressive_flag_is_a_no_op():
    # un80.cpm.strip_cpm_eof (src/un80/cpm.py:28-61) takes `aggressive`, but
    # its default branch breaks at the first byte that is not 0x1A, so its
    # "is everything after this also 0x1A?" test can never be false and the
    # two branches strip the same run. The keyword is kept for the
    # cross-check; it must not change anything.
    for sample in (b"", b"abc", b"abc\x1a\x1a", b"\x1a" * 5, b"a\x1ab\x1a"):
        assert strip_cpm_eof(sample) == strip_cpm_eof(sample, aggressive=True)


def test_pad_to_record_arithmetic():
    assert pad_to_record(b"") == b""            # ceil(0/128)*128 == 0
    assert len(pad_to_record(b"x")) == 128
    assert len(pad_to_record(b"x" * 128)) == 128
    assert len(pad_to_record(b"x" * 129)) == 256
    # The measured pair: reference 1537 -> 1664, guest 1664 unchanged.
    assert len(pad_to_record(b"x" * 1537)) == 1664
    assert len(pad_to_record(b"x" * 1664)) == 1664
    assert len(pad_to_record(b"x" * 14720)) == 14720


def test_pad_to_record_pads_with_0x1a():
    padded = pad_to_record(b"abc")
    assert padded[:3] == b"abc"
    assert set(padded[3:]) == {CPM_EOF}


def test_pad_to_record_rejects_a_nonsense_record_size():
    with pytest.raises(ValueError):
        pad_to_record(b"abc", record=0)


def test_pad_to_record_does_not_hide_a_wrong_pad_byte():
    # Padding is not permission to ignore the tail. A guest that padded with
    # 0x00 instead of 0x1A still differs, at the offset where the pad starts.
    guest = b"abc" + b"\x00" * 125
    reference = b"abc"
    result = diff_files({"f": guest}, {"f": reference}, normalize=[P])
    assert result.differ == 1
    assert result.diffs[0].first_diff_offset == 3


# ---------------------------------------------------------------------------
# Parsing and the pipeline
# ---------------------------------------------------------------------------

def test_parse_normalizations_canonicalises_order_and_dedupes():
    # SPEC.md 7.2 shows the echo as ["lowercase_names","pad_to_record"];
    # `normalize` is uniqueItems with no order semantics, so call order must
    # not survive into the result.
    assert parse_normalizations(["pad_to_record", "lowercase_names"]) == (L, P)
    assert parse_normalizations(["lowercase_names", "pad_to_record"]) == (L, P)
    assert parse_normalizations(["pad_to_record", "pad_to_record"]) == (P,)
    assert parse_normalizations(None) == ()
    assert parse_normalizations([]) == ()
    assert parse_normalizations([L, "crlf_to_lf"]) == (L, C)


def test_parse_normalizations_refuses_an_unknown_member():
    # A silently dropped `pad_to_record` reports identical:2 differ:21 and
    # looks like a real answer.
    with pytest.raises(ToolExecutionError) as excinfo:
        parse_normalizations(["lowercase_names", "true", "pad"])
    body = excinfo.value.to_json()
    assert body["error"] == "bad_argument"
    assert body["argument"] == "normalize"
    assert body["unknown"] == ["true", "pad"]
    assert body["allowed"] == [n.value for n in SCHEMA_ORDER]


def test_normalize_bytes_runs_strip_before_pad():
    # strip-then-pad is a canonical record form and is idempotent; the other
    # order would collapse the pair to a plain strip.
    guest = b"abc" + bytes([CPM_EOF]) * 125          # 128, guest record form
    reference = b"abc"                                # 3, exact host size
    assert normalize_bytes(guest, (S, P)) == normalize_bytes(reference, (S, P))
    assert len(normalize_bytes(guest, (S, P))) == 128
    once = normalize_bytes(guest, (S, P))
    assert normalize_bytes(once, (S, P)) == once


def test_normalize_bytes_ignores_lowercase_names():
    data = b"AbC\r\n"
    assert normalize_bytes(data, (L,)) == data


def test_normalize_bytes_runs_crlf_before_pad():
    # crlf_to_lf changes length, so it has to happen before the alignment or
    # the padding would be computed against the wrong size.
    data = b"a\r\n" * 50            # 150 bytes -> 100 after crlf
    out = normalize_bytes(data, (C, P))
    assert len(out) == 128
    assert out[:100] == b"a\n" * 50


def test_content_and_suggestion_orders_cover_the_same_set():
    assert set(CONTENT_ORDER) == set(SUGGESTION_ORDER)
    assert L not in CONTENT_ORDER


def test_normalize_name():
    assert normalize_name("B5-TIME.INF", ()) == "B5-TIME.INF"
    assert normalize_name("B5-TIME.INF", (L,)) == "b5-time.inf"
    assert normalize_name("sub/DIR/A.TXT", (L,)) == "sub/dir/a.txt"


# ---------------------------------------------------------------------------
# report_bytes -- x80_cpm_run's collect.normalize
# ---------------------------------------------------------------------------

def test_report_bytes_normalizes_the_report_and_says_the_raw_size():
    raw = b"x" * 1537
    plain = report_bytes(raw)
    assert (plain.bytes, plain.raw_bytes, plain.changed) == (1537, 1537, False)
    assert plain.sha256 == sha256_hex(raw)

    padded = report_bytes(raw, (P,))
    assert (padded.bytes, padded.raw_bytes, padded.changed) == (1664, 1537, True)
    assert padded.sha256 == sha256_hex(pad_to_record(raw))
    assert padded.sha256 != plain.sha256
    assert padded.to_json() == {"bytes": 1664, "sha256": padded.sha256}


def test_report_bytes_ignores_lowercase_names():
    raw = b"AbC"
    assert report_bytes(raw, (L,)).sha256 == sha256_hex(raw)


# ---------------------------------------------------------------------------
# The engine: pairing, counting, notes
# ---------------------------------------------------------------------------

def test_pairing_needs_lowercase_names():
    guest = {"b5-time.inf": b"x" * 1664}
    reference = {"B5-TIME.INF": b"x" * 1664}

    bare = diff_files(guest, reference)
    assert (bare.files_compared, bare.identical, bare.differ) == (0, 0, 0)
    assert bare.only_in_guest == ["b5-time.inf"]
    assert bare.only_in_reference == ["B5-TIME.INF"]
    assert bare.pass_ is False

    lowered = diff_files(guest, reference, normalize=[L])
    assert (lowered.files_compared, lowered.identical, lowered.differ) == (1, 1, 0)
    assert lowered.pass_ is True


def test_pass_is_false_when_a_file_is_missing_even_if_every_pair_matches():
    # SPEC.md 7.1's negative case: 1 of 23 extracted, and the 1 that was
    # written matched. That is not a pass.
    result = diff_files({"a": b"1"}, {"a": b"1", "b": b"2"})
    assert (result.files_compared, result.identical, result.differ) == (1, 1, 0)
    assert result.only_in_reference == ["b"]
    assert result.pass_ is False


def test_a_name_collision_is_reported_not_resolved():
    with pytest.raises(ToolExecutionError) as excinfo:
        diff_files({"A.TXT": b"1", "a.txt": b"2"}, {"a.txt": b"1"}, normalize=[L])
    body = excinfo.value.to_json()
    assert body["error"] == "normalization_collision"
    assert body["side"] == "guest"
    assert body["collisions"][0]["key"] == "a.txt"


def test_first_diff_offset_is_null_for_a_pure_length_difference():
    result = diff_files({"f": b"abcd"}, {"f": b"ab"})
    entry = result.diffs[0]
    assert entry.first_diff_offset is None
    assert (entry.guest_bytes, entry.reference_bytes) == (4, 2)


def test_first_diff_offset_locates_the_byte():
    result = diff_files({"f": b"abcd"}, {"f": b"abXd"})
    assert result.diffs[0].first_diff_offset == 2


def test_sha256_mode_matches_bytes_mode_on_the_verdict():
    guest = {"f": b"abcd", "g": b"same"}
    reference = {"f": b"abXd", "g": b"same"}
    as_bytes = diff_files(guest, reference)
    as_hash = diff_files(guest, reference, compare="sha256")
    assert (as_bytes.identical, as_bytes.differ) == (as_hash.identical, as_hash.differ)
    assert as_bytes.diffs[0].first_diff_offset == 2
    assert as_hash.diffs[0].first_diff_offset is None
    assert 'compare:"sha256"' in as_hash.diffs[0].note


def test_the_note_points_at_pad_to_record():
    # The one hint that gets a caller from 21 differing members to the right
    # normalization without reading the spec first.
    result = diff_files({"f": b"x" * 1537 + b"\x1a" * 127}, {"f": b"x" * 1537})
    note = result.diffs[0].note
    assert "1664 == ceil(1537/128)*128" in note
    assert '"pad_to_record"' in note
    # pad_to_record is preferred over strip_cpm_eof: both work here, and only
    # one of them equates two files by deleting content.
    assert '"strip_cpm_eof"' not in note


def test_the_note_reports_the_on_disk_sizes_when_normalization_moved_them():
    guest = {"f": b"a" * 1537}
    reference = {"f": b"b" * 1537}
    entry = diff_files(guest, reference, normalize=[P]).diffs[0]
    assert (entry.guest_bytes, entry.reference_bytes) == (1664, 1664)
    assert "on disk guest 1537, reference 1537" in entry.note


def test_the_comparison_is_symmetric():
    guest = {"a.txt": b"1", "only_g": b"x"}
    reference = {"A.TXT": b"2", "only_r": b"y"}
    forward = diff_files(guest, reference, normalize=[L])
    backward = diff_files(reference, guest, normalize=[L])
    assert forward.identical == backward.identical
    assert forward.differ == backward.differ
    assert forward.only_in_guest == backward.only_in_reference
    assert forward.only_in_reference == backward.only_in_guest


def test_result_json_shape():
    payload = diff_files({"f": b"abcd"}, {"f": b"abXd"}, normalize=["lowercase_names"]).to_json()
    assert list(payload) == [
        "pass", "normalization_applied", "files_compared", "identical", "differ",
        "only_in_guest", "only_in_reference", "diffs",
    ]
    assert payload["normalization_applied"] == ["lowercase_names"]
    assert list(payload["diffs"][0]) == [
        "name", "guest_bytes", "reference_bytes", "first_diff_offset", "note",
    ]


# ---------------------------------------------------------------------------
# Trees
# ---------------------------------------------------------------------------

def _write(root: pathlib.Path, name: str, data: bytes) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_collect_tree_is_recursive_and_skips_symlinks(tmp_path):
    root = tmp_path / "t"
    _write(root, "a.txt", b"a")
    _write(root, "sub/b.txt", b"b")
    (root / "link.txt").symlink_to(root / "a.txt")
    (root / "empty").mkdir()
    assert sorted(collect_tree(root)) == ["a.txt", "sub/b.txt"]


def test_exclude_drops_the_staged_input_by_its_normalized_name(tmp_path):
    root = tmp_path / "t"
    _write(root, "m9.arc", b"input")
    _write(root, "out.txt", b"out")
    # The caller staged it as M9.ARC; cpmemu left it lowercased.
    assert sorted(collect_tree(root, exclude=["M9.ARC"], normalize=[L])) == ["out.txt"]
    assert sorted(collect_tree(root, exclude=["*.arc"])) == ["out.txt"]
    assert sorted(collect_tree(root, exclude=[])) == ["m9.arc", "out.txt"]


def test_diff_trees_does_not_touch_the_disk(tmp_path):
    """SPEC.md 6.3: normalization applies to the REPORTED bytes and sha256
    only, never to the file on disk."""
    guest, reference = tmp_path / "guest", tmp_path / "ref"
    raw_guest = b"x" * 1537 + bytes([CPM_EOF]) * 127
    raw_reference = b"x" * 1537
    gpath = _write(guest, "b5-time.inf", raw_guest)
    rpath = _write(reference, "B5-TIME.INF", raw_reference)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (gpath, rpath)}

    result = diff_trees(guest, reference, normalize=["lowercase_names", "pad_to_record"])
    assert (result.identical, result.differ, result.pass_) == (1, 0, True)

    for path, (data, mtime) in before.items():
        assert path.read_bytes() == data
        assert path.stat().st_mtime_ns == mtime
    # And in particular the guest file is still 1664 and the reference still
    # 1537: the run reported them equal without making them equal.
    assert gpath.stat().st_size == 1664
    assert rpath.stat().st_size == 1537


def test_normalize_module_opens_nothing_for_writing():
    """The static half of the same rule. One read-only `open` in the module,
    and no rename/unlink/chmod/copy anywhere."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src" / "eightymcp" / "normalize.py"
    ).read_text()
    code_lines = [
        line for line in source.splitlines()
        if "open(" in line and not line.lstrip().startswith(("#", "*", '"""'))
    ]
    assert code_lines == ['    with open(path, "rb") as handle:']
    for forbidden in (
        "os.remove", "os.unlink", "os.rename", "os.replace", "os.truncate",
        "shutil.copy", "shutil.move", "shutil.rmtree", ".write_bytes",
        ".write_text", ".unlink(", ".mkdir(", "os.chmod",
    ):
        assert forbidden not in source, forbidden


def test_diff_trees_refuses_a_verdict_if_a_file_moves_under_it(tmp_path, monkeypatch):
    guest, reference = tmp_path / "guest", tmp_path / "ref"
    gpath = _write(guest, "a.txt", b"same")
    _write(reference, "a.txt", b"same")

    import eightymcp.normalize as normalize_module

    real_read = normalize_module._read_bytes

    def read_then_scribble(path):
        data = real_read(path)
        if path == gpath:
            os.utime(gpath, ns=(0, 0))
        return data

    monkeypatch.setattr(normalize_module, "_read_bytes", read_then_scribble)
    with pytest.raises(ToolExecutionError) as excinfo:
        diff_trees(guest, reference)
    body = excinfo.value.to_json()
    assert body["error"] == "input_changed_during_compare"
    assert str(gpath) in body["paths"]


# ---------------------------------------------------------------------------
# The measured ARC, as shape
# ---------------------------------------------------------------------------

def _synthetic_arc_pair(seed: int = 0):
    """Build a guest/reference pair with the measured sizes.

    The content is arbitrary but the sizes and the pad byte are the measured
    ones, so the counts this produces -- 46, 2/21, 23/0 -- come out of the
    real size table and not out of an assumption about them. The bytes-level
    claim (guest == reference + 0x1A padding, for all 23) is checked against
    the real files in ``test_acceptance_real_arc``.
    """
    rng = random.Random(seed)
    guest: dict[str, bytes] = {}
    reference: dict[str, bytes] = {}
    for name, guest_bytes, reference_bytes in ARC_MEMBERS:
        body = bytes(rng.randrange(1, 26) for _ in range(reference_bytes))
        reference[name] = body
        guest[name.lower()] = body + bytes([CPM_EOF]) * (guest_bytes - reference_bytes)
    return guest, reference


def test_arc_shape_no_normalization_is_46_only_in_lines_and_0_matches():
    guest, reference = _synthetic_arc_pair()
    result = diff_files(guest, reference)
    assert len(result.only_in_guest) + len(result.only_in_reference) == 46
    assert (result.files_compared, result.identical, result.differ) == (0, 0, 0)
    assert result.pass_ is False


def test_arc_shape_lowercase_names_alone_is_2_and_21():
    guest, reference = _synthetic_arc_pair()
    result = diff_files(guest, reference, normalize=["lowercase_names"])
    assert (result.files_compared, result.identical, result.differ) == (23, 2, 21)
    matched = {name for name, _g, _r in ARC_MEMBERS} - {d.name.upper() for d in result.diffs}
    assert sorted(matched) == sorted(ALREADY_ALIGNED)


def test_arc_shape_both_normalizations_is_23_of_23():
    guest, reference = _synthetic_arc_pair()
    result = diff_files(guest, reference, normalize=["lowercase_names", "pad_to_record"])
    assert (result.files_compared, result.identical, result.differ) == (23, 23, 0)
    assert result.pass_ is True
    assert result.to_json()["normalization_applied"] == ["lowercase_names", "pad_to_record"]


# ---------------------------------------------------------------------------
# The real thing: cpmemu + un80, when both are installed
# ---------------------------------------------------------------------------

def _find_un80() -> pathlib.Path | None:
    candidates = []
    if os.environ.get("X80_UN80_SRC"):
        candidates.append(pathlib.Path(os.environ["X80_UN80_SRC"]))
    candidates += [
        pathlib.Path.home() / "src" / "80un",
        pathlib.Path(__file__).resolve().parent.parent.parent / "80un",
    ]
    scratch = os.environ.get("CLAUDE_SCRATCHPAD")
    if scratch:
        candidates.append(pathlib.Path(scratch) / "80un")
    for candidate in candidates:
        if (candidate / "src" / "un80" / "cpm.py").is_file():
            return candidate
    return None


def _find_cpmemu() -> str | None:
    explicit = os.environ.get("X80_CPMEMU")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("cpmemu")
    if found:
        return found
    for candidate in (pathlib.Path.home() / "src" / "cpmemu" / "src" / "cpmemu",):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


UN80 = _find_un80()
CPMEMU = _find_cpmemu()


@pytest.mark.skipif(UN80 is None, reason="no 80un checkout; set X80_UN80_SRC")
def test_port_agrees_with_un80():
    """The ported strip_cpm_eof/crlf_to_lf against the originals.

    This is the guard that makes porting the right call instead of a risk:
    the engine has no runtime dependency on 80un, and any machine that has a
    checkout still catches drift. If it ever fails, the port is wrong and
    every differential verdict this engine produced is suspect.
    """
    sys.path.insert(0, str(UN80 / "src"))
    try:
        from un80 import cpm as un80_cpm  # noqa: PLC0415
    finally:
        sys.path.pop(0)

    assert un80_cpm.CPM_EOF == CPM_EOF

    corpus: list[bytes] = [
        b"", b"a", b"\x1a", b"\x1a" * 128, b"abc\x1a", b"abc" + b"\x1a" * 125,
        b"a\x1ab", b"a\x1ab\x1a\x1a", b"\r\n", b"a\r\nb\rc\nd\x1a\x1a",
        b"\r", b"\n\r", b"\r\n\x1a",
    ]
    rng = random.Random(20260728)
    alphabet = bytes([0x00, 0x0A, 0x0D, 0x1A, 0x41, 0xFF])
    for _ in range(400):
        corpus.append(bytes(rng.choice(alphabet) for _ in range(rng.randrange(0, 40))))
    if (UN80 / ARC_FIXTURE).is_file():
        corpus.append((UN80 / ARC_FIXTURE).read_bytes())

    for sample in corpus:
        assert strip_cpm_eof(sample) == un80_cpm.strip_cpm_eof(sample), sample
        assert strip_cpm_eof(sample, aggressive=True) == un80_cpm.strip_cpm_eof(
            sample, aggressive=True
        ), sample
        # The two branches of the original agree too, which is why the
        # keyword is a no-op here.
        assert un80_cpm.strip_cpm_eof(sample) == un80_cpm.strip_cpm_eof(
            sample, aggressive=True
        ), sample
        assert crlf_to_lf(sample) == un80_cpm.crlf_to_lf(sample), sample


@pytest.mark.skipif(
    UN80 is None or CPMEMU is None,
    reason="needs a 80un checkout (X80_UN80_SRC) and a cpmemu binary (X80_CPMEMU)",
)
def test_acceptance_real_arc(tmp_path):
    """SPEC.md 9 phase 1: identical:23 differ:0 under
    ["lowercase_names","pad_to_record"], identical:2 differ:21 under
    lowercase_names alone, and 46 "Only in" lines with none."""
    assert UN80 is not None and CPMEMU is not None
    fixture = UN80 / ARC_FIXTURE
    if not fixture.is_file():
        pytest.skip(f"no {ARC_FIXTURE} in the 80un checkout")

    guest = tmp_path / "guest"
    guest.mkdir()
    shutil.copyfile(fixture, guest / "M9.ARC")
    home, xdg = tmp_path / "home", tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    cfg = tmp_path / "run.cfg"
    cfg.write_text(
        f"program = {UN80 / '80un.com'}\n"
        f"cd = {guest}\n"
        "default_mode = binary\n"      # SPEC.md 6.4: never auto, it truncates
        "eol_convert = false\n"
        f"printer = {tmp_path / '.lst'}\n"
        f"aux_output = {tmp_path / '.pun'}\n"
    )
    completed = subprocess.run(
        [CPMEMU, str(cfg), "M9.ARC"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_CONFIG_HOME": str(xdg)},
    )
    assert b"23 file(s) extracted" in completed.stdout, completed.stdout
    assert b"Program exit via JMP 0" in completed.stderr, completed.stderr
    assert (guest / "b5-time.inf").stat().st_size == 1664

    reference = tmp_path / "ref"
    subprocess.run(
        [sys.executable, "-m", "un80.cli", str(fixture), "-o", str(reference)],
        cwd=UN80,
        env={**os.environ, "PYTHONPATH": str(UN80 / "src")},
        capture_output=True,
        check=True,
        timeout=60,
    )
    assert (reference / "B5-TIME.INF").stat().st_size == 1537

    bare = diff_trees(guest, reference, exclude=["M9.ARC"])
    assert len(bare.only_in_guest) + len(bare.only_in_reference) == 46
    assert bare.identical == 0

    lowered = diff_trees(guest, reference, normalize=["lowercase_names"], exclude=["M9.ARC"])
    assert (lowered.files_compared, lowered.identical, lowered.differ) == (23, 2, 21)

    both = diff_trees(
        guest, reference, normalize=["lowercase_names", "pad_to_record"], exclude=["M9.ARC"]
    )
    assert (both.files_compared, both.identical, both.differ) == (23, 23, 0)
    assert both.pass_ is True

    # And the size table this file carries is still the truth.
    for name, guest_bytes, reference_bytes in ARC_MEMBERS:
        assert (guest / name.lower()).stat().st_size == guest_bytes, name
        assert (reference / name).stat().st_size == reference_bytes, name
        raw_reference = (reference / name).read_bytes()
        assert (guest / name.lower()).read_bytes() == raw_reference + bytes(
            [CPM_EOF]
        ) * (guest_bytes - reference_bytes), name
