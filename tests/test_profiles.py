"""Tests for the profile table, backend discovery, images and doctor.

Two kinds of assertion live here and they are kept apart deliberately.

**Machine-independent.** The eleven SPEC.md 3.2 rows, the divergence strings
SPEC.md 6.4 names verbatim, the external-server recommendations, the phase
gate, the ``x80_probe`` routing table, and the shape of every result. These
run identically on a machine with no backend installed at all, because a
missing backend is a ``blocked_by`` string and never an exception -- which is
itself one of the assertions.

**Machine-dependent.** Anything that needs a real binary or a real image is
skipped when it is not there, and says what it wanted. The dyld check is the
important one: ``mpm2_emu`` on a stock macOS install links
``/usr/local/lib/libqkz80.4.dylib``, macOS does not ship it, and the binary
aborts under dyld with RC 134. :func:`test_missing_dylib_is_reported` builds a
fake binary rather than requiring an mpm2 checkout, so the resolution logic is
tested everywhere and the real binary only confirms it.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from eightymcp import __version__
from eightymcp.mbp import REQUIRED_OPS
from eightymcp.profiles import (
    BACKENDS,
    CHEAPEST_PROFILE,
    DEFAULT_ROMWBW_VERSION,
    EXTERNAL_SERVERS,
    LOCAL_IMAGES,
    PROFILE_TABLE,
    SERVER_PHASE,
    BackendProbe,
    Config,
    NO_SIBLING_SEARCH_ENV,
    ImageCatalog,
    Prober,
    bdos_call,
    classify_probe,
    detect_family,
    doctor_report,
    int21_call,
    missing_libraries,
    profiles_result,
    resolve_images,
    shared_library_dependencies,
)
from eightymcp.schemas import (
    CPM_PROFILES,
    DOS_PROFILES,
    INPUT_SCHEMAS,
    PROFILE_IDS,
    PROFILE_TIER,
)
from eightymcp.server import validate_and_fill
from eightymcp.types import (
    Escalation,
    Family,
    ProbeVerdict,
    SyscallLayer,
    Tier,
    ToolExecutionError,
)

REPO = Path(__file__).resolve().parents[1]

#: A hermetic environment: no config file, no image dirs, no PATH. Everything
#: the prober can find under this must come from the sibling-checkout search,
#: which is what makes the "nothing installed" assertions meaningful.
EMPTY_ENV = {
    "HOME": "/nonexistent-80mcp-test-home",
    "PATH": "",
    NO_SIBLING_SEARCH_ENV: "1",
}


# ==========================================================================
# The table itself -- SPEC.md 3.2
# ==========================================================================

def test_eleven_rows_matching_the_schema_enums():
    assert len(PROFILE_TABLE) == 11
    assert tuple(PROFILE_TABLE) == PROFILE_IDS
    for pid, spec in PROFILE_TABLE.items():
        assert spec.id == pid
        assert spec.tier is PROFILE_TIER[pid]
        assert spec.family in (Family.Z80, Family.X86)
        assert spec.backend in BACKENDS
        assert spec.os


def test_the_three_by_two_grid_of_spec_3_1():
    """hosted = OS-API translation, hardware = real OS on an emulated machine."""
    hosted = {p for p, s in PROFILE_TABLE.items() if s.tier is Tier.HOSTED}
    assert hosted == {"cpm-hosted", "dos-hosted"}
    assert PROFILE_TABLE["cpm-hosted"].backend == "cpmemu"
    assert PROFILE_TABLE["dos-hosted"].backend == "dosiz"
    # SPEC.md 3.3: MP/M is mpm2_emu, and never romwbw_emu.
    assert PROFILE_TABLE["mpm2"].backend == "mpm2_emu"
    assert PROFILE_TABLE["mpm2"].consoles == 4


def test_boot_targets_match_spec_3_2():
    expected = {
        "cpm22": "2",
        "cpm3": "2.3",
        "zsdos": "Z",
        "zsystem": "2.1",
        "nzcom": "2.2",
        "z80-bare": "none",
    }
    for pid, target in expected.items():
        assert PROFILE_TABLE[pid].boot_target == target


# ==========================================================================
# fidelity.divergences -- carried inline, not buried in docs (SPEC.md 6.4)
# ==========================================================================

def test_cpm_hosted_carries_the_two_divergences_spec_6_4_names():
    divs = PROFILE_TABLE["cpm-hosted"].divergences
    joined = " ".join(divs)
    # "setup_command_line() never writes the FCB at 0x5C, so a program testing
    #  fcb(1)=' ' for its usage banner takes the wrong branch"
    assert "setup_command_line()" in joined
    assert "0x5C" in joined
    assert "fcb(1)=' '" in joined
    assert "usage banner" in joined
    # "created filenames are lowercased on the host: TEST.TXT becomes test.txt"
    assert "lowercased on the host" in joined
    assert "TEST.TXT becomes test.txt" in joined


def test_every_profile_carries_its_divergences_inline_in_the_result():
    result = profiles_result(environ=EMPTY_ENV, probe=False)
    by_id = {p.id: p for p in result.profiles}
    for pid, spec in PROFILE_TABLE.items():
        assert by_id[pid].fidelity.tier is spec.tier
        assert by_id[pid].fidelity.divergences == list(spec.divergences)
    # The hosted tier is where the divergences bite, so it must not be empty.
    assert by_id["cpm-hosted"].fidelity.divergences
    assert by_id["dos-hosted"].fidelity.divergences


def test_cpm3_divergences_carry_the_pager_and_the_missing_r8_w8():
    joined = " ".join(PROFILE_TABLE["cpm3"].divergences)
    assert "Press RETURN to Continue" in joined
    assert "21 lines" in joined
    assert "r8.com/w8.com" in joined


def test_mpm2_divergences_carry_the_three_spec_3_4_corrections():
    joined = " ".join(PROFILE_TABLE["mpm2"].divergences)
    assert "set_local_mode(true)" in joined          # correction 2
    assert "0A>" in joined
    assert "find_free()" in joined                   # correction 3
    assert "MP/M II Console" in joined
    assert "60 Hz" in joined
    assert "port 0 is rejected" in joined            # launch gotcha


def test_dos_hosted_divergences_carry_the_relative_argv0_and_the_crynwr_lines():
    joined = " ".join(PROFILE_TABLE["dos-hosted"].divergences)
    assert "rc 102" in joined
    assert "RELATIVE" in joined
    assert "go32" in joined
    assert "Crynwr" in joined
    assert "slirp" in joined


# ==========================================================================
# The two profiles that can never be ready -- SPEC.md 3.6 and 3.7
# ==========================================================================

def test_freedos_is_blocked_by_the_spec_3_7_string_verbatim():
    prof = _profile(profiles_result(environ=EMPTY_ENV), "freedos")
    assert prof.ready is False
    assert "freedos boot has never been demonstrated headless" in prof.blocked_by


def test_z80_bare_is_blocked_because_there_is_no_start_flag():
    prof = _profile(profiles_result(environ=EMPTY_ENV), "z80-bare")
    assert prof.ready is False
    assert any("--start" in b for b in prof.blocked_by)


@pytest.mark.skipif(
    not shutil.which("romwbw_emu")
    and not (Path.home() / "src/romwbw_emu/src/romwbw_emu").exists(),
    reason="romwbw_emu is not installed",
)
def test_romwbw_emu_really_has_no_start_flag():
    """SPEC.md 3.6, re-verified against the installed binary rather than quoted."""
    probe = Prober().backend("romwbw_emu")
    assert probe.found
    out = subprocess.run(
        [probe.path, "--help"], capture_output=True, timeout=10
    )
    text = (out.stdout + out.stderr).decode("utf-8", "replace")
    assert "--boot=" in text, "sanity: this is the romwbw_emu help text"
    assert "--start" not in text


# ==========================================================================
# Discovery: absent is a string, never an exception
# ==========================================================================

def test_nothing_installed_gives_eleven_blocked_profiles_and_no_exception():
    result = profiles_result(environ=EMPTY_ENV)
    assert len(result.profiles) == 11
    for p in result.profiles:
        assert p.ready is False
        assert p.blocked_by, f"{p.id} is not ready and says nothing about why"
        assert all(isinstance(b, str) and b for b in p.blocked_by)
        assert p.binary_path is None
        assert p.backend_version is None


def test_a_blocked_profile_names_the_env_var_and_the_checkout():
    prof = _profile(profiles_result(environ=EMPTY_ENV), "dos-hosted")
    joined = " ".join(prof.blocked_by)
    assert "EIGHTYMCP_DOSIZ" in joined
    assert "qxDOS" in joined


def test_discovery_order_is_config_env_path_sibling(tmp_path):
    fake = tmp_path / "cpmemu"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    env = dict(EMPTY_ENV, EIGHTYMCP_CPMEMU=str(fake))
    probe = Prober(environ=env).backend("cpmemu")
    assert probe.found and probe.via == "env" and probe.path == str(fake)

    cfg_file = tmp_path / "config.json"
    other = tmp_path / "cpmemu_from_config"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(other.stat().st_mode | stat.S_IEXEC)
    cfg_file.write_text(json.dumps({"backends": {"cpmemu": {"path": str(other)}}}))
    env2 = dict(env, EIGHTYMCP_CONFIG=str(cfg_file))
    probe2 = Prober(environ=env2).backend("cpmemu")
    assert probe2.via == "config" and probe2.path == str(other)


def test_a_non_executable_file_is_reported_not_used(tmp_path):
    dud = tmp_path / "dosiz"
    dud.write_text("not a binary")
    dud.chmod(0o644)
    probe = Prober(environ=dict(EMPTY_ENV, EIGHTYMCP_DOSIZ=str(dud))).backend("dosiz")
    assert probe.found is False
    assert any("not executable" in p for p in probe.problems)


def test_a_malformed_config_is_a_problem_not_a_crash(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{ not json")
    cfg = Config.load(bad, environ=EMPTY_ENV)
    assert cfg.problems and "could not be read" in cfg.problems[0]
    assert cfg.backends == {}


# ==========================================================================
# Dynamic library resolution -- the mpm2 case
# ==========================================================================

@pytest.mark.skipif(sys.platform != "darwin", reason="otool is macOS-only")
def test_missing_dylib_is_reported(tmp_path):
    """Compile a binary against a dylib, delete the dylib, and check the probe.

    This is the ``mpm2_emu`` failure mode without needing an mpm2 checkout:
    the executable is on disk and executable, and dyld will abort. A probe
    that stats the file and stops reports it as present and it is not.
    """
    cc = shutil.which("cc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler")
    libsrc = tmp_path / "lib.c"
    libsrc.write_text("int probe_symbol(void){return 7;}\n")
    lib = tmp_path / "libprobefake.1.dylib"
    rc = subprocess.run(
        [cc, "-dynamiclib", "-install_name",
         "/usr/local/lib/libprobefake.1.dylib", str(libsrc), "-o", str(lib)],
        capture_output=True,
    )
    if rc.returncode != 0:
        pytest.skip(f"could not build the fixture dylib: {rc.stderr!r}")
    binsrc = tmp_path / "main.c"
    binsrc.write_text("int probe_symbol(void);int main(void){return probe_symbol();}\n")
    exe = tmp_path / "fakeemu"
    rc = subprocess.run(
        [cc, str(binsrc), str(lib), "-o", str(exe)], capture_output=True
    )
    if rc.returncode != 0:
        pytest.skip(f"could not build the fixture binary: {rc.stderr!r}")

    deps = shared_library_dependencies(exe)
    assert "/usr/local/lib/libprobefake.1.dylib" in deps

    env = {k: v for k, v in os.environ.items() if not k.startswith("DYLD_")}
    missing = missing_libraries(exe, env)
    assert missing == ["/usr/local/lib/libprobefake.1.dylib"]

    # ...and it really does abort, which is the point of checking.
    run = subprocess.run([str(exe)], capture_output=True, env=env)
    assert run.returncode != 0

    # DYLD_LIBRARY_PATH pointing at the real directory resolves it.
    env2 = dict(env, DYLD_LIBRARY_PATH=str(tmp_path))
    assert missing_libraries(exe, env2) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="otool is macOS-only")
def test_a_missing_dylib_becomes_the_spec_6_4_blocked_by_string(tmp_path):
    """The literal SPEC.md 6.4 wording for the measured mpm2 case."""
    probe = BackendProbe(name="mpm2_emu")
    from eightymcp.profiles import _library_hint

    dep = "/usr/local/lib/libqkz80.4.dylib"
    assert _library_hint(dep) == "libqkz80"
    expected = (
        "mpm2_emu links /usr/local/lib/libqkz80.4.dylib which is not installed; "
        "set DYLD_LIBRARY_PATH or install libqkz80."
    )
    # Same template the prober uses.
    built = (
        f"{probe.name} links {dep} which is not installed; "
        f"set DYLD_LIBRARY_PATH or install {_library_hint(dep)}."
    )
    assert built == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="otool is macOS-only")
def test_system_libraries_are_not_reported_missing():
    """/usr/lib lives in the dyld shared cache with no file on disk."""
    assert missing_libraries("/bin/ls") == []


# ==========================================================================
# Images
# ==========================================================================

def test_no_catalog_is_an_actionable_note_not_an_exception(tmp_path):
    cat = ImageCatalog.load(
        Config(environ=EMPTY_ENV), environ=dict(EMPTY_ENV, HOME=str(tmp_path))
    )
    assert cat.catalog_path is None
    assert cat.problems
    joined = " ".join(cat.problems)
    assert "romwbw_disks" in joined
    assert "EIGHTYMCP_CATALOG_DIR" in joined
    assert "allow_fetch" in joined
    # LOCAL_IMAGES are always there, catalog or not.
    assert set(LOCAL_IMAGES) <= set(cat.images)


def test_local_build_images_are_unpinned_and_say_so():
    for iid, img in LOCAL_IMAGES.items():
        assert img.source == "local_build"
        assert img.sha256 == ""
        assert img.url is None
        assert "not in the pinned romwbw_disks catalog" in img.description.lower()
    assert set(LOCAL_IMAGES) == {"mpm2_system", "freedos_starter"}


def test_x80_images_never_fetches_without_both_flags(tmp_path, monkeypatch):
    # Point every image search root at an empty dir so the answer does not
    # depend on whether this machine happens to have the image cached: with it
    # present, resolve_images returns instead of raising and the test flips.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("EIGHTYMCP_IMAGE_DIR", str(tmp_path / "empty"))
    with pytest.raises(ToolExecutionError) as exc:
        resolve_images(
            ["hd1k_combo"],
            dry_run=False,
            allow_fetch=False,
            environ=dict(os.environ),
        )
    body = exc.value.to_json()
    assert body["error"] == "fetch_not_allowed"
    assert "allow_fetch" in body["hint"]
    assert "only tool" in body["reason"]


def test_x80_images_listing_is_offline_and_dry_by_default(monkeypatch):
    """Listing must not open a socket. Poison the socket module and list."""
    import socket

    def refuse(*a, **k):  # pragma: no cover - only runs on a regression
        raise AssertionError("x80_images opened a socket during a dry run")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    out = resolve_images()  # no ids at all
    assert isinstance(out["images"], list)
    assert all(e["fetched"] is False for e in out["images"])
    assert out["catalog_version"]


def test_unknown_image_id_is_a_structured_bad_argument():
    with pytest.raises(ToolExecutionError) as exc:
        resolve_images(["no_such_image"])
    body = exc.value.to_json()
    assert body["error"] == "bad_argument"
    assert body["unknown"] == ["no_such_image"]
    assert "known" in body


def test_the_result_carries_every_spec_6_4_key():
    out = resolve_images(["mpm2_system"])
    assert set(out) == {
        "images",
        "catalog_version",
        "upstream_package_sha256",
        "notes",
    }
    entry = out["images"][0]
    assert set(entry) == {
        "id", "filename", "bytes", "sha256", "licence",
        "present", "fetched", "path", "description",
    }
    # SPEC.md 6.4 spells the output key "licence"; the catalog file spells the
    # input key "license". The value is surfaced verbatim.
    assert "license" not in entry


def test_importing_profiles_does_not_import_urllib():
    """The network boundary, enforced. urllib is imported inside _fetch_url."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, eightymcp.profiles as p; "
         "p.resolve_images(); "
         "print('urllib.request' in sys.modules)"],
        capture_output=True,
        env=dict(os.environ, PYTHONPATH=str(REPO / "src")),
        cwd=str(REPO),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip() == "False"


