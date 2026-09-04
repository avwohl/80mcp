# evidence/

Raw transcripts from the verification pass behind [SPEC.md](../SPEC.md), captured
2026-09-04 on macOS 27 / arm64. Appendix A of the spec indexes every number back to a
run label here.

| dir | run |
|---|---|
| `t1` | CP/M 2.2 batch under `romwbw_emu` — stdout/stderr separation, boot timing, the idle-CPU measurement, and `nb.txt`: unpaced input being eaten by the romldr autoboot prompt |
| `t2` | CP/M 3 boot, `--boot=2.3` — the `[BANKED]` banner, and `stdout.txt` ending at `Press RETURN to Continue` (the pager caveat, spec §3.5) |
| `t3` | `80un.com` unpacking a 23-member ARC under `cpmemu`, and the differential run against the Python `un80` |
| `t4` | `dosiz` batch — `SORT.EXE` in a sandbox, exit-code propagation |
| `t5` | `mpm2_emu` booting MP/M II V2.1 — the loader output, the memory segment table, and the console-attribution defect (three prompts for four consoles; spec §3.4, correction 2) |
| `t6*.py` | the pty drivers used to reach the `sim>` debugger, which is unreachable over a pipe |

`t1/xdg/romwbw_emu/nvram` is kept deliberately: it is the file that proves an MCP run
mutates the user's persisted boot target unless `XDG_CONFIG_HOME` is redirected per
session (spec §5.4, Invariant 1).

**Text only.** The runs also produced third-party binaries — a DJGPP `SORT.EXE`, a 1986
CP/M `.ARC` and its extracted members — which are not committed here, matching the
family's practice of not bundling copyrighted content. The transcripts record what those
runs produced; rerunning them needs the fixtures from `dosiz/tests/` and `80un/tests/`.
