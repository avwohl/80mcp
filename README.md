# 80mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server over the
Z80 and x86 emulator family in the sibling repos — so an agent can run a CP/M, MP/M or
DOS package and assert on what came back, and debug the emulators themselves without a
human doing it by mouse click and screen read.

**Phase 1 ships. Phases 2-5 are still proposal.** The full design is in
[SPEC.md](https://github.com/avwohl/80mcp/blob/main/SPEC.md); this file is the short version. If you are an agent about
to make a call, read [DRIVING-WITH-AI.md](https://github.com/avwohl/80mcp/blob/main/DRIVING-WITH-AI.md) instead — it is
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
bug report. The doctor output, backend discovery and protocol notes are in
[docs/status.md](https://github.com/avwohl/80mcp/blob/main/docs/status.md).

## What ships

Seven batch tools over two backends: `x80_profiles`, `x80_cpm_run`, `x80_dos_run`,
`x80_diff_run`, `x80_files`, `x80_probe` and `x80_images`. The other sixteen tools
are not registered. Two profiles run today: `cpm-hosted` (cpmemu) and `dos-hosted`
(dosiz). Every hardware profile reports `ready:false` with the phase that will
unblock the profile. The full tool and profile tables are in
[docs/status.md](https://github.com/avwohl/80mcp/blob/main/docs/status.md).

For an agent that writes, assembles and single-steps a CP/M program on period
8080/Z80 hardware, use [deltecent/altairsim](https://github.com/deltecent/altairsim)
alongside 80mcp. [docs/background.md](https://github.com/avwohl/80mcp/blob/main/docs/background.md) explains why.

## Documentation

| file | for |
|---|---|
| [DRIVING-WITH-AI.md](https://github.com/avwohl/80mcp/blob/main/DRIVING-WITH-AI.md) | **an agent about to make a call.** The seven tools, the profile decision tree, the two tiers, and twelve measured gotchas |
| [examples/agent/80un/](https://github.com/avwohl/80mcp/blob/main/examples/agent/80un/) | a runnable end-to-end exercise: unpack an archive, prove it byte-correct, then break it four ways on purpose |
| [tests/README.md](https://github.com/avwohl/80mcp/blob/main/tests/README.md) | how to run the suite and which tests need which backend binary |
| [SPEC.md](https://github.com/avwohl/80mcp/blob/main/SPEC.md) | the full design: all 23 tools, the MBP boundary, and the measured-facts index in Appendix A |
| [evidence/](https://github.com/avwohl/80mcp/blob/main/evidence/) | the raw transcripts every number in the spec traces back to |
| [docs/status.md](https://github.com/avwohl/80mcp/blob/main/docs/status.md) | `80mcp doctor` output, backend discovery, protocol revision, and the full tool and profile tables |
| [docs/background.md](https://github.com/avwohl/80mcp/blob/main/docs/background.md) | prior art (altairsim, Spice86, DOSBox-X), what is missing, the mode matrix, and two design conclusions |

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
| [z80cpmw](https://github.com/avwohl/z80cpmw) · [ioscpm](https://github.com/avwohl/ioscpm) · [cpmdroid](https://github.com/avwohl/cpmdroid) | GUI clients. **Explicitly not automation targets** — see [docs/background.md](https://github.com/avwohl/80mcp/blob/main/docs/background.md) |

## License

GPL-3.0-or-later, matching the rest of the family.