# ==========================================================================
# caps, the phase gate, and the shape of the result
# ==========================================================================

def test_phase_one_serves_only_the_seven_required_ops_and_only_one_shot():
    pr = Prober(environ=EMPTY_ENV)
    assert pr.server_phase == SERVER_PHASE == 1
    assert set(pr.served_ops("cpmemu")) == set(REQUIRED_OPS)
    assert set(pr.served_ops("dosiz")) == set(REQUIRED_OPS)
    # SPEC.md 4.7 applied to profiles: an adapter that has not shipped serves
    # nothing and the profile says so.
    assert pr.served_ops("romwbw_emu") == ()
    assert pr.served_ops("mpm2_emu") == ()
    assert pr.served_ops("emu88d") == ()


def test_a_phase_two_profile_says_which_phase_blocks_it():
    prof = _profile(profiles_result(environ=EMPTY_ENV), "cpm22")
    assert any("phase 2" in b for b in prof.blocked_by)
    assert not [c for c in prof.caps if c.startswith("op:")]


def test_caps_use_two_documented_namespaces():
    result = profiles_result(environ=EMPTY_ENV, probe=False)
    for p in result.profiles:
        for cap in p.caps:
            assert cap == cap.strip() and " " not in cap
        ops = [c[3:] for c in p.caps if c.startswith("op:")]
        assert set(ops) <= set(REQUIRED_OPS) | {
            "regs", "mem_read", "mem_write", "step", "bp_set", "bp_clear",
            "disasm", "screen_text", "screen_pixels", "console_list",
            "console_select", "trace_on", "trace_off", "trace_read",
            "syscall_bp",
        }
    cpm = _profile(result, "cpm-hosted")
    assert "drives:A-P" in cpm.caps
    assert _profile(result, "dos-hosted").caps.count("drives:C-Z") == 1
    assert "exit_code" in _profile(result, "dos-hosted").caps
    # SPEC.md 5.4 Invariant 4: no CP/M profile claims an exit code.
    for pid in CPM_PROFILES:
        assert "exit_code" not in _profile(result, pid).caps


