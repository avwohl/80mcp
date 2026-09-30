# What ships, and what does not

Phase 1 status of 80mcp: the `80mcp doctor` output, how backends are found, the protocol revision, and the tool and profile tables. Back to the [README](../README.md).

## 80mcp doctor

`80mcp doctor` is the first thing to run and the first thing to paste into a
bug report. On a machine with only `cpmemu` built it prints the backend table,
the pinned image catalog, and every profile with its blocking reason:

```
80mcp 0.1.0  phase 1  protocol 2026-07-28
backends
  name        status     version  path
  cpmemu      ok         4.8.0    /Users/…/cpmemu/src/cpmemu
  dosiz       not found  -        -
profiles
  id          family  tier      backend     consoles  ready
  cpm-hosted  z80     hosted    cpmemu      1         yes
  cpm3        z80     hardware  romwbw_emu  1         no
  …
  cpm3 blocked:
    - the romwbw_emu adapter ships in phase 2; this build is phase 1 and
      ships the one-shot adapter only (SPEC.md 9)

1 of 11 profiles ready: cpm-hosted
```

**Backends are not vendored.** `cpmemu` and `dosiz` are found at runtime in
`$EIGHTYMCP_CPMEMU` / `$EIGHTYMCP_DOSIZ`, in
`$XDG_CONFIG_HOME/80mcp/config.json`, on `$PATH`, or in a sibling checkout. A
missing one is a profile reporting `ready:false` with an actionable
`blocked_by`, never a crash.

The server speaks MCP **2026-07-28** (`server/discover`, `_meta.protocolVersion`,
`resultType`, `ttlMs`/`cacheScope`) and still accepts the legacy `initialize`
handshake from clients on 2025-11-25 or 2025-06-18, replying with the highest
revision both sides support and omitting the 2026-only envelope fields on that
connection. Every client shipping today still opens with `initialize`.

## Tools and profiles

Seven batch tools over two backends. The other sixteen tools are **not
registered** — not stubbed, not erroring, absent — because a tool that is
listed and always fails is a lie in `tools/list` (SPEC.md 4.7).

| tool | phase | state |
|---|---|---|
| `x80_profiles` | 1 | **ships** — the profile table, capabilities, fidelity divergences. Call it first |
| `x80_cpm_run` | 1 | **ships** — the batch verb. No `exit_code` field, deliberately |
| `x80_dos_run` | 1 | **ships** — and this one *does* carry an exit code |
| `x80_diff_run` | 1 | **ships** — guest vs host reference, byte for byte, under a declared normalization |
| `x80_files` | 1 | **ships** for `list` / `to_guest` / `from_guest` on a kept sandbox. `handles` and `resolve` return a structured `unsupported` |
| `x80_probe` | 1 | **ships** — which OS a package actually needs, from its syscalls, plus a literal next call |
| `x80_images` | 1 | **ships** — the pinned catalog. The only tool that touches the network |
| `x80_open` `x80_session` `x80_run` `x80_send` `x80_recv` `x80_screen` `x80_regs` `x80_step` `x80_breakpoints` `x80_disasm` `x80_monitor` `x80_trace` | 2 | not registered — need the pty adapter and the VT emulator |
| `x80_consoles` | 3 | not registered — needs the mpm2 SSH adapter |
| `x80_mem_read` `x80_mem_write` | 4 | not registered — need an upstream change (SPEC.md 6.9) |
| `x80_syscall_break` | 5 | not registered |

| profile | tier | backend | state |
|---|---|---|---|
| `cpm-hosted` | hosted | cpmemu | **runnable** |
| `dos-hosted` | hosted | dosiz | **runnable** |
| `cpm22` `cpm3` `zsdos` `zsystem` `nzcom` | hardware | romwbw_emu | described; blocked on the phase-2 adapter |
| `z80-bare` | hardware | romwbw_emu | blocked on phase 2 *and* on the absent `--start=ADDR` (SPEC.md 3.6) |
| `mpm2` | hardware | mpm2_emu | blocked on the phase-3 adapter |
| `freedos` `x86-bare` | hardware | emu88d | blocked on the phase-5 adapter; `freedos` has never booted headless |

Measured end to end on `cpm-hosted` with a 23-member 1986 ARC and the real
`cpmemu` 4.8.0: `pass:true`, `exit_reason:"jmp_0"`, 667 ms, 23 files,
`B5-TIME.INF` at exactly 1664 bytes, and `x80_diff_run` reporting 23 of 23
byte-identical against the Python reference under
`normalize:["lowercase_names","pad_to_record"]`. The whole exercise is
runnable: [`examples/agent/80un/`](../examples/agent/80un/).
