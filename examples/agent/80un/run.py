#!/usr/bin/env python3
"""The 80mcp phase-1 batch exercise, end to end, against a real cpmemu.

Nine calls: prove a 1986 CP/M archive unpacks byte-correctly, then break it
four ways on purpose so the failure signatures are familiar.

    python3 examples/agent/80un/run.py --un80 /path/to/80un

80un is not vendored here. It is https://github.com/avwohl/80un and it is
found, in order, from --un80, $X80_UN80_SRC, ~/src/80un, or a checkout
sitting beside this repo. cpmemu is found by the server itself (SPEC.md 5.2):
$EIGHTYMCP_CPMEMU, the config file, $PATH, then a sibling checkout.

Exit 0 if every expectation in EXPECTED held, 1 otherwise. Nothing here is
composed: every number in EXPECTED was measured by this script on
cpmemu 4.8.0 / macOS 27 arm64, and the transcript is in expected/.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ARC_REL = "tests/samples/arc/method9.arc"

# Measured, not asserted from theory. Each entry is (label, value) checked
# against the live run at the bottom of main().
EXPECTED = {
    "profiles.ready": ["cpm-hosted"],
    "probe.verdict": "sufficient",
    "probe.recommended": "cpm-hosted",
    "run.pass": True,
    "run.exit_reason": "jmp_0",
    "run.files": 23,
    "run.b5_time_inf_bytes": 1664,
    "run.b5_time_inf_host_name": "b5-time.inf",
    "run.has_exit_code_key": False,
    "list.since_start": 23,
    "diff.both.identical": 23,
    "diff.both.differ": 0,
    "diff.lowercase_only.identical": 2,
    "diff.lowercase_only.differ": 21,
    "diff.none.only_in_guest": 23,
    "diff.none.only_in_reference": 23,
    "auto.pass": False,
    "auto.files": 2,
    "auto.b5_time_inf_bytes": 1437,
    "truncated.pass": False,
    "truncated.exit_reason": "timeout",
}


def find_un80(explicit: str | None) -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    repo = here.parent.parent.parent.parent
    for cand in filter(None, [
        explicit,
        os.environ.get("X80_UN80_SRC"),
        os.environ.get("EIGHTYMCP_80UN"),
        pathlib.Path.home() / "src" / "80un",
        repo.parent / "80un",
    ]):
        p = pathlib.Path(cand)
        if (p / "80un.com").is_file() and (p / ARC_REL).is_file():
            return p
    sys.exit(
        "no 80un checkout found. Clone https://github.com/avwohl/80un and pass\n"
        "  --un80 /path/to/80un   (or set $X80_UN80_SRC)\n"
        f"Looked for 80un.com and {ARC_REL} under each candidate."
    )


def server_cmd() -> list[str]:
    if shutil.which("80mcp"):
        return ["80mcp"]
    return [sys.executable, "-m", "eightymcp.cli"]


def rpc(calls: list[dict]) -> list[dict]:
    """One server process, all calls, newline-delimited JSON on stdio."""
    proc = subprocess.Popen(
        server_cmd(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    payload = "".join(json.dumps(c) + "\n" for c in calls).encode()
    out, err = proc.communicate(payload, timeout=600)
    if proc.returncode not in (0, None):
        sys.stderr.write(err.decode())
    replies = [json.loads(line) for line in out.decode().splitlines()]
    for r in replies:
        if "error" in r:
            sys.exit(f"JSON-RPC error on id {r.get('id')}: {json.dumps(r['error'], indent=2)}")
    return replies


def call(tool: str, args: dict, ident: int) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
            "params": {"name": tool, "arguments": args,
                       "_meta": {"protocolVersion": "2026-07-28"}}}


def sc(reply: dict) -> dict:
    return reply["result"]["structuredContent"]


def head(n: int, title: str) -> None:
    print(f"\n{'=' * 72}\n{n}. {title}\n{'=' * 72}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--un80", help="path to a 80un checkout")
    ap.add_argument("--keep", action="store_true", help="leave the sandboxes on disk")
    args = ap.parse_args()

    un80 = find_un80(args.un80)
    com, arc = str(un80 / "80un.com"), str(un80 / ARC_REL)
    print(f"80un   {un80}")
    print(f"server {' '.join(server_cmd())}")

    work = pathlib.Path(tempfile.mkdtemp(prefix="80un-exercise-"))
    # The deliberately broken fixture, made here rather than committed: this
    # repo does not ship third-party CP/M content (see evidence/README.md).
    raw = pathlib.Path(arc).read_bytes()
    bad = work / "method9-truncated.arc"
    bad.write_bytes(raw[: len(raw) // 2])

    files_in = [{"guest_name": "M9.ARC", "host_path": arc, "mode": "binary"}]
    reference = {"argv": ["python3", "-m", "un80.cli", "{input}", "-o", "{outdir}"],
                 "env": {"PYTHONPATH": "src"}, "cwd": str(un80)}
    guest = {"profile": "cpm-hosted", "program": com, "args": ["M9.ARC"],
             "default_mode": "binary", "eol_convert": False}

    def diff(norm: list[str], ident: int) -> dict:
        return call("x80_diff_run", {
            "guest": guest, "reference": reference, "inputs": files_in,
            "normalize": norm, "compare": "bytes", "timeout_ms": 60000}, ident)

    replies = rpc([
        call("x80_profiles", {"family": "z80", "only_ready": True}, 1),
        call("x80_probe", {"program": com, "args": ["M9.ARC"], "files_in": files_in}, 2),
        call("x80_cpm_run", {
            "profile": "cpm-hosted", "cpu": "z80", "program": com, "args": ["M9.ARC"],
            "files_in": files_in, "default_mode": "binary", "eol_convert": False,
            "timeout_ms": 10000, "collect": {"return_content": "never"},
            "assert": {"stdout_contains": ["23 file(s) extracted"],
                       "stdout_not_contains": ["Error", "Cannot open", "Invalid"],
                       "files_created": ["TIME2.ASM", "ZTIM-S3.CPM"],
                       "no_unimplemented_bdos": True},
            "keep_sandbox": True}, 3),
        diff(["lowercase_names", "pad_to_record"], 4),
        diff(["lowercase_names"], 5),
        diff([], 6),
        # Trap 1: default_mode auto. SPEC.md 5.4 Invariant 3.
        call("x80_cpm_run", {
            "profile": "cpm-hosted", "program": com, "args": ["M9.ARC"],
            "files_in": files_in, "default_mode": "auto", "eol_convert": True,
            "timeout_ms": 10000, "collect": {"return_content": "never"},
            "assert": {"stdout_contains": ["23 file(s) extracted"]}}, 7),
        # Trap 2: a truncated archive. Stdout looks healthy right up to the cut.
        call("x80_cpm_run", {
            "profile": "cpm-hosted", "program": com, "args": ["BAD.ARC"],
            "files_in": [{"guest_name": "BAD.ARC", "host_path": str(bad), "mode": "binary"}],
            "timeout_ms": 10000, "collect": {"return_content": "never"},
            "assert": {"stdout_contains": ["23 file(s) extracted"]}}, 8),
        # Trap 3: a phase-5 op, answered structurally rather than by failing.
        call("x80_files", {"op": "resolve", "sandbox": "/nonexistent",
                           "resolve_name": "A:FOO.TXT"}, 9),
    ])
    by_id = {r["id"]: r for r in replies}

    head(1, "x80_profiles - what can this installation actually run?")
    profs = sc(by_id[1])["profiles"]
    for p in profs:
        print(f"  {p['id']:<12} {p['backend']} {p['backend_version']}  ready={p['ready']}")
    print("  fidelity.divergences:")
    for d in profs[0]["fidelity"]["divergences"]:
        print(f"    - {d}")

    head(2, "x80_probe - what OS does this program actually need?")
    p2 = sc(by_id[2])
    print(f"  verdict {p2['verdict']}  recommended {p2['recommended_profile']}")
    for e in p2["evidence"]:
        print(f"    {e}")
    print(f"  escalation: {json.dumps(p2['escalation'])}")

    head(3, "x80_cpm_run - the batch verb, default_mode binary")
    r3 = sc(by_id[3])
    print(f"  pass={r3['pass']}  exit_reason={r3['exit_reason']}  "
          f"wall_ms={r3['wall_ms']}  files_out={len(r3['files_out'])}")
    print(f"  'exit_code' among the result keys: {'exit_code' in r3}   <- SPEC.md 5.4 Invariant 4")
    for a in r3["assertions"]:
        print(f"    {'ok  ' if a['ok'] else 'FAIL'} {a['kind']} {a['value']!r}")
    b5 = next(f for f in r3["files_out"] if f["guest_name"] == "B5-TIME.INF")
    print(f"  B5-TIME.INF -> host {b5['host_name']}, {b5['bytes']} bytes  "
          f"<- lowercased on create, and padded to a 128-byte record")
    print(f"  stderr: {r3['stderr'].splitlines()[-1]}")
    sandbox = r3["sandbox"]

    head(4, "x80_files - what did the run really create?")
    listing = sc(rpc([call("x80_files", {"op": "list", "sandbox": sandbox,
                                         "since": "start"}, 10)])[0])
    print(f"  {len(listing['files'])} files created since the run started")
    for f in listing["files"][:3]:
        print(f"    {f['guest_name']:<14} -> {f['host_name']:<14} {f['bytes']:>6} bytes")
    print("    ...")

    head(5, "x80_diff_run - the same algorithm as a CP/M .COM and as Python")
    rows = []
    for label, ident in (("['lowercase_names','pad_to_record']", 4),
                         ("['lowercase_names']", 5),
                         ("[]  (no normalization)", 6)):
        d = sc(by_id[ident])
        rows.append((label, d))
        print(f"  normalize={label}")
        print(f"    compared {d['files_compared']}  identical {d['identical']}  "
              f"differ {d['differ']}  only-in "
              f"{len(d['only_in_guest']) + len(d['only_in_reference'])}")
    note = next((x["note"] for x in sc(by_id[5])["diffs"] if x["note"]), None)
    if note:
        print(f"  why 21 differ: {note}")

    head(6, "Trap 1 - default_mode auto (SPEC.md 5.4 Invariant 3)")
    r7 = sc(by_id[7])
    print(f"  pass={r7['pass']}  exit_reason={r7['exit_reason']}  "
          f"files_out={len(r7['files_out'])}   process rc was 0 either way")
    print(f"  stdout ends: {r7['stdout'].strip().splitlines()[-1]!r}")
    for f in r7["files_out"]:
        print(f"    {f['guest_name']:<14} {f['bytes']:>6} bytes")
    for w in r7["warnings"]:
        print(f"  warning: {w}")

    head(7, "Trap 2 - a truncated archive. Stdout lies; exit_reason does not.")
    r8 = sc(by_id[8])
    print(f"  pass={r8['pass']}  exit_reason={r8['exit_reason']}  "
          f"files_out={len(r8['files_out'])} of 23")
    print(f"  last stdout line: {r8['stdout'].strip().splitlines()[-1]!r}  "
          f"<- no error message anywhere")

    head(8, "Trap 3 - a phase-5 op answers structurally, not with a bare failure")
    print("  isError:", by_id[9]["result"]["isError"])
    print(" ", json.dumps(sc(by_id[9]), indent=2)[:420], "...")

    head(9, "Checking every measured expectation")
    d_both, d_low, d_none = (sc(by_id[i]) for i in (4, 5, 6))
    actual = {
        "profiles.ready": [p["id"] for p in profs],
        "probe.verdict": p2["verdict"],
        "probe.recommended": p2["recommended_profile"],
        "run.pass": r3["pass"],
        "run.exit_reason": r3["exit_reason"],
        "run.files": len(r3["files_out"]),
        "run.b5_time_inf_bytes": b5["bytes"],
        "run.b5_time_inf_host_name": b5["host_name"],
        "run.has_exit_code_key": "exit_code" in r3,
        "list.since_start": len(listing["files"]),
        "diff.both.identical": d_both["identical"],
        "diff.both.differ": d_both["differ"],
        "diff.lowercase_only.identical": d_low["identical"],
        "diff.lowercase_only.differ": d_low["differ"],
        "diff.none.only_in_guest": len(d_none["only_in_guest"]),
        "diff.none.only_in_reference": len(d_none["only_in_reference"]),
        "auto.pass": r7["pass"],
        "auto.files": len(r7["files_out"]),
        "auto.b5_time_inf_bytes": next(
            (f["bytes"] for f in r7["files_out"] if f["guest_name"] == "B5-TIME.INF"), None),
        "truncated.pass": r8["pass"],
        "truncated.exit_reason": r8["exit_reason"],
    }
    bad_rows = [(k, EXPECTED[k], actual[k]) for k in EXPECTED if actual[k] != EXPECTED[k]]
    for k in EXPECTED:
        mark = "ok  " if actual[k] == EXPECTED[k] else "FAIL"
        print(f"  {mark} {k:<32} {actual[k]!r}")
    if bad_rows:
        print(f"\n{len(bad_rows)} expectation(s) did not hold:")
        for k, want, got in bad_rows:
            print(f"  {k}: expected {want!r}, measured {got!r}")

    if args.keep:
        print(f"\nsandbox kept at {sandbox}\nfixtures in {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{len(EXPECTED) - len(bad_rows)}/{len(EXPECTED)} expectations held")
    return 1 if bad_rows else 0


if __name__ == "__main__":
    sys.exit(main())