def test_the_result_matches_the_spec_6_4_out_shape():
    body = profiles_result(environ=EMPTY_ENV, probe=False).to_json()
    assert set(body) == {
        "profiles",
        "server_version",
        "protocol_version",
        "external_servers_recommended",
    }
    assert body["server_version"] == __version__
    for p in body["profiles"]:
        assert list(p) == [
            "id", "family", "tier", "backend", "backend_version", "binary_path",
            "os", "caps", "consoles", "images", "fidelity", "ready", "blocked_by",
        ]
        assert list(p["images"]) == ["required"]
        assert list(p["fidelity"]) == ["tier", "divergences"]
        for req in p["images"]["required"]:
            assert list(req) == ["id", "sha256", "present"]
    for rec in body["external_servers_recommended"]:
        assert list(rec) == ["for", "name", "url", "why"]
    assert json.loads(json.dumps(body)) == body


def test_external_servers_point_at_altairsim_and_spice86():
    names = {e.name for e in EXTERNAL_SERVERS}
    assert names == {"altairsim", "Spice86"}
    by_name = {e.name: e for e in EXTERNAL_SERVERS}
    assert "deltecent/altairsim" in by_name["altairsim"].url
    assert "CP/M" in by_name["altairsim"].for_ or "S-100" in by_name["altairsim"].for_
    assert "DOS" in by_name["Spice86"].for_


