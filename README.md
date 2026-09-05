# 80mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server over the
Z80 and x86 emulator family in the sibling repos — so an agent can run a CP/M, MP/M or
DOS package and assert on what came back, and debug the emulators themselves without a
human doing it by mouse click and screen read.

**Phase 1 ships. Phases 2-5 are still proposal.** The full design is in
[SPEC.md](SPEC.md); this file is the short version. If you are an agent about
to make a call, read [DRIVING-WITH-AI.md](DRIVING-WITH-AI.md) instead — it is
the operating manual, and its Gotchas chapter is measured rather than
remembered.

## Install and run

```
pip install -e .
80mcp doctor        # every backend and profile, exiting nonzero on any problem
80mcp               # speak MCP JSON-RPC on stdio
```

Python 3.11+, **no third-party runtime dependencies**. `pytest` and
`jsonschema` are dev-only: `pip install -e ".[dev]"`.

Register it as an stdio server:

```json
{"mcpServers": {"80mcp": {"command": "80mcp", "args": []}}}
```

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

## What ships, and what does not

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
runnable: [`examples/agent/80un/`](examples/agent/80un/).

## Documentation

| file | for |
|---|---|
| [DRIVING-WITH-AI.md](DRIVING-WITH-AI.md) | **an agent about to make a call.** The seven tools, the profile decision tree, the two tiers, and twelve measured gotchas |
| [examples/agent/80un/](examples/agent/80un/) | a runnable end-to-end exercise: unpack an archive, prove it byte-correct, then break it four ways on purpose |
| [tests/README.md](tests/README.md) | how to run the suite and which tests need which backend binary |
| [SPEC.md](SPEC.md) | the full design: all 23 tools, the MBP boundary, and the measured-facts index in Appendix A |
| [evidence/](evidence/) | the raw transcripts every number in the spec traces back to |

## Read this first: most of it already exists

The honest answer to "is there an MCP server for this?" is *mostly yes*, and the spec
opens by saying so:

- **[deltecent/altairsim](https://github.com/deltecent/altairsim)** has an MCP server
  compiled into the simulator — **31 tools** over stdio, on the same `Machine` object as
  its monitor. It boots real CP/M 2.2 and CP/M 3 from a fresh clone with 36 disk images
  tracked in git, and ships a 606-line `docs/DRIVING-WITH-AI.md` plus a worked
  "debug this broken HELLO.ASM" exercise. It was built and driven live while writing this
  spec: `ctest` 61/61, `altair_tests` 214,254 checks / 0 failed, and CP/M 2.2 reached the
  `A>` prompt in 0.04 s over MCP. It has zero stars, which means nobody has found it — not
  that it is immature. **If you want an agent that writes, assembles and single-steps a
  CP/M program on period 8080/Z80 hardware, use altairsim.** This spec recommends
  registering it alongside 80mcp rather than competing with it, and borrows its
  `run{from, input, until, timeout_ms, max_steps}` expect-loop verbatim.
- **DOS is covered six ways over.** [Spice86](https://github.com/OpenRakis/Spice86)
  (65+ tools, DOS-aware down to `read_dos_psp` and the MCB chain) plus six independent
  DOSBox-X MCP servers that appeared between April and September 2026.
- **Generic Z80 is saturated** — Gearsystem and Gearcoleco at ~80 tools each,
  mcp-openmsx, and roughly eight ZX Spectrum servers.

## What is actually missing

Three things, confirmed empty across GitHub, the official registry, Glama, PulseMCP,
mcp.so and LobeHub:

1. **A batch/package verb.** "Here is a `.COM`, here are some host files, run it, hand
   me back the files it created, byte-exact." altairsim declined host-filesystem
   passthrough in writing (`DESIGN.md` §12.2); its nearest substitute measured 7 tool
   calls with no deadline, no exit reason and no artifact manifest.
2. **MP/M.** Zero MCP servers anywhere — also none for CP/M-86, MP/M-86 or Concurrent
   CP/M. No existing server has a concept that maps onto *"which of my four terminals is
   this output for."*
3. **The BDOS / XDOS / INT 21h syscall layer.** Every CP/M-adjacent server drives a
   console with an expect loop on `A>`. Nothing answers "which host file did that FCB
   resolve to."

Plus two that are missing because they are yours: **RomWBW HBIOS**, which five shipping
products are built on, and **x86** (altairsim's `intel8259a.h:47` says outright that it
has no 8086 core).

## The mode matrix

The proposal was `z80: bare / cp/m / mp/m` and `x86: bare / freedos`. That is right and
it misses a tier you already own — the one `dosiz`'s own README names:

> `dosiz : FreeDOS-on-emu88 :: cpmemu : RomWBW-on-disk`

One tier translates the OS **API** to the host filesystem (instant, no disk image,
file-oriented — exactly what "just packages to be run" wants). The other runs the **real
OS on emulated hardware**. So the grid is 3×2:

| | hosted (OS-API translation) | hardware (real OS on an emulated machine) |
|---|---|---|
| **z80 bare** | — | `z80-bare` — romwbw_emu ROM monitor |
| **z80 CP/M** | `cpm-hosted` — cpmemu | `cpm22` `cpm3` `zsdos` `zsystem` `nzcom` — romwbw_emu |
| **z80 MP/M** | — | `mpm2` — mpm2_emu |
| **x86 bare** | — | `x86-bare` — emu88, needs a runner |
| **x86 DOS** | `dos-hosted` — dosiz | `freedos` — emu88 + `freedos_hd.img` |

Demonstrated headless while writing this: CP/M 2.2, CP/M 3 (banked, `60K TPA`), ZSDOS,
Z-System, NZCOM, **MP/M II V2.1** (four consoles configured, seven 48K banks), and DOS
programs under `dosiz` (42/42 djgpp fixtures, exit codes propagating). Never
demonstrated: `freedos` booting headless, and `z80-bare`. Both are marked as such in the
spec rather than assumed.

That paragraph is about the **emulators**, driven by hand while the spec was written —
not about this server. Through 80mcp today, only the two hosted cells are reachable:
`cpm-hosted` and `dos-hosted`. Every hardware cell is described by `x80_profiles` and
reports `ready:false` with the phase that will unblock it.

`SPEC.md` §3.2 carries the full matrix with a per-profile *validated by* column, because
"it boots" and "a package runs correctly on it" are different claims.

## The sibling repos

| repo | role here |
|---|---|
| [romwbw_emu](https://github.com/avwohl/romwbw_emu) | the Z80 + RomWBW HBIOS core, and the primary interactive backend. Already headless, pipe-driven, with a `sim>` debugger |
| [cpmemu](https://github.com/avwohl/cpmemu) | BDOS-level CP/M 2.2 translator — the `cpm-hosted` batch backend. Also supplies `qkz80`, the CPU core the rest of the family compiles |
| [mpm2](https://github.com/avwohl/mpm2) | real MP/M II V2.1 with a banked XIOS, 60 Hz preemption and up to 8 SSH consoles — the `mpm2` backend, and the unclaimed ground |
| [dosiz](https://github.com/avwohl/dosiz) | DOS-API translator (INT 21h/31h/67h → host C++) — the `dos-hosted` batch backend |
| [qxDOS](https://github.com/avwohl/qxDOS) | owns `emu88`, the from-scratch 8088/286/386 whole PC — the `x86-bare` and `freedos` backend |
| [romwbw_disks](https://github.com/avwohl/romwbw_disks) | the sha256-pinned ROM and disk-image catalog every profile resolves through |
| [80un](https://github.com/avwohl/80un) | not an emulator — the reference fixture. Ships the same unpacker as a CP/M `.COM` and as Python, so it validates both the batch verb and the differential verb |
| [z80cpmw](https://github.com/avwohl/z80cpmw) · [ioscpm](https://github.com/avwohl/ioscpm) · [cpmdroid](https://github.com/avwohl/cpmdroid) | GUI clients. **Explicitly not automation targets** — see below |

## Two conclusions worth knowing before reading the spec

**Drive a headless core; do not drive the GUI.** All three GUI clients are thin shells
over one C++ core that already has two working headless front ends. The blockers are hard
rather than soft: `ioscpm`'s own `MANUAL_CHECKS.md` §3 records that synthetic key events
*provably do not reach the app* in the Simulator; `adb shell input text` drops characters
at speed; a `WM_COMMAND` + `PrintWindow` driver crashed `z80cpmw` twice inside comctl32.
The one GUI asset worth keeping is `z80cpmw`'s `TerminalView::cellAt()` as a *dialect
oracle*, never as a driving target.

**MP/M means `mpm2`, and never `romwbw_emu`.** All four MP/M prerequisites are absent or
the wrong shape in `romwbw_emu` — 32K/`0x8000` bank geometry where MP/M needs
48K/`0xC000`, no RTC tick (the only interrupt facility is a *random* fuzzing injector),
no XIOS at all, and `SYSGET_CIOCNT` hard-returns one console behind a 478-line contract
four shipping ports keep. Teaching it MP/M is months of work that would destabilise the
emulator five products compile in place.

## Also found while writing this

`romwbw_emu/disks/disks.xml:34` still describes CP/M Plus as *"NOT WORKING, under
investigation."* It boots banked and runs `DIR`. The corrected text exists downstream in
`ioscpm/release_assets/disks.xml:39` and never propagated back — and that stale line is
where a tool-builder or an agent would look to decide whether to expose CP/M 3 at all.

## License

GPL-3.0-or-later, matching the rest of the family.