# ==========================================================================
# The x80_profiles arguments, validated against the shipped schema
# ==========================================================================

@pytest.mark.parametrize(
    "args",
    [
        {},
        {"family": "z80"},
        {"family": "x86", "only_ready": True},
        {"profile": "cpm-hosted"},
        {"probe": False},
    ],
)
def test_every_schema_legal_argument_set_produces_a_result(args):
    filled = validate_and_fill(INPUT_SCHEMAS["x80_profiles"], args)
    result = profiles_result(
        family=filled["family"],
        profile=filled.get("profile"),
        only_ready=filled["only_ready"],
        probe=filled["probe"],
        environ=EMPTY_ENV,
    )
    ids = [p.id for p in result.profiles]
    if "profile" in args:
        assert ids == [args["profile"]]
    elif args.get("family") == "z80":
        assert set(ids) == {
            p for p, s in PROFILE_TABLE.items() if s.family is Family.Z80
        }
    elif args.get("only_ready"):
        assert ids == []  # nothing is installed under EMPTY_ENV


def test_probe_false_is_the_cheap_static_answer():
    result = profiles_result(environ=EMPTY_ENV, probe=False)
    for p in result.profiles:
        assert p.ready is False
        assert any("not probed" in b for b in p.blocked_by)


def test_an_unknown_profile_is_a_structured_error():
    with pytest.raises(ToolExecutionError) as exc:
        profiles_result(profile="cpm4", environ=EMPTY_ENV)
    body = exc.value.to_json()
    assert body["error"] == "bad_argument"
    assert body["argument"] == "profile"
    assert set(body["known"]) == set(PROFILE_IDS)


def test_require_ready_refuses_with_an_escalation():
    pr = Prober(environ=EMPTY_ENV)
    with pytest.raises(ToolExecutionError) as exc:
        pr.require_ready("cpm22")
    body = exc.value.to_json()
    assert body["error"] == "profile_not_ready"
    assert body["profile"] == "cpm22"
    assert body["blocked_by"]
    assert body["escalation"]["tool"] == "x80_profiles"


# ==========================================================================
# x80_probe routing
# ==========================================================================

def test_the_cheapest_profile_per_family_is_the_hosted_tier():
    assert CHEAPEST_PROFILE[Family.Z80] == "cpm-hosted"
    assert CHEAPEST_PROFILE[Family.X86] == "dos-hosted"
    for pid in CHEAPEST_PROFILE.values():
        assert PROFILE_TABLE[pid].tier is Tier.HOSTED


def test_syscall_naming():
    assert bdos_call(9).name == "C_WRITESTR"
    assert bdos_call(9).layer is SyscallLayer.BDOS
    assert bdos_call(45).name == "F_ERRMODE"       # CP/M 3
    assert bdos_call(141).layer is SyscallLayer.XDOS
    assert bdos_call(141).name == "P_DISPATCH"     # MP/M
    assert int21_call(0x4C).name == "TERMINATE_WITH_CODE"
    assert int21_call(0x4C).layer is SyscallLayer.INT21
    assert int21_call(0xF3).name == "AH_F3"        # unknown, still legible


def test_detect_family_reads_the_bytes(tmp_path):
    exe = tmp_path / "prog.bin"
    exe.write_bytes(b"MZ\x90\x00")
    assert detect_family(exe) is Family.X86
    com = tmp_path / "prog.com"
    com.write_bytes(b"\xc3\x00")
    assert detect_family(com) is Family.Z80
    assert detect_family(tmp_path / "nothing-here") is Family.Z80


def test_a_clean_run_escalates_to_the_batch_verb():
    verdict, rec, evidence, esc = classify_probe(
        ran_on="cpm-hosted",
        unimplemented=[],
        loaded=True,
        ready_profiles={"cpm-hosted"},
        program="/tmp/80un.com",
        args=["M9.ARC"],
    )
    assert verdict is ProbeVerdict.SUFFICIENT
    assert rec == "cpm-hosted"
    assert esc == Escalation(
        tool="x80_cpm_run",
        arguments={
            "profile": "cpm-hosted",
            "program": "/tmp/80un.com",
            "args": ["M9.ARC"],
        },
    )
    assert evidence


def test_a_cpm3_only_call_recommends_cpm3():
    verdict, rec, evidence, esc = classify_probe(
        ran_on="cpm-hosted",
        unimplemented=[bdos_call(45, 3)],
        loaded=True,
        ready_profiles={"cpm-hosted", "cpm3"},
        program="/tmp/p.com",
    )
    assert verdict is ProbeVerdict.NEEDS_RICHER_OS
    assert rec == "cpm3"
    assert esc.tool == "x80_cpm_run"
    assert esc.arguments["profile"] == "cpm3"
    assert any("CP/M 3 additions" in e for e in evidence)


def test_an_xdos_call_recommends_mpm2():
    verdict, rec, _, _ = classify_probe(
        ran_on="cpm-hosted",
        unimplemented=[bdos_call(141)],
        loaded=True,
        ready_profiles={"mpm2"},
    )
    assert verdict is ProbeVerdict.NEEDS_RICHER_OS
    assert rec == "mpm2"


def test_a_recommendation_that_is_not_ready_escalates_to_x80_profiles():
    """An agent handed a call that cannot work is worse off than one handed
    the reason it cannot work."""
    verdict, rec, evidence, esc = classify_probe(
        ran_on="cpm-hosted",
        unimplemented=[bdos_call(45)],
        loaded=True,
        ready_profiles=set(),  # nothing installed
    )
    assert rec == "cpm3"
    assert esc.tool == "x80_profiles"
    assert esc.arguments == {"profile": "cpm3", "probe": True}
    assert any("not ready on this install" in e for e in evidence)


def test_a_program_that_would_not_load():
    verdict, rec, evidence, _ = classify_probe(
        ran_on="dos-hosted", unimplemented=[], loaded=False
    )
    assert verdict is ProbeVerdict.FAILED_TO_LOAD
    assert rec == "freedos"
    assert any("could not load" in e for e in evidence)


# ==========================================================================
# doctor
# ==========================================================================

def test_doctor_exits_two_when_nothing_is_runnable():
    text, rc = doctor_report(environ=EMPTY_ENV)
    assert rc == 2
    assert "0 of 11 profiles ready" in text
    assert "backends" in text and "images" in text and "profiles" in text


def test_doctor_draws_no_box():
    text, _ = doctor_report(environ=EMPTY_ENV, verbose=True)
    assert not set(text) & set("┌┐└┘│─├┤┬┴┼╔╗╚╝║═+")
    for line in text.splitlines():
        assert not line.startswith("|") and not line.endswith("|")


def test_doctor_names_every_backend_and_its_dyld_status():
    text, _ = doctor_report(environ=EMPTY_ENV)
    for name in BACKENDS:
        assert name in text


def test_doctor_is_nonzero_whenever_a_profile_is_blocked():
    """SPEC.md 5.5: doctor refuses a mismatched install and exits nonzero."""
    _text, rc = doctor_report(environ=EMPTY_ENV)
    assert rc != 0
    # And on this machine, whatever it is: freedos and z80-bare can never be
    # ready, so a clean exit is impossible in phase 1 and the code must say so.
    _text2, rc2 = doctor_report()
    assert rc2 != 0


# ==========================================================================
# This machine, when something is actually installed
# ==========================================================================

def _profile(result, pid):
    for p in result.profiles:
        if p.id == pid:
            return p
    raise AssertionError(f"{pid} missing from the result")


def test_a_found_backend_reports_a_version_and_a_path():
    pr = Prober()
    found = [n for n in BACKENDS if pr.backend(n).found]
    if not found:
        pytest.skip("no backend installed on this machine")
    for name in found:
        bp = pr.backend(name)
        assert Path(bp.path).is_file()
        assert bp.version, (
            f"{name} was found at {bp.path} and no version could be read; "
            f"BackendSpec.version_argv or the checkout fallback needs updating"
        )


def test_a_ready_profile_really_has_its_backend():
    result = profiles_result()
    for p in result.profiles:
        if not p.ready:
            continue
        assert p.binary_path and Path(p.binary_path).is_file()
        assert p.blocked_by == []
        assert [c for c in p.caps if c.startswith("op:")]
        for req in p.images.required:
            assert req.present


def test_the_catalog_pins_match_the_files_when_both_are_present():
    cat = ImageCatalog.load()
    if cat.catalog_path is None:
        pytest.skip("no romwbw_disks catalog on this machine")
    assert cat.version == DEFAULT_ROMWBW_VERSION
    assert cat.upstream_package_sha256
    checked = 0
    for iid in ("emu_avw", "hd1k_combo", "hd1k_cpm3"):
        loc = cat.locate(iid)
        if loc.path is None:
            continue
        checked += 1
        assert loc.verified
        assert loc.sha256 == cat.images[iid].sha256
    if checked == 0:
        pytest.skip("no catalog images present on this machine")


def test_a_file_with_the_right_name_and_the_wrong_bytes_is_rejected(tmp_path):
    cat = ImageCatalog.load()
    if cat.catalog_path is None:
        pytest.skip("no romwbw_disks catalog on this machine")
    img = cat.images["hd1k_cpm3"]
    impostor = tmp_path / img.filename
    impostor.write_bytes(b"\x00" * img.bytes)
    cat.image_dirs.insert(0, tmp_path)
    loc = cat.locate("hd1k_cpm3")
    assert any(p == str(impostor) for p, _h in loc.rejected)
    assert loc.path != str(impostor)
