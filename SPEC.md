# 80mcp — an MCP server for the avwohl Z80 and x86 emulator family

**Status:** proposal. Nothing in this document has been built.
**Target MCP revision:** 2026-07-28.
**Proposed repo:** `avwohl/80mcp` (new).

Every claim in this document is labelled. **Demonstrated** means a command was executed
on a real machine and its output is quoted here. **Measured** means a number came off a real run. **Never run**
means exactly that. Where a design decision rests on reading source rather than running
it, the file and line are given so the next person can check.

---

## 1. Does this already exist, and should you build it?

**Short answer: three of the five things you asked for do not exist anywhere, and the
other two are owned by projects better than anything you would ship. Build the three.**

### 1.1 What already exists and is better than what you would write

**`deltecent/altairsim` owns "an agent that writes, assembles, runs and single-steps a
CP/M program on period 8080/Z80 hardware."** Demonstrated: built from a fresh clone with
`./build.sh` (exit 0, no dependencies), `ctest` returned *100% tests passed, 0 tests
failed out of 61*, and `./build/altair_tests` returned *214254 checks, 0 failed* across
103 suites. Its MCP server was driven live over stdio: **31 tools** (not the 19 its own
`DRIVING-WITH-AI.md` claims — that is doc drift, `DESIGN.md` §11 is right), booting three
different CP/M flavours out of a 32 MB self-contained clone with 36 disk images tracked
in git:

| machine | call | result | wall |
|---|---|---|---|
| CP/M 2.2, 88-2SIO | `run{from:0xFF00,until:"A>"}` | `56K CP/M 2.2b v2.3 / For Altair 8" Floppy / A>` | 0.04 s |
| CP/M 3 non-banked, dualide | `run{from:0xF000}` then `input:"P"` | `64K CP/M VERSION (Non banked) / A>` | — |
| CP/M 3 banked, SD Systems | CR then `"C\r"` | `SD Systems CP/M Plus Ver 3.0 / Banked Rel. 1.5 / A>` | 2.2 s |

Its `run{from, input, until, timeout_ms, max_steps}` → `{output, pc, steps, stopped}` is
the best expect-loop primitive in the survey. **Adopt that argument shape verbatim** (§6.2).
It also has `snapshot`/`restore`, which nothing in your family has.

**Spice86 (656★, 65+ tools including `read_dos_psp`, `read_dos_mcb_chain`, EMS/XMS, VGA,
breakpoints) plus six independent DOSBox-X MCP servers own general DOS.** Do not write a
general DOS MCP server. Generic Z80 is likewise not a gap: Gearsystem/Gearcoleco at ~80
tools each, mcp-openmsx, roughly eight ZX Spectrum servers.

### 1.2 What does not exist anywhere

All three were confirmed empty this session across GitHub, the official registry, Glama,
PulseMCP, mcp.so and LobeHub.

1. **A batch/package verb.** "Here is a `.COM`, here are some host files, run it and hand
   me back the files it created, byte-exact." altairsim structurally cannot do this: it
   declined host-filesystem passthrough in writing (`DESIGN.md` §12.2 — *"no `DISK` verbs
   in the monitor and no `disk_*` tools over MCP"*; `docs/roadmap.md` strikes the
   pull-a-.COM-out-of-an-image transcript as **"Cut."**). Its nearest substitute was
   measured: host `IN.TXT` → `R IN.TXT` → `PIP OUT.TXT=IN.TXT` → `W OUT.TXT` → host, in
   **7 tool calls / 0.39 s**, with no deadline, no exit_reason, no assertions and no
   artifact manifest — a hand-written expect script per package.
2. **MP/M.** Zero MCP servers. Same for CP/M-86, MP/M-86, Concurrent CP/M. No existing
   server anywhere has a concept that maps onto *"which of my four terminals is this
   output for."* altairsim has zero MP/M hits, and it is structurally blocked:
   `src/mcp/server.cpp:607` `console()` finds *the* one serial unit with `state=="console"`
   and rebinds it; `run`/`send`/`recv` have no line argument. Adding a second 2SIO line
   works (`board_add` + `board_set` + `connect` all succeeded) and then **no tool can read
   or write it**.
3. **The BDOS / XDOS / INT-21h syscall layer.** Every CP/M-adjacent server drives the
   console with an expect loop on `A>` and reaches memory through a monitor. Nothing
   answers "which host file did that FCB resolve to." In altairsim the closest reachable
   thing is a breakpoint at 0x0005 plus `regs`, at **2 MCP round trips per BDOS call** — 20
   calls cost 42 tool calls — and no FCB→host-file mapping is *possible* there, because a
   mounted image has no host-side filesystem view at all (§12.2 again).

Two more things nobody else has, because they are yours: **RomWBW HBIOS** (altairsim's
only RomWBW hits are two lines in a third-party ROM listing, `roms/MASTER0/MASTER0.LST:119`;
it has no RomWBW ROM, no HBIOS, no test, and its bankmem card models Vector Graphic /
Cromemco / North Star, not 512K ROM + 512K RAM), and **x86** (`src/chips/intel8259a.h:47`
says outright: *"this simulator has no 8086 core"*).

### 1.3 Should you contribute upstream to altairsim instead?

**No, and the licence direction is the hard stop.** `DESIGN.md` §1 scopes altairsim as *"a
hardware development bench"* for the Altair/S-100 — the point is developing new hardware,
not running CP/M. §0.1 forbids sourcing hardware facts from other emulators, which is
exactly what an HBIOS shim would be. §14 makes a period-manual-sourced board document a
merge gate. And **altairsim is MIT while every one of your repos with a LICENSE is
GPL-3.0** — `romwbw_emu`, `cpmemu`, `ioscpm`, `cpmdroid`, `z80cpmw`, `romwbw_disks`,
`dosiz`, `qxDOS`, `80un`, all verified. Upstreaming your code is a relicensing act before
it is an engineering one. A RomWBW *board* would also be weeks in someone else's tree at
hardware speed, against `romwbw_emu`'s HBIOS shim that reaches the CP/M 2.2 prompt in
under 0.1 s — and would still leave batch, host-fs, MP/M, x86, syscall tracing and
multi-machine unsolved.

**One cheap upstream contribution is worth making: three issues, no code, no licence
entanglement.** (a) the 19-vs-31 tool-count doc drift; (b) `monitor{command:"!…"}` — it
executes an arbitrary host shell as the launching user (`!echo HOSTSHELL_REACHED_$(id -u); pwd`
returned `HOSTSHELL_REACHED_501` and the cwd) **and the subprocess inherits stdout, so its
output lands raw on the JSON-RPC channel**; a strict client throws and desyncs permanently
(my driver did); (c) the CP/M 3 idle-heuristic gap (§1.4).

### 1.4 Three measured altairsim mistakes not to repeat

- **Make the idle signal actually fire.** altairsim's `stopped:"idle"` fires instantly on
  the 88-2SIO CP/M 2.2 machine and **never** on the propio/dualide CP/M 3 machine:
  `run{input:"P",timeout_ms:20000}` burned the full 20.00 s and returned
  `stopped:"timeout"`; even `run{input:"\r"}` sitting at the `A>` prompt burned 20.00 s.
  The same call with `until:"A>"` returned in 0.00 s. Every one of your backends has a
  *real* idle signal (`HBIOSDispatch::isConsoleIdle()`, `EmulatorEngine::isIdle()`,
  `dos_machine::is_waiting_for_key()`); use it, and never ship a heuristic in its place.
- **Never let a subprocess write to the JSON-RPC channel.** altairsim's own code knows
  this — its `--mirror` bind error is deliberately sent *"to STDERR, never `out`, which is
  the JSON-RPC channel a stray line would corrupt"* — and the `!` escape violates it.
- **Do not ship a 2024-11-05 handshake.** altairsim's `initialize` returns
  `protocolVersion "2024-11-05"` (hardcoded, `src/mcp/server.cpp:1568`), handles only five
  methods, and carries no annotations, no `outputSchema`, no `ttlMs`. It predates every
  convention in §4.

### 1.5 Verdict

Build `80mcp`, scoped to exactly this:

> **80mcp is a supervisor for the avwohl emulator family. It exists to run *your*
> packages, drive *your* multi-console MP/M, and debug *your* emulators. It is not a
> general CP/M server and not a general DOS server.**

Ship a one-line pointer to altairsim in the README for generic CP/M-on-period-hardware
work, and recommend registering it alongside. **Phase 1 alone — seven tools, two
backends, zero emulator changes — is worth building even if the answer to everything else
is "use altairsim."**

---

## 2. Non-goals, stated once, up front

- **Not a general DOS MCP server.** Spice86 and six DOSBox-X servers own that ground. Our
  x86 side exists to debug `emu88`/`dosiz` and run our own DOS packages.
- **Not a general CP/M-on-period-hardware server.** altairsim owns that.
- **Not a GUI driver.** `ioscpm`, `cpmdroid` and `z80cpmw` are thin shells over a core
  that already has two headless front ends. The blockers are hard, not soft: `ioscpm`'s
  `MANUAL_CHECKS.md` §3 records that synthetic key events *provably do not reach the app*
  in the Simulator; `adb shell input text` fires each character as a separate command and
  the guest drops some; a `WM_COMMAND` + `PrintWindow` driver session against `z80cpmw`
  crashed the app twice inside comctl32. The one thing worth keeping is `z80cpmw`'s
  `TerminalView::cellAt()` as a *dialect oracle* (§9, phase 6), never as a driving target.
- **No snapshot / restore / fork / rewind.** Weeks in each of four repos, and for
  `romwbw_emu` the state layout would become a downstream contract four ports must honour.
  Deterministic input replay is the 80% substitute at ~200 lines and zero emulator change
  (§6.10). If someone needs true fork/rewind on an 8080/Z80 CP/M machine today, altairsim
  has it.
- **No Roots, Sampling or Logging.** Deprecated in 2026-07-28. Paths are tool parameters
  and resource URIs, we call no model, every diagnostic goes to stderr.
- **No elicitation, therefore no MRTR re-entrancy.** §4.5.
- **No router mode.** Gearsystem needs one at ~80 tools. At 23 we do not. Revisit past ~45.
- **No second transport in v1.** stdio only. The *watching* use case that loopback HTTP is
  usually reached for is served by `x80_open{mirror:true}` (a TCP mirror of the console),
  which is a server feature, not a protocol.
- **No audio tools.** `dosiz`'s `DOSIZ_AUDIO_DUMP` renders 0.25 s of PCM from the sound
  devices' *final register state*, not from the run. It is a smoke test, not a recording.
- **No EGA/VGA planar screenshots.** Modes 0Dh/0Eh/10h/12h and CGA 4/5/6 are not
  composited in `emu88` — `emit_video_frame` branches VESA → Hercules → mode 13h → *else
  treat it as text*. Return an explicit `unsupported_video_mode` error rather than a
  confidently wrong PNG. Mode 13h, Mode X, Hercules and VESA are in scope.
- **No mouse, no key-hold.** `emu88::queue_key` is press-only — it queues make and break
  together, and `sendScancodeRelease:` is an explicit no-op. Fine for a shell prompt,
  wrong for a game reading INT 09h key-down state.
- **Never teach `romwbw_emu` MP/M.** §3.3.

---

## 3. The mode matrix

### 3.1 The grid is 3×2, not 2 + 2

Your framing was `z80: bare / cp/m / mp/m` and `x86: bare / freedos`. That is right as far
as it goes and it misses a tier you already own and ship. The structural analogy is exact
and `dosiz`'s own README states it:

> **`dosiz : FreeDOS-on-emu88 :: cpmemu : RomWBW-on-disk`**

One tier translates the OS **API** straight to the host filesystem — instant, no disk
image, file-oriented, and *exactly* what "packages like 80un.git… just packages to be run"
wants. The other runs the **real OS on emulated hardware** — needed for anything touching
a BIOS, a screen, or a boot. So the grid is:

| | hosted (OS-API translation) | hardware (real OS on emulated machine) |
|---|---|---|
| **z80 bare** | — | `z80-bare` (romwbw_emu, ROM monitor) |
| **z80 CP/M** | `cpm-hosted` (cpmemu) | `cpm22` `cpm3` `zsdos` `zsystem` `nzcom` (romwbw_emu) |
| **z80 MP/M** | — | `mpm2` (mpm2_emu) |
| **x86 bare** | — | `x86-bare` (emu88d, new runner) |
| **x86 DOS** | `dos-hosted` (dosiz) | `freedos` (emu88d) |

### 3.2 Profiles, backends, and demonstrated status

| profile | family | tier | backend | OS | status | evidence | validated by |
|---|---|---|---|---|---|---|---|
| `cpm-hosted` | z80 | hosted | `cpmemu` | CP/M 2.2 BDOS surface | **demonstrated** | 23-member ARC unpacked 23/23 in 0.63 s wall / 99% CPU; `23 file(s) extracted` | **`80un`** — `80un.com` vs the Python `un80`, 23/23 byte-identical after `pad_to_record` |
| `cpm22` | z80 | hardware | `romwbw_emu --boot=2` | CP/M 2.2 | **demonstrated** | `CP/M-80 v2.2, 54.0K TPA`; full 96-file `DIR`; boot to prompt under 0.1 s (`real 0.12` at `sleep 0.1`) | **`80un`** (`R8`-staged), plus `DIR` against `hd1k_combo.img` slice 0 |
| `cpm3` | z80 | hardware | `romwbw_emu --boot=2.3` | CP/M 3 **banked** | **demonstrated** | `CP/M v3.0 [BANKED] for HBIOS v3.5.1`, `60K TPA`; **pager caveat, §3.5** | `DIR *.COM` on slice 3 — **truncates at the pager**, §3.5 |
| `zsdos` | z80 | hardware | `romwbw_emu --boot=Z` | ZSDOS | demonstrated in the research pass; **not re-run in verification** | — | boot banner only |
| `zsystem` | z80 | hardware | `romwbw_emu --boot=2.1` | Z-System | as above | — | boot banner only |
| `nzcom` | z80 | hardware | `romwbw_emu --boot=2.2` | NZCOM / ZCPR3 | as above | — | boot banner + ZCPR3 named-directory `DIR` |
| `mpm2` | z80 | hardware, **multi-console** | `mpm2_emu` | MP/M II V2.1 | **demonstrated, with two hard prerequisites** | §3.4 | **`mpm2`'s own CI** boots MP/M and drives `DIR`/`STAT` over SSH; 2 concurrent consoles here, **4 never run** |
| `dos-hosted` | x86 | hosted | `dosiz` | DOS 6.22 API on emu88's 386 | **demonstrated** | 42/42 djgpp fixtures in 2.729 s; `DJ_PRINTF.exe` → rc **7**; sandboxed `SORT.EXE < IN.TXT > OUT.TXT` in **0.022 s** | `tests/djgpp/run.sh` 42/42 + 25/25 DPMI fixtures; real GNU tools and Open Watcom binaries |
| `z80-bare` | z80 | hardware | `romwbw_emu --boot=none` | none | **needs-work, never run** | §3.6 | nothing |
| `x86-bare` | x86 | hardware | **`emu88d` (new)** | none | needs the ~600-line runner | a ~60-line prototype booted, read 0xB8000 and injected keys in the research pass | a throwaway ~60-line driver, not committed anywhere |
| `freedos` | x86 | hardware | `emu88d` + `freedos_hd.img` | FreeDOS 1.4 / MS-DOS 4.0 | **`available:false` until proven** | §3.7 | **nothing — `boot()`'s only caller is the iOS app** |

`ZPM3` and `QPM` are deliberately absent: claimed by `romwbw_emu`, never demonstrated.
They cost a profile string each when someone boots one.

**Governing rule, to be written into `CONTRIBUTING.md`:**

> **A new operating system adds a profile string, not a tool.**

Ten profiles across two CPU families and seven operating systems sit behind one `profile`
argument. That single sentence is what holds the surface at 23 tools instead of drifting
toward Gearsystem's 80 and a router mode.

### 3.3 Correction: MP/M is `mpm2`, and never `romwbw_emu`

Every MP/M string in the six repos you named is a pointer to `avwohl/mpm2` or an
aspiration; none is an implementation. Teaching `romwbw_emu` MP/M is months and would
destabilise the emulator five products compile in place. All four hard requirements are
absent or the wrong shape:

- **Bank geometry is wrong.** `romwbw_mem.h:38-40`: `BANK_SIZE = 32*1024`,
  `BANK_BOUNDARY = 0x8000`. MP/M needs 48K banked below `0xC000` with 16K common above
  (`mpm2/include/banked_mem.h:14-24`). RomWBW's whole HBIOS lives in the high common today.
- **No RTC tick.** The only interrupt facility is a *random* fuzzing injector
  (`romwbw_emu.cc:131-143`, mt19937 seeded from `random_device`); the ROM handles RST 38H
  as `ei / reti`; `SYSGET_TIMER` (0xD0) is declared at `hbios_dispatch.h:240` with no case
  in `handleSYS`.
- **No XIOS.** `grep -i xios` over the whole repo returns nothing. mpm2's equivalent is 738
  lines of `asm/bnkxios.asm` plus 752 of `src/xios.cpp`.
- **One console, baked into a downstream contract.** `hbios_dispatch.cc:1673`:
  `SYSGET_CIOCNT` returns 1. `handleCIO()` reads the unit at `:801` and never uses it.
  Making CIO unit-aware is a breaking change across four ports that keep the 478-line
  `emu_io.h` contract managed by a 39 KB `DOWNSTREAM.md`.

### 3.4 MP/M: what is true, and three corrections

**Demonstrated.** `mpm2_emu` boots real MP/M II V2.1 headless. Literal command:

```
DYLD_LIBRARY_PATH=/Users/wohl/src/cpmemu/src mpm2_emu -l -t 20 \
  -w 127.0.0.1:18099 -p 127.0.0.1:18223 \
  -d A:.../mpm2/disks/mpm2_system.img < /dev/null
```

RC 0, and the real DRI loader output: `MP/M II V2.1 Loader / Copyright (C) 1981, Digital
Research / Nmb of consoles = 4 / Breakpoint RST # = 6 / Memory Segment Table: …`, then
`MP/M II Sys 9100H 6F00H Bank 0` plus seven `Memseg Usr 0000H C000H Bank 1..7` (7 × 48K),
then `MP/M II V2.1 / Copyright (C) 1982, Digital Research` three times. Boot completes
under 5 s wall; at `-t 9` the emulator reported `Z80 executed 1237927 instructions`,
because it paces to a 60 Hz real-time tick rather than running flat out.

**Correction 1 — `mpm2_emu` is not a self-contained binary.** `otool -L` shows the hard
install-name `/usr/local/lib/libqkz80.4.dylib`, which is not installed. Without
`DYLD_LIBRARY_PATH=/Users/wohl/src/cpmemu/src` it is **RC 134, dyld failure**. Any spec
listing `mpm2` as ready must carry that as an explicit prerequisite; the real fix is an
rpath or a static link (§8, item 14).

**Correction 2 — the `-l` local console has no console attribution and must never be used
as a transport.** Demonstrated: in every run, four consoles are configured and only
**three** prompts ever appear on stdout — `grep -o -E '^[0-9]A>' | sort | uniq -c` gives
exactly one each of `1A>`, `2A>`, `3A>` and **zero `0A>`**, at `-t 6`, `-t 12` and `-t 20`
alike. Worse, a `DIR` typed at the `1A>` prompt answered `Directory for User 3:` and left
the cursor at `3A>`. Reading `src/main.cpp:258-266` explains it: local mode sets
`set_local_mode(true)` on **all 8** consoles so their output interleaves on one
undifferentiated stdout. Four consoles' banners and one console's session, one stream.

**Correction 3 — you cannot request a console number; the guest tells you its own.**
`ConsoleManager::find_free()` (`src/console.cpp:72-80`) hands out the **highest** free
console first, with the comment *"MP/M II creates TMP on console (MAXCONSOLE - 1) …
Assign from highest active console down."* So a server that picks console 0 and expects to
get it is wrong. But the guest announces itself in-band on connect —
`src/ssh_session_libssh.cpp:298`:

```c
snprintf(banner, sizeof(banner), "\r\nMP/M II Console %d\r\n\r\n", console_id_);
```

**So `console` addressing needs no patch, no `ConsoleQueue` front end and no protocol
invention.** The server opens N SSH clients at `x80_open` and *labels* each by the banner
it receives. Two concurrent consoles were demonstrated in the design pass:

```
MP/M II Console 3            MP/M II Console 2
MP/M II V2.1                 MP/M II V2.1
3A>DIR                       2A>USERS
00:00:09 A:DIR     .PRL      00:00:10
Directory for User  3:       USERS?
A: $3$      SUP              2A>
3A>
```

**Four** concurrent SSH consoles have never been run. That is the phase-3 acceptance gate.

**Two launch gotchas the code must encode.** No host key ships in the checkout (`Failed to
set host key` → `Failed to listen`), so the server mints a throwaway RSA key per session
with `ssh-keygen`. And **port 0 is rejected on both `-w` and `-p`** despite `--help`
saying `0 to disable` — `parse_listen_address` returns `Invalid HTTP listen address: 0`.
The server must bind an ephemeral port itself, close it, and pass the real number.

### 3.5 New: CP/M 3 paginates and then blocks

Demonstrated. `t2/stdout.txt` ends with `Press RETURN to Continue ` after 21 lines of
directory. CP/M 2.2's `DIR` on the same disk ran to completion unpaged. **Any capture
verb needs pager handling or it silently truncates on CP/M 3 and then hangs.** `x80_run`
therefore takes `pager` (`"auto"` | `"stop"` | `"off"`, default `"auto"`) and reports
`pager_answered:int` (§6.5).

### 3.6 Correction: there is no `--start=ADDR`, so `z80-bare` does not ship today

`romwbw_emu --help` on v1.38 lists `--strict-io`, `--trace=FILE`, `--symbols=FILE`,
`--escape=`, `--boot=`, `--boot=none`, `--disk0/1=`, `--config`/`--no-config`/`--save-config`.
**There is no `--start`.** A bare machine is reachable as `--boot=none` (come up at the ROM
menu) plus `sim> pc ADDR` over the pty, which is *needs-work*, not ships-today, and has
never been run. Do not advertise `z80-bare` as ready.

### 3.7 `freedos` stays `available:false` until one job proves it

No automated test anywhere in `qxDOS` boots any DOS; `boot()`'s only caller is
`Emu88Emulator.mm:823`. The gate is one CI job that runs
`scripts/build_starter_disk.sh` and boots the result headless under `emu88d`. Until that
job is green, `x80_profiles` reports `freedos` with `ready:false` and
`blocked_by:["freedos boot has never been demonstrated headless"]`. Also note: **emu88 has
no DOS and therefore no ERRORLEVEL**, so a `freedos` batch run needs a result-file
convention (a guest batch file writing a result file, `dos_io::lpt_output`, or
`serial_tx`), not an exit code.

### 3.8 Also correct one line of catalog, because an agent will read it

`romwbw_emu/disks/disks.xml:34` still says CP/M Plus is *"NOT WORKING, under
investigation."* It boots, banked, and runs `DIR`. `ioscpm/release_assets/disks.xml:39`
was corrected in v1.4.1 and the fix never propagated back. That stale line is the one
place a tool-builder or an agent would look to decide whether to expose CP/M 3.

---

## 4. Session and handle model (MCP 2026-07-28)

### 4.1 What the revision took away

- `initialize` / `notifications/initialized` handshake **removed**. Every request carries
  its protocol version and client capabilities in `_meta`. New required RPC
  **`server/discover`**.
- Protocol-level sessions and `Mcp-Session-Id` **removed**. *"Servers that need cross-call
  state use explicit, server-minted handles passed as ordinary tool arguments."*
- `tools/list` / `resources/list` / `prompts/list` **MUST NOT vary per-connection**.
- `ttlMs` + `cacheScope` now **required** on the list methods and on `resources/read`.
- `resultType` required on every result. Legal values: **`"complete"`** and
  **`"input_required"`** (`schema.ts:216`). `"tool_result"` is not a value.
- `cacheScope` legal values: **`"public"`** and **`"private"`** (`schema.ts:1109`).
  `"server"` is not a value.
- Annotation defaults that bite: `readOnlyHint` false, **`destructiveHint` true**,
  `idempotentHint` false, **`openWorldHint` true**. An emulator is a closed world; a
  server that omits annotations ships every tool as open-world and destructive.
- `resources/subscribe` → one long-lived `subscriptions/listen`;
  `notifications/resources/updated` carries a URI only.
- Roots, Sampling and Logging deprecated. `ping` and `logging/setLevel` removed.
- Resource-not-found error code `-32002` → `-32602`.
- `structuredContent` may be any JSON value; `inputSchema`/`outputSchema` accept any
  JSON Schema 2020-12 keywords, including `$ref`, `if`/`then` and `$defs`.

### 4.2 Handles

`x80_open` mints an opaque handle: `m_` + 128 bits of urandom, base32. Never a pid, a path
or an index. The spec's non-normative **Stateful Tools** section prescribes four rules;
each is satisfied explicitly:

| rule | implementation |
|---|---|
| validate caller against handle | handles are per server process; a handle from another process is unknown, not merely unauthorised |
| opaque ids | `m_` + 128 random bits, base32 |
| state retention policy in the creating tool's description | `x80_open`'s description carries §4.3 verbatim |
| expired handle → tool execution error, not JSON-RPC error | `isError:true`, `resultType:"complete"`, structured body `{"error":"handle_expired","expired_at":…,"sandbox_path":…,"hint":"the sandbox is preserved for 24h; call x80_open again"}` |

### 4.3 Retention policy (quoted verbatim in `x80_open`'s description)

> Machines live in this MCP server process. A machine is reaped after 10 minutes idle or
> 8 hours absolute, whichever is first, and **dies when this server process exits** — MCP
> clients may restart a stdio server at will, so do not assume a handle survives a client
> restart. Its **sandbox directory outlives it**: on disk for 24 hours, named by the
> handle, so `x80_files{sandbox:…}` can still fetch what the guest produced even after the
> handle is gone. A crashed machine keeps its handle for the life of this process together
> with the exit signal and the last 64 KB of stderr; a crash is the most valuable moment
> in a debugging session and is never silently reaped.

### 4.4 The daemon: deliberately deferred to v2, behind a boundary that costs nothing now

The strongest architectural argument in the design round was for a supervised daemon
(`80mcpd` on a unix socket, machines outliving the stdio server, visible from any client).
It is rejected for v1 and the reasoning is a measurement:

**Boots are cheap on every profile except one.** CP/M 2.2 reaches the prompt in under
0.1 s; CP/M 3 in 0.06 s; `cpm-hosted` and `dos-hosted` have no boot at all (0.022 s for a
whole sandboxed `SORT.EXE` run). So losing a machine mostly costs a re-boot and a replayed
setup script — which is exactly what `x80_session{op:"reset", replay_to:N}` already does.
The one profile where it hurts is `mpm2`: ~5 s and 1.2 M instructions to four TMPs.

**The ruling.** All machine state lives behind one interface, `MachineStore`, and nothing
above that interface knows where the machines are. v1's implementation is in-process. v2's
is a client of `$XDG_RUNTIME_DIR/80mcp/<proto-version>.sock` (macOS:
`~/Library/Caches/80mcp/`) with the protocol version *in the path*, auto-spawn on absence,
and exit after 10 minutes with zero machines. **Nothing above the socket changes**, which
is what makes the deferral free.

**The named trigger to build v2:** the first time someone loses a live `mpm2` session to a
client restart and says so. Not before. A daemon is net-new long-lived surface — stale
sockets, auto-spawn races, version skew, orphan reaping — in a family whose stated problem
is more surface than hands.

### 4.5 MRTR: solved by not eliciting

A tool returning `resultType:"input_required"` **terminates**; the client re-issues the
whole call with `inputResponses` plus the echoed opaque `requestState`. Any tool that can
elicit must therefore be re-entrant and must not double-apply — which for us would mean
`x80_run` typing `DIR\r` twice.

**v1 elicits nowhere.** Every destructive thing is an explicit required argument in the
schema (`overwrite:true`, `allow_fetch:true`) and we return `isError` instead of asking.
That deletes the entire hazard class rather than mitigating it. The one network tool,
`x80_images`, never runs implicitly — no other tool may trigger a download.

**The dedupe cache ships anyway, because clients retry on transport errors.** Every
mutating tool takes an optional `idempotency_key`. **It is a client-supplied nonce, never a
hash of the arguments** — an argument hash cannot distinguish a retry from a deliberate
repeat, and would silently swallow the second of two identical `x80_send{text:"DIR\r"}`
calls, which is a completely normal agent loop. The server keeps a 10-minute
`(handle, tool, idempotency_key) → result` cache, dedupes only when a key is explicitly
supplied, and `x80_send` records bytes under an `op_id` **before** queueing them, so a
retry resumes rather than restarts.

### 4.6 Resources

`resources/list` must not vary per connection either, so machines cannot appear as
resources. **Resource templates** are the spec-clean shape:

| template | `ttlMs` | `cacheScope` |
|---|---|---|
| `80mcp://machine/{handle}/transcript` | 0 | `private` |
| `80mcp://machine/{handle}/screen` | 0 | `private` |
| `80mcp://machine/{handle}/files/{path}` | 0 | `private` |
| `80mcp://catalog/images` | 3600000 | `public` |

`tools/list` itself: `ttlMs: 86400000`, `cacheScope: "public"` — identical for every user
of the same install, changing only when `backends.toml` changes, and the server restarts
on that.

**The transcript is a resource, not a tool result.** A long debugging session's transcript
would eat the context window if it came back in every `x80_run`. `x80_run` returns only
the delta since the last call plus a `stopped` reason. `subscriptions/listen` plus
`notifications/resources/updated` gives live tailing.

### 4.7 Tool-list versioning

The list is frozen within a server build and identical on every connection, as the spec
requires. **It grows by version, and a tool enters the list on the release where at least
one backend actually serves it.** A tool that is listed and always errors is a lie in
`tools/list`. Absence from every connection of a given build is legal — the rule is about
connections, not about server versions.

That is not the same as `caps`. `x80_step` ships in v1.1 because `romwbw_emu` serves it;
calling it against `cpmemu` returns a **structured** error naming an alternative:

```json
{"error":"unsupported","op":"step","backend":"cpmemu",
 "reason":"cpmemu has no debugger; CPMEmulator is declared inside a 3429-line cpmemu.cc with no header, so nothing can reach it from outside the process",
 "alternative_profile":"cpm22",
 "escalation":{"tool":"x80_open","arguments":{"profile":"cpm22"}}}
```

An agent can act on that. A bare `"not supported"` cannot be acted on.

---

## 5. Architecture

### 5.1 Where the code lives: a new repo, `avwohl/80mcp`

Forced by the coupling facts. Inside `romwbw_emu` it becomes a fifth port under the
`emu_io.h` contract and half the server (x86) has no home there. Inside `cpmemu` it widens
the blast radius of the one repo three siblings compile `qkz80` from with no version gate.
Inside `qxDOS` it risks the fixed six-file `dosiz` link list. Inside `dosiz` it is x86-only
and `emu88` changes are forbidden by that repo's own `CLAUDE.md`.

**Two hard rules for `CONTRIBUTING.md`:**

> **1. 80mcp never contains a copy of any emulator source.** It consumes sibling checkouts
> by path (as `dosiz` does) or installed binaries, and pins both in `backends.toml`.
>
> **2. 80mcp never compiles emulator source into itself.** It execs already-built
> binaries. The single exception is `backends/emu88d/`, which compiles `../qxDOS/emu88/*.cc`
> **by path**, with every new line of code in *our* translation units — because there is no
> emu88 runner to exec.

Rule 2 is what makes "zero emulator changes for phases 1-3" true rather than aspirational.

### 5.2 Backends are subprocesses

In-process linking is where maintainability dies here, and the evidence is unambiguous:
`romwbw_emu` has no library, no header install, no pkg-config, and two functions
(`emu_host_path_caps`, `emu_host_file_get_read_name`) deliberately left undefined so a new
front end *fails to link on purpose*; `cpmemu`'s CP/M personality is inside a 3429-line
`cpmemu.cc` with no header; `dosiz` has one public function (`bridge.h:24`), file-scope
statics everywhere, and `_exit()`s to dodge destructor order; and `emu88` is lovely and
white-box but `dosiz` compiles a **fixed six-file list** from it, so a seventh `.cc` builds
in `qxDOS` and fails to LINK in `dosiz`.

Subprocess is not a compromise. The backends were built for it: clean guest-stdout /
diagnostics-stderr separation in all three, pty-friendly, exit codes where they exist.

### 5.3 MBP — the machine backend protocol

One line protocol. This is the single most important boundary in the design, because it is
what lets an upstream improvement swap an adapter without touching a tool schema.

- **Transport:** newline-delimited JSON on **fd 3** (control), guest bytes on a **pty**
  (fd 0/1), diagnostics on **fd 2**. Three streams, never mixed.
- **Wire:** `{"id":N,"op":"…","args":{…}}` → `{"id":N,"ok":true,"result":{…}}`.
- **Required ops, every backend:** `caps`, `boot`, `run`, `send`, `recv`, `idle`, `stop`.
- **Optional ops, advertised by `caps`:** `regs`, `mem_read`, `mem_write`, `step`,
  `bp_set`, `bp_clear`, `disasm`, `screen_text`, `screen_pixels`, `console_list`,
  `console_select`, `trace_on`, `trace_off`, `trace_read`, `syscall_bp`.

Three adapter shapes, and every backend is one of them:

| shape | backends | how ops are served | upstream change |
|---|---|---|---|
| **1. Native MBP** | `emu88d` (new); later `romwbwd` | the binary speaks MBP on fd 3 | we wrote it |
| **2. Pty adapter** (in-server) | `romwbw_emu`, `mpm2_emu` | spawn the stock binary under a pty + stderr pipe; `regs`/`step`/`bp_*` map to `sim>` commands after the escape byte; MP/M consoles are N SSH clients; `screen_text` from our VT emulator | **none** |
| **3. One-shot adapter** | `cpmemu`, `dosiz` | `boot` is a no-op; `run` = exec in the sandbox with this stdin, capture streams, wait for exit or deadline; no `regs`, no `step` | **none** |

When `romwbw_emu` grows the `--control=PATH` fifo (§8, item 6), it moves from shape 2 to
shape 1 and nothing above the adapter changes. That is the property you are paying for.

**MBP conformance suite:** the same op set run against every backend on every release, so
drift is visible the day it happens rather than in a bug report six months later.

### 5.4 The four launch invariants

Every design in the round got at least one of these wrong. State them once, at the top, and
enforce them in one place.

**Invariant 1 — hermetic launch is four things, none optional.**

```
HOME=<session>/home  XDG_CONFIG_HOME=<session>/xdg \
  romwbw_emu --no-config --boot=<explicit target> …
```

`XDG_CONFIG_HOME` + `--no-config` alone is **not sufficient**, and this was demonstrated:
with a sandbox `XDG_CONFIG_HOME` and `--no-config`, stderr still said

```
Loaded NVRAM setting 'C' from /Users/wohl/.config/romwbw_emu/nvram (migrates to the new path on exit)
```

The cause is a deliberate migration fallback at `romwbw_emu.cc:1379-1390` — when the XDG
path holds no setting, `load_nvram_setting(legacy_nvram_path)` runs, and
`get_legacy_nvram_path()` (`:79-82`) is built from `$HOME` unconditionally. **Writes are
isolated** (no nvram file appeared in any sandbox; stderr confirmed the sandbox path *"is
unchanged"*); **reads are not**. Passing an explicit `--boot=` does override the leaked
value — `--boot=2` and `--boot=2.3` both won over the persisted `'C'` — so all four
together are correct and any three of them are not. Without this, the same MCP call boots
a different operating system depending on what the developer last chose interactively.

**Invariant 2 — pace before every send, or you corrupt the run.** Demonstrated: the only
change between a working and a broken run was removing a 1.5 s wait.

```
{ printf 'DIR\r'; sleep 2; printf '\x03'; } | romwbw_emu … --boot=2 --no-config
```

Output ends `CP/M-80 v2.2, 54.0K TPA / A>^C / A>`. The `DIR` never reached CP/M — it was
consumed by the romldr `AutoBoot in 0 Seconds (<esc> aborts, <enter> now)` prompt. **RC
still 0.** With the wait, the identical command returned the full 96-file directory. The
same hazard bites in the other direction: one stray byte arriving during a `DIR`
truncates the listing (CP/M's abort-on-keypress). Every send waits for the backend's real
idle signal or a prompt match first — never a fixed sleep in shipped code.

**Invariant 3 — binary mode by default on `cpm-hosted`, as a correctness requirement.**
`cpmemu`'s `default_mode = auto` never resolves on *write*. It does not merely corrupt
bytes: demonstrated, the same 23-member ARC under `default_mode = auto` gave

```
80UN - CP/M Archive Unpacker v2.3
Extracting:
  -03MAR86 OK
  B5-TIME.INF
  Error

1 file(s) extracted
```

**1 of 23** extracted, the one written file truncated (1437 bytes against 1664), the
other 21 absent — and **process exit code 0**, with stderr still
saying `Program exit via JMP 0`. There is no CLI flag; only a config file can set this. The
batch verb synthesizes the `.cfg` and defaults to `binary`. This is also the sharpest
available proof of Invariant 4.

**Invariant 4 — no bare exit code on any CP/M path.** CP/M has no exit-status concept;
`cpmemu` `exit(0)`s on every normal path including its runaway watchdog; `romwbw_emu`
always returns 0; all three of `80un`'s failure modes exit 0; and per Invariant 3 a
96%-failed run is indistinguishable from success at the process level. **`x80_cpm_run` has
no `exit_code` field at all** — not a nullable one, not one guarded by `if`/`then`. Success
is asserted from stdout plus the file manifest. `dosiz` is the exception and gets its own
verb, `x80_dos_run`, which does carry `exit_code` (demonstrated: `DJ_PRINTF.exe` → rc 7).

### 5.5 Resource hygiene, deadlines and cost

- **Nothing in the family has a timeout.** `dosiz`'s run loop has no instruction counter
  and no wall clock (a 2-byte `EB FE` spin ran until SIGKILL); `cpmemu`'s only guard is a
  hardcoded 9e9-instruction watchdog that does not fire on a guest blocked on an idle open
  pipe; `romwbw_emu`'s is 10e9. **Every launch is externally deadlined and killed by
  process group.** `mpm2_emu -t SECS` is the family's only internal timeout and is passed
  as belt-and-braces, never relied on.
- **A boot deadline is a different control from a lifetime TTL, and both are needed.**
  Every machine-opening tool takes `boot_timeout_ms` (default 30000, max 300000) and
  returns `stopped:"boot_timeout"` with whatever `boot_output` arrived, rather than
  hanging the tool call.
- **`SIGSTOP` idle machines.** A polled CP/M guest on an open idle pipe burns ~95% of a
  core (BDOS 6). Three idle sessions would cost three cores. `stopped:"idle"` SIGSTOPs the
  child process group; the next call SIGCONTs it. Measured contrast: a `romwbw_emu` session
  blocked on console input costs **0.03 s CPU over 4 s wall** — it blocks, it does not spin
  — so the SIGSTOP matters for `cpmemu` and `dosiz`, not for `romwbw_emu`.
- **Disk images are copy-on-write cloned into the session directory and the catalog copy
  is never mutated.** On APFS `clonefile()` makes the 51,380,224-byte combo image
  microseconds. Catalog-sourced disks default `write_protect:true`; a raw `host_path`
  defaults writable and is cloned regardless.
- **`printer` and `aux_output` go to files in the sandbox, not to `/dev/null` and not to
  empty.** Empty values produce `Warning: Cannot open printer file '': No such file or
  directory` on every single run. `/dev/null` silences that but **silently destroys the
  LST: and PUN: byte streams** — a CP/M utility that writes its report to the list device
  then returns an empty stdout and looks like a clean no-op. Route both to
  `<sandbox>/.lst` and `<sandbox>/.pun`, and surface them in the result as
  `list_output`/`punch_output` with byte counts, the same way `files_out` handles disk
  writes.
- **`dosiz` runs `chdir`'d into the sandbox with a relative program name.** Mandatory, not
  stylistic. Demonstrated: `./build/dosiz /abs/path/tests/DJ_PRINTF.exe` → **rc 102**,
  `C:\PRIVATE\TMP\…\DJ_PRINTF.EXE: can't open` — `argv[0]` is built as `"C:"` + the host
  path, uppercased with backslashes, and DJGPP's go32 stub reopens it to load its COFF
  payload.
- **Two `dosiz` stderr lines print on every single run** and are filtered by name, with the
  filtering recorded rather than silently swallowed:
  `dosiz: ethernet/slirp backend unavailable; INT 0x60 packet driver will accept guest
  calls but RX/TX will no-op.` and `dosiz: Crynwr pktdrv installed at INT 60h, stub at
  0060:0000`.
- **ROM and disk are pinned as a pair.** The C++ HBIOS emulates RomWBW v3.5.1 *exactly*;
  a mismatched slice prints `*** WARNING: HBIOS/CBIOS Version Mismatch ***` into a
  transcript an agent will misread. `x80_open` takes both `rom` and `disks`, refuses a
  mismatched pair by default, and takes `allow_version_mismatch:true` for the deliberate
  skew experiment. `80mcp doctor` refuses to start a mismatched install and exits nonzero.

### 5.6 The screen, and why it is a fidelity liability

There is no character-cell buffer anywhere in the Z80 half of the family, by design —
`romwbw_emu`'s `DECISIONS.md` §3 rules that *"the VT emulation is the host terminal's
job."* So the server runs its own VT emulator, and that makes us the family's **fourth**
divergent parser. The three that exist diverge *deliberately* (SGR 0 resetting to green vs
CGA-7, `ESC[104m`, SGR 1 brightening, SGR 39/49, the 0x7F–0xFF range), documented across a
113 KB `FEATURE_PARITY.md`.

Mitigations, all of which are in the schema rather than in prose:

- One core parser plus a **dialect table** of divergence points. `dialect` is reported on
  every `x80_screen` result and is **required** on any colour or attribute assertion.
- `source` says where the cells came from: `"guest_ram"` (emu88, 0xB8000 — ground truth) or
  `"vt:<dialect>"` (everything else — approximate).
- `lossy[]` is a machine-readable list, not a prose caveat:
  `high_bit_masked` (romwbw_emu masks every output byte to 0x7F **before you see it**, so
  line-drawing and 8-bit national characters are destroyed at the source),
  `cr_dropped` (over a pipe romwbw_emu emits bare LF; a pty's ONLCR restores it),
  `adm3a_translated` (cpmemu's ADM-3A-to-ANSI translator drops TAB and every unlisted
  control byte), `bios_banner_row0` (emu88 injects `iosFreeDOS <version>` before the first
  printable TTY character, so row 0 of a fresh boot is not the guest's).
- Validated against `z80cpmw`'s existing 516-check `TerminalView::cellAt()` corpus — the
  only VT model in the family that has been pixel-verified.

**A screen assertion an agent cannot audit for lossiness is worse than no screen.**

---

## 6. Tool surface

**23 tools.** Names are `x80_`-prefixed. **Argument shapes** converge with altairsim
name-for-name where they overlap (`run{from,input,until,timeout_ms,max_steps}` →
`{output,pc,steps,stopped}`, the `stopped` enum, `send{text}`, `recv{}`); **tool names do
not**, because both designs recommend registering altairsim alongside and the MCP spec
puts collision handling on the client:

> *"Tool name uniqueness is scoped to a single server. Clients or proxies that aggregate
> tools from multiple servers MAY encounter naming collisions … and SHOULD implement a
> disambiguation strategy such as prefixing tool names with a server identifier."*

Adopting bare `run`/`mount`/`step` would *maximise* the ambiguity in exactly the
muscle-memory case convergence is meant to serve. The prefix costs nothing; the argument
shapes are where the muscle memory actually lives.

**Conformance test, run in CI, fails the build:** (a) every `inputSchema` and
`outputSchema` in the repo `json.loads` and validates against the JSON Schema 2020-12
metaschema; (b) stripping `x80_` leaves a superset of altairsim's interactive verb set
(`run`, `send`, `recv`, `regs`, `step`, `monitor`, `breakpoints`, `disasm`, `mem_*`) and
its `stopped` enum; (c) no `resultType` other than `"complete"`/`"input_required"` and no
`cacheScope` other than `"public"`/`"private"` appears anywhere in the source or the docs.
Convergence that is not tested is convergence that drifts.

### 6.1 The list, with phase and status

| # | tool | phase | status |
|---|---|---|---|
| 1 | `x80_profiles` | 1 | ships-today |
| 2 | `x80_cpm_run` | 1 | ships-today |
| 3 | `x80_dos_run` | 1 | ships-today |
| 4 | `x80_diff_run` | 1 | ships-today |
| 5 | `x80_files` | 1 | ships-today (sandbox routes); `image` route phase 2; `hostfile` route phase 4 |
| 6 | `x80_probe` | 1 | ships-today |
| 7 | `x80_images` | 1 | ships-today |
| 8 | `x80_open` | 2 | needs-small-work (pty adapter) |
| 9 | `x80_session` | 2 | needs-small-work; `op:"reset"` with `replay_to` is ~200 lines |
| 10 | `x80_run` | 2 | needs-small-work |
| 11 | `x80_send` | 2 | needs-small-work |
| 12 | `x80_recv` | 2 | needs-small-work |
| 13 | `x80_screen` | 2 | needs-small-work (VT emulator) |
| 14 | `x80_regs` | 2 | needs-small-work (sim> reply parsing) |
| 15 | `x80_step` | 2 | needs-small-work |
| 16 | `x80_breakpoints` | 2 | needs-small-work |
| 17 | `x80_disasm` | 2 | needs-small-work on Z80 (`ud80`); x86 needs Zydis, phase 5 |
| 18 | `x80_monitor` | 2 | ships-today |
| 19 | `x80_trace` | 2 | ships-today as a stderr parser; the `resolved` block is phase 5 |
| 20 | `x80_consoles` | 3 | needs-small-work |
| 21 | `x80_mem_read` | 4 | **needs-upstream — see §6.9. Does NOT ship today.** |
| 22 | `x80_mem_write` | 4 | **needs-upstream — see §6.9.** |
| 23 | `x80_syscall_break` | 5 | needs-medium-work |

**Deliberately not in the list, ever, until they work:** `snapshot`, `restore`. Nothing in
the family has a save-state and listing them would be a lie.

### 6.2 Annotations

Every tool is `openWorldHint:false` except `x80_images`. The defaults bite hard
(`destructiveHint` true, `openWorldHint` true), so this table is not optional decoration.

| tool | readOnly | destructive | idempotent | openWorld |
|---|---|---|---|---|
| `x80_profiles` | true | false | true | false |
| `x80_images` | false | false | true | **true** |
| `x80_cpm_run` | false | false | false | false |
| `x80_dos_run` | false | false | false | false |
| `x80_diff_run` | false | false | false | false |
| `x80_probe` | false | false | false | false |
| `x80_files` | false | **true** | false | false |
| `x80_open` | false | false | false | false |
| `x80_session` | false | **true** | false | false |
| `x80_run` | false | **true** | false | false |
| `x80_send` | false | **true** | false | false |
| `x80_recv` | false | false | false | false |
| `x80_screen` | true | false | true | false |
| `x80_consoles` | true | false | true | false |
| `x80_regs` | true | false | true | false |
| `x80_step` | false | **true** | false | false |
| `x80_breakpoints` | false | false | false | false |
| `x80_mem_read` | true | false | true | false |
| `x80_mem_write` | false | **true** | false | false |
| `x80_disasm` | true | false | true | false |
| `x80_monitor` | false | **true** | false | false |
| `x80_trace` | false | false | false | false |
| `x80_syscall_break` | false | false | false | false |

### 6.3 Shared schema definitions

The 2026-07-28 revision accepts any JSON Schema 2020-12 keywords, including `$defs` and
`$ref` resolution, so there is no longer any reason to inline-duplicate or hand-wave a
shared shape. Every `inputSchema` that uses one of these carries it in its own `$defs`.

```json
{
  "FileIn": {
    "type": "object", "additionalProperties": false,
    "properties": {
      "guest_name": {"type": "string", "description": "Name as the guest will see it. CP/M profiles: uppercase 8.3."},
      "host_path": {"type": "string"},
      "content_b64": {"type": "string"},
      "mode": {"enum": ["binary", "text"], "default": "binary"}
    },
    "required": ["guest_name"],
    "oneOf": [{"required": ["host_path"]}, {"required": ["content_b64"]}]
  },
  "Collect": {
    "type": "object", "additionalProperties": false,
    "properties": {
      "return_content": {"enum": ["never", "inline_if_under_kb", "always"], "default": "inline_if_under_kb"},
      "max_kb": {"type": "integer", "default": 64, "maximum": 4096},
      "exclude": {"type": "array", "items": {"type": "string"}, "default": []}
    }
  },
  "Normalize": {
    "type": "array", "uniqueItems": true,
    "items": {"enum": ["lowercase_names", "pad_to_record", "strip_cpm_eof", "crlf_to_lf"]},
    "description": "NOT a boolean. lowercase_names: cpmemu lowercases on create. pad_to_record: pad the shorter side to the next 128-byte boundary with 0x1A - REQUIRED for LBR and ARC members, because the guest writes whole records and a host reference writes the exact size."
  },
  "Fidelity": {
    "type": "object",
    "properties": {
      "tier": {"enum": ["hosted", "hardware"]},
      "divergences": {"type": "array", "items": {"type": "string"}}
    }
  }
}
```

### 6.4 Phase 1 — the batch tools

#### `x80_profiles` — ships-today

> List the machine profiles this installation can actually run: backend, tier,
> capabilities, required disk images and whether they are present. **Call this first.**
> Capabilities differ enormously between backends, and a profile with a missing image or a
> ROM/disk version mismatch will fail `x80_open`.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":false,"properties":{"family":{"enum":["z80","x86","all"],"default":"all"},"profile":{"type":"string","description":"Report on one profile only, with full detail."},"only_ready":{"type":"boolean","default":false,"description":"Omit profiles whose backend binary or disk images are missing."},"probe":{"type":"boolean","default":true,"description":"Stat the backend binaries and images, run the ROM/disk version check, and check dynamic-library resolution. Set false for a cheap static answer."}}}
```

**Out:** `{profiles:[{id, family, tier, backend, backend_version, binary_path, os, caps:[string], consoles:int, images:{required:[{id,sha256,present:bool}]}, fidelity:{tier,divergences:[string]}, ready:bool, blocked_by:[string]}], server_version, protocol_version, external_servers_recommended:[{for,name,url,why}]}`

`fidelity.divergences` is carried inline per profile, not buried in docs. `cpm-hosted`
carries, at minimum: *"setup_command_line() never writes the FCB at 0x5C, so a program
testing fcb(1)=' ' for its usage banner takes the wrong branch"* and *"created filenames
are lowercased on the host: TEST.TXT becomes test.txt."* `blocked_by` for `mpm2` on a
stock macOS install reads *"mpm2_emu links /usr/local/lib/libqkz80.4.dylib which is not
installed; set DYLD_LIBRARY_PATH or install libqkz80."* `external_servers_recommended`
points at altairsim for generic CP/M-on-S-100 and at Spice86 for general DOS.

`80mcp doctor` is the same data as a CLI table, exits nonzero on any problem, and is the
first thing every support conversation for the next two years should start with.

#### `x80_cpm_run` — ships-today. **THE BATCH VERB.**

> Run one CP/M package to completion in a fresh sandbox and return its console output, a
> manifest of the files it created, a termination reason, and your assertions.
> **This tool deliberately has no `exit_code` field**, because no CP/M backend has one:
> CP/M has no exit-status concept, cpmemu exit(0)s on every path including its runaway
> watchdog, romwbw_emu always returns 0, and a run that extracted 1 of 23 files and printed
> "Error" still exited 0 (measured). Success must be asserted from stdout plus the file
> manifest.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["profile","program"],"additionalProperties":false,"$defs":{"FileIn":{"type":"object","additionalProperties":false,"properties":{"guest_name":{"type":"string"},"host_path":{"type":"string"},"content_b64":{"type":"string"},"mode":{"enum":["binary","text"],"default":"binary"}},"required":["guest_name"],"oneOf":[{"required":["host_path"]},{"required":["content_b64"]}]}},"properties":{"profile":{"enum":["cpm-hosted","cpm22","cpm3","zsdos","zsystem","nzcom","mpm2"],"description":"cpm-hosted (cpmemu) is instant and file-oriented. The others boot a real OS on emulated hardware and need the files staged into a disk image."},"cpu":{"enum":["8080","z80"],"default":"z80"},"program":{"type":"string","description":"Host path to the .COM for hosted profiles; a host path (staged onto the session image) or a bare guest-resident name for hardware profiles."},"args":{"type":"array","items":{"type":"string"},"default":[],"description":"The CP/M command tail."},"files_in":{"type":"array","default":[],"items":{"$ref":"#/$defs/FileIn"}},"stdin":{"type":"string","description":"Bytes fed to the guest console."},"default_mode":{"enum":["binary","text","auto"],"default":"binary","description":"MUST default to binary. cpmemu's auto mode never resolves on WRITE: the same 23-member ARC extracted 1 of 23, printed 'Error', truncated the one file it wrote, and exited 0 (measured). Only a config file can set this; the server synthesizes one."},"eol_convert":{"type":"boolean","default":false},"boot_timeout_ms":{"type":"integer","default":30000,"maximum":300000,"description":"Hardware profiles only: give up waiting for the OS prompt after this long and return stopped:'boot_timeout' with whatever boot output arrived."},"timeout_ms":{"type":"integer","default":10000,"maximum":600000,"description":"Wall-clock deadline for the guest run. Enforced by the server and killed by process group; no backend in this family has an internal wall clock."},"collect":{"type":"object","additionalProperties":false,"properties":{"return_content":{"enum":["never","inline_if_under_kb","always"],"default":"inline_if_under_kb"},"max_kb":{"type":"integer","default":64,"maximum":4096},"exclude":{"type":"array","items":{"type":"string"},"default":[]},"normalize":{"type":"array","uniqueItems":true,"items":{"enum":["lowercase_names","pad_to_record","strip_cpm_eof","crlf_to_lf"]},"description":"Applied to the REPORTED sha256 and content only, never to the file on disk."}}},"assert":{"type":"object","additionalProperties":false,"properties":{"stdout_contains":{"type":"array","items":{"type":"string"}},"stdout_not_contains":{"type":"array","items":{"type":"string"}},"files_created":{"type":"array","items":{"type":"string"}},"file_sha256":{"type":"object","additionalProperties":{"type":"string"}},"no_unimplemented_bdos":{"type":"boolean","default":false}}},"keep_sandbox":{"type":"boolean","default":false}}}
```

**Out:**
`{pass:bool, exit_reason:"jmp_0"|"bdos_0"|"wboot"|"instruction_limit"|"eof_giveup"|"ctrl_c"|"timeout"|"boot_timeout"|"emulator_error", wall_ms, stdout, stderr, files_out:[{guest_name,host_name,bytes,sha256,content_b64?}], list_output:{bytes,sha256,content_b64?}, punch_output:{bytes,sha256,content_b64?}, assertions:[{kind,value,ok,actual?}], diagnostics:{unimplemented_bdos:[int], config_warnings:[string]}, warnings:[string], fidelity:{tier,divergences:[string]}, sandbox:string|null}`

`exit_reason` is parsed from the backend's stable stderr sentinel lines: `Program exit via
JMP 0`, `System reset`, `BIOS WBOOT called - exiting`, `Reached instruction limit`,
`[Exiting: 1024 console reads past end of input]`. `diagnostics.unimplemented_bdos` comes
from cpmemu's `Unimplemented BDOS function N` line and is the cheapest possible signal for
*"this package needs a richer CP/M than this one — rerun under `cpm3`."* `host_name` is
reported separately from `guest_name` because cpmemu lowercases on create, so a caller
writing case-sensitive assertions against host names will be wrong.

#### `x80_dos_run` — ships-today

> Run one DOS package to completion in a fresh sandbox. Unlike `x80_cpm_run` this **does**
> have a meaningful exit code — dosiz propagates the DOS AH=4Ch AL value (measured: rc 7)
> — but rc 1 is ambiguous between "the guest exited 1" and "dosiz failed to load the
> program", so `exit_code_meaning` disambiguates it from stderr.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["profile","program"],"additionalProperties":false,"$defs":{"FileIn":{"type":"object","additionalProperties":false,"properties":{"guest_name":{"type":"string"},"host_path":{"type":"string"},"content_b64":{"type":"string"},"mode":{"enum":["binary","text"],"default":"binary"}},"required":["guest_name"],"oneOf":[{"required":["host_path"]},{"required":["content_b64"]}]}},"properties":{"profile":{"enum":["dos-hosted","freedos"],"description":"dos-hosted (dosiz) traps INT 21h/31h/67h to the host filesystem: instant, no image, real exit codes. freedos boots a real FreeDOS kernel on emu88, needs an image, and has NO exit code - emu88 has no DOS and therefore no ERRORLEVEL, so it needs a result-file convention."},"program":{"type":"string","description":"Host path to the .EXE/.COM. The server chdirs into the sandbox and passes a RELATIVE name: an absolute path gives 'C:\\PATH\\PROG.EXE: can't open', rc 102, because argv[0] is built as 'C:' + the uppercased backslashed host path and DJGPP's go32 stub reopens it (measured)."},"args":{"type":"array","items":{"type":"string"},"default":[]},"files_in":{"type":"array","default":[],"items":{"$ref":"#/$defs/FileIn"}},"stdin":{"type":"string"},"env":{"type":"object","additionalProperties":{"type":"string"}},"boot_timeout_ms":{"type":"integer","default":30000,"maximum":300000,"description":"freedos only."},"timeout_ms":{"type":"integer","default":20000,"maximum":600000},"expect_exit_code":{"type":"integer"},"collect":{"type":"object","additionalProperties":false,"properties":{"return_content":{"enum":["never","inline_if_under_kb","always"],"default":"inline_if_under_kb"},"max_kb":{"type":"integer","default":64,"maximum":4096}}},"assert":{"type":"object","additionalProperties":false,"properties":{"stdout_contains":{"type":"array","items":{"type":"string"}},"stdout_not_contains":{"type":"array","items":{"type":"string"}},"files_created":{"type":"array","items":{"type":"string"}},"file_sha256":{"type":"object","additionalProperties":{"type":"string"}},"no_unimplemented_int21":{"type":"boolean","default":false}}},"keep_sandbox":{"type":"boolean","default":false}}}
```

**Out:**
`{pass:bool, exit_code:int|null, exit_code_meaning:"guest_exit"|"loader_failure"|"pm_fault"|"timeout"|"none_available", wall_ms, stdout, stderr, files_out:[…], assertions:[…], diagnostics:{unimplemented_int21:[{ah,ax,bx,cx,dx}], pm_fault?:{cs,eip,err,regs}, stderr_filtered:[string]}, fidelity:{…}}`

`exit_code` is `null` and `exit_code_meaning` is `"none_available"` on the `freedos`
profile. `dosiz` prints `dosiz: unimplemented INT 21h AH=XXh (…) -- returning
invalid-function, program continues` **once per distinct AH** and keeps going, so one run
yields the whole set — the DOS-side twin of `unimplemented_bdos`.

#### `x80_diff_run` — ships-today

> Run the same inputs through a guest program and a host reference implementation and
> compare the outputs byte for byte under a **declared** normalization. Built for the case
> 80un proves: the same algorithm shipped as a CP/M .COM and as Python.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["guest","reference","inputs"],"additionalProperties":false,"$defs":{"FileIn":{"type":"object","additionalProperties":false,"properties":{"guest_name":{"type":"string"},"host_path":{"type":"string"},"content_b64":{"type":"string"},"mode":{"enum":["binary","text"],"default":"binary"}},"required":["guest_name"],"oneOf":[{"required":["host_path"]},{"required":["content_b64"]}]},"GuestRun":{"type":"object","additionalProperties":false,"required":["profile","program"],"properties":{"profile":{"type":"string"},"program":{"type":"string"},"args":{"type":"array","items":{"type":"string"}},"cpu":{"enum":["8080","z80"]},"default_mode":{"enum":["binary","text","auto"],"default":"binary"},"eol_convert":{"type":"boolean","default":false},"stdin":{"type":"string"}}}},"properties":{"guest":{"$ref":"#/$defs/GuestRun"},"reference":{"type":"object","required":["argv"],"additionalProperties":false,"properties":{"argv":{"type":"array","items":{"type":"string"},"description":"Host command. {input} and {outdir} are substituted."},"env":{"type":"object","additionalProperties":{"type":"string"}},"cwd":{"type":"string"}}},"inputs":{"type":"array","minItems":1,"items":{"$ref":"#/$defs/FileIn"}},"normalize":{"type":"array","uniqueItems":true,"items":{"enum":["lowercase_names","pad_to_record","strip_cpm_eof","crlf_to_lf"]},"description":"An ENUM, never a boolean. Measured on a 23-member ARC: without normalization, diff -rq reports 46 'Only in' lines and 0 matches. With ['lowercase_names','pad_to_record'], 23 of 23 match."},"compare":{"enum":["bytes","sha256"],"default":"bytes"},"timeout_ms":{"type":"integer","default":60000,"maximum":600000,"description":"Covers the guest run and the reference command together. Both are killed by process group."}}}
```

**Out:**
`{pass:bool, normalization_applied:[string], files_compared:int, identical:int, differ:int, only_in_guest:[string], only_in_reference:[string], diffs:[{name,guest_bytes,reference_bytes,first_diff_offset,note}]}`

#### `x80_files` — ships-today (sandbox routes)

> Move files between the host and a guest, list what the guest can see, show what it has
> open right now, and answer "where *would* `A:FOO.TXT` come from?" without running
> anything. The direction is in the op name, not in a flag, because the single most common
> agent failure in this domain is editing a host copy of a file instead of the one the
> guest can see.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["op"],"additionalProperties":false,"$defs":{"FileIn":{"type":"object","additionalProperties":false,"properties":{"guest_name":{"type":"string"},"host_path":{"type":"string"},"content_b64":{"type":"string"},"mode":{"enum":["binary","text"],"default":"binary"}},"required":["guest_name"],"oneOf":[{"required":["host_path"]},{"required":["content_b64"]}]}},"properties":{"op":{"enum":["list","to_guest","from_guest","handles","resolve"],"description":"to_guest = host -> guest. from_guest = guest -> host. handles = what the guest has open right now. resolve = dry-run name resolution, runs nothing."},"handle":{"type":"string","description":"A live machine from x80_open. Mutually exclusive with `sandbox`."},"sandbox":{"type":"string","description":"A sandbox path returned by a batch verb with keep_sandbox:true, or by a dead machine's handle_expired error. Mutually exclusive with `handle`."},"files":{"type":"array","items":{"$ref":"#/$defs/FileIn"},"description":"op:'to_guest' only."},"names":{"type":"array","items":{"type":"string"},"description":"op:'from_guest'/'list': guest names or globs. Omit to take everything created since the machine started."},"drive":{"type":"string","pattern":"^[A-Z]$","description":"CP/M profiles accept A-P; DOS profiles accept C-Z. The legal set per profile is in x80_profiles; an out-of-range letter returns isError naming the profile's actual drive set."},"via":{"enum":["auto","sandbox","hostfile","image"],"default":"auto","description":"sandbox: cpmemu/dosiz host-directory passthrough, instant. hostfile: HBIOS 0xE1-0xEA via R8/W8 (romwbw) or INT E0h via R.COM/W.COM (emu88) - byte-granular, no image surgery. image: cpm_disk.py with the pinned diskdef, requires the machine stopped or the disk flushed."},"since":{"enum":["start","last_call","never"],"default":"never","description":"op:'list': filter to files created or modified since that point. Answers the question no emulator in the family reports today."},"hash":{"type":"boolean","default":false},"overwrite":{"type":"boolean","default":false,"description":"op:'to_guest': required true to replace an existing guest file. There is no elicitation; this returns isError instead of asking."},"return_content":{"enum":["never","inline_if_under_kb","always"],"default":"inline_if_under_kb"},"max_kb":{"type":"integer","default":64,"maximum":4096},"export_to":{"type":"string","description":"op:'from_guest': host directory to write into."},"resolve_name":{"type":"string","description":"op:'resolve': the guest name to resolve, e.g. A:FOO.TXT."},"resolve_for_write":{"type":"boolean","default":false,"description":"op:'resolve': resolution differs by direction. On cpm-hosted a *.EXT config mapping routes a READ but does NOT route a BDOS 22 Make, which lands in the drive directory or the cwd under a lowercased 8.3 name."},"console":{"type":"integer","minimum":0,"maximum":7,"description":"op:'handles' on mpm2: only handles owned by the process attached to this console."},"idempotency_key":{"type":"string","description":"A client-supplied nonce, not a hash of these arguments. Supply it only to make a retry safe; two deliberate identical calls without a key are two calls."}}}
```

**Out (per op):**
`list` → `{files:[{guest_name,host_name,bytes,sha256?,created:bool,modified:bool,user?:int}], drives:[{letter,backing,writable}], via}`
`to_guest` → `{placed:[{guest_name,via,drive,bytes,host_path_used}], skipped:[{guest_name,reason}]}`
`from_guest` → `{files:[{guest_name,bytes,sha256,content_b64?,resource_uri,host_path?}], truncated:[string]}`
`handles` → `{handles:[{fcb_addr|dos_handle, guest_name, host_path, mode, eol_convert, record_position, write_mode, eof_seen, bytes_on_host, owner:{process,console}?}], limits:{max_open_files,max_locked_records}}`
`resolve` → `{guest_name, would_resolve_to, mechanism:"config_mapping"|"argv_automap"|"drive_letter"|"cwd_fallback", tried:[{path,mechanism,result}]}`

`handles` and `resolve` are phase 5 and cpmemu-first: the data already exists as
`CPMEmulator::open_files` (`src/cpmemu.cc:346`), a `std::map<qkz80_uint16, OpenFile>` whose
`OpenFile` carries `unix_path`, `cpm_name`, `mode`, `position`, `write_mode` and
`eof_seen`. **Nothing in the surveyed MCP ecosystem exposes either.**

**One caveat that must be in the tool description:** the `hostfile` route on RomWBW depends
on `r8.com`/`w8.com`, which exist on `hd1k_combo.img` slice 0 and `hd1k_infocom.img` and
**not on the CP/M 3 slice**. That profile must use the `image` route with the machine
stopped. `w8` probes `HBF_HOST_CAPS` first and refuses on an emulator that answers no.

#### `x80_probe` — ships-today

> Run a package on the cheapest profile and report what operating system it actually needs,
> from the syscalls it made — evidence instead of a screen read. Returns a literal next
> call to make.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["program"],"additionalProperties":false,"$defs":{"FileIn":{"type":"object","additionalProperties":false,"properties":{"guest_name":{"type":"string"},"host_path":{"type":"string"},"content_b64":{"type":"string"},"mode":{"enum":["binary","text"],"default":"binary"}},"required":["guest_name"],"oneOf":[{"required":["host_path"]},{"required":["content_b64"]}]}},"properties":{"program":{"type":"string"},"args":{"type":"array","items":{"type":"string"},"default":[]},"files_in":{"type":"array","default":[],"items":{"$ref":"#/$defs/FileIn"}},"stdin":{"type":"string"},"family":{"enum":["z80","x86","auto"],"default":"auto"},"timeout_ms":{"type":"integer","default":15000,"maximum":300000}}}
```

**Out:**
`{ran_on:"cpm-hosted"|"dos-hosted", verdict:"sufficient"|"needs_richer_os"|"needs_hardware_tier"|"failed_to_load", unimplemented:[{layer,func,name,count}], evidence:[string], recommended_profile:string, escalation:{tool:string, arguments:object}}`

`escalation` is a literal call the agent can make next — e.g.
`{"tool":"x80_cpm_run","arguments":{"profile":"cpm3","program":"…","args":["…"]}}`. It
costs nothing and it is the difference between an agent that reruns on `cpm3` and one that
gives up.

#### `x80_images` — ships-today. **The only tool that touches the network.**

> Download and verify disk images from the pinned `romwbw_disks` catalog. Every other tool
> in this server is `openWorldHint:false`. **Never called implicitly** — no tool call may
> trigger a download.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":false,"properties":{"image_ids":{"type":"array","items":{"type":"string"},"description":"Catalog ids, e.g. hd1k_combo, hd1k_cpm3, emu_avw, mpm2_system, freedos_starter. Omit to list without fetching."},"romwbw_version":{"type":"string","description":"Which catalog generation, e.g. 3.5.1. Defaults to the version the installed romwbw_emu is pinned to: it emulates HBIOS v3.5.1 EXACTLY and emu_validate_rom_hcb refuses a mismatched ROM."},"dry_run":{"type":"boolean","default":true},"allow_fetch":{"type":"boolean","default":false,"description":"Required true alongside dry_run:false to actually download. There is no elicitation; without it this returns isError."},"timeout_ms":{"type":"integer","default":120000,"maximum":600000}}}
```

**Out:**
`{images:[{id,filename,bytes,sha256,licence,present:bool,fetched:bool,path,description}], catalog_version, upstream_package_sha256, notes:[string]}`

`licence` is surfaced **verbatim** from the catalog, which already carries `Mixed` and
`Abandonware` values. MP/M images built with `--tree=src` from DRI source are marked
separately from ones carrying original DRI binaries. Images are never bundled in the
package (§10).

### 6.5 Phase 2 — interactive Z80

#### `x80_open` — needs-small-work

Description carries the retention policy from §4.3 verbatim.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["profile"],"additionalProperties":false,"$defs":{"FileIn":{"type":"object","additionalProperties":false,"properties":{"guest_name":{"type":"string"},"host_path":{"type":"string"},"content_b64":{"type":"string"},"mode":{"enum":["binary","text"],"default":"binary"}},"required":["guest_name"],"oneOf":[{"required":["host_path"]},{"required":["content_b64"]}]}},"properties":{"profile":{"type":"string","description":"A profile id from x80_profiles."},"program":{"type":"string","description":"REQUIRED on the hosted tier (cpm-hosted, dos-hosted): those backends are not shells, they exec one guest binary, so there is nothing to boot into. Give the .COM/.EXE that will BE the session, e.g. a FreeCOM REPL. Ignored on the hardware tier."},"args":{"type":"array","items":{"type":"string"},"default":[]},"rom":{"type":"string","description":"RomWBW ROM: a catalog id or a host path. Must pair with a disk from the matching release; the server refuses a mismatch by default rather than letting '*** WARNING: HBIOS/CBIOS Version Mismatch ***' reach a transcript an agent will misread."},"allow_version_mismatch":{"type":"boolean","default":false,"description":"Run a deliberate ROM/disk skew experiment. The warning is then reported in `warnings`, not suppressed."},"disks":{"type":"array","maxItems":16,"items":{"type":"object","required":["unit"],"additionalProperties":false,"properties":{"unit":{"type":"integer","minimum":0,"maximum":15},"image_id":{"type":"string","description":"Catalog image id, resolved through the pinned sha256."},"host_path":{"type":"string"},"write_protect":{"type":"boolean","default":true,"description":"Defaults TRUE for catalog images. Writable images are copy-on-write cloned into the session directory; the catalog copy is never mutated."}}}},"boot":{"type":"string","description":"Backend boot target: 'C', '2', '2.1', '2.2', '2.3', 'Z' for romwbw_emu; 'none' for the ROM menu; 0x80 for emu88d. ALWAYS sent explicitly - never inherited from NVRAM. romwbw_emu reads the legacy ~/.config NVRAM as a migration fallback even under a sandbox XDG_CONFIG_HOME with --no-config (romwbw_emu.cc:1379-1390, measured), so omitting this boots whatever the developer last chose interactively."},"cpu":{"enum":["8080","z80","8088","186","286","386"]},"consoles":{"type":"integer","minimum":1,"maximum":8,"default":1,"description":"mpm2 only. Opens N SSH clients. You CANNOT choose which console number you get: ConsoleManager::find_free() assigns the highest free console first. Each client is labelled by the 'MP/M II Console N' banner the guest sends on connect."},"default_mode":{"enum":["binary","text","auto"],"default":"binary","description":"cpm-hosted only. Same correctness requirement as the batch verb: auto never resolves on write."},"eol_convert":{"type":"boolean","default":false},"sandbox_from":{"type":"string","description":"Seed the sandbox from this host directory (copied, never mounted)."},"files_in":{"type":"array","default":[],"items":{"$ref":"#/$defs/FileIn"}},"dialect":{"enum":["xterm","z80cpmw","ioscpm","cpmdroid","cpmemu-adm3a"],"default":"xterm","description":"Which port's VT semantics x80_screen renders under. The three GUI ports diverge DELIBERATELY; a screen assertion is only meaningful against a named dialect."},"symbols":{"type":"string","description":"Host path to a .sym file, passed as romwbw_emu --symbols. Enables symbolic breakpoints."},"boot_timeout_ms":{"type":"integer","default":30000,"maximum":300000,"description":"Give up waiting for the OS prompt after this long and return stopped:'boot_timeout' with whatever arrived. A lifetime TTL is a different control and does not substitute."},"idle_timeout_ms":{"type":"integer","default":600000,"maximum":3600000},"ttl_ms":{"type":"integer","maximum":28800000},"mirror":{"type":"boolean","default":false,"description":"Also expose console 0 on a loopback TCP port so a human can telnet in and watch."},"record":{"type":"boolean","default":true,"description":"Record the boot config plus every input byte with its instruction count, enabling deterministic replay via x80_session{op:'reset', replay_to}."}}, "allOf":[{"if":{"properties":{"profile":{"enum":["cpm-hosted","dos-hosted"]}},"required":["profile"]},"then":{"required":["program"]}}]}
```

**Out:**
`{handle, profile, backend, backend_version, caps:[string], consoles:[{index,label,console_id}], boot_output, stopped:"idle"|"match"|"boot_timeout", dialect, sandbox, transcript_uri, screen_uri, mirror_port?, expires_at, escape_char, warnings:[string], image_pins:[{unit,image_id,sha256,romwbw_version}]}`

`escape_char` is returned because reserving `^E` for the debugger **takes that key away
from the guest** for the life of the session, and `^E` is cursor-up in the WordStar
diamond. An agent that is about to drive WordStar needs to know. `consoles[].console_id`
is the number the guest announced, which may not equal `index`.

#### `x80_session` — needs-small-work

> List, inspect, close and reboot machines. `op:"reset"` with `replay_to` is this family's
> substitute for fork/rewind, because no backend here has a save-state.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["op"],"additionalProperties":false,"properties":{"op":{"enum":["list","status","close","reset","gc"]},"handle":{"type":"string","description":"Required for status, close and reset."},"include_dead":{"type":"boolean","default":true,"description":"op:'list': include crashed and expired machines whose sandboxes are still readable."},"flush_disks":{"type":"boolean","default":true,"description":"op:'close': flush dirty images before killing. False discards guest writes."},"collect_files":{"type":"boolean","default":true,"description":"op:'close': extract files created during the session and report them."},"export_to":{"type":"string","description":"op:'close': host directory to copy the sandbox into before teardown."},"boot":{"type":"string","description":"op:'reset': optionally change the boot target on the way through."},"replay_to":{"type":"integer","description":"op:'reset': re-boot, then replay the recorded input log up to this instruction count. Exact for CP/M and DOS workloads with no wall-clock or RNG input. NOT exact for mpm2, whose 60 Hz preemption is wall-clock driven; the result then carries replay_exact:false."},"clear_sandbox":{"type":"boolean","default":false,"description":"op:'reset'."},"older_than_hours":{"type":"integer","default":24,"description":"op:'gc': delete orphaned sandbox directories older than this."}}}
```

**Out (per op):**
`list` → `{machines:[{handle,profile,backend,state:"running"|"suspended"|"crashed"|"expired",created_at,last_activity,expires_at,cpu_seconds,instructions,consoles,sandbox,exit_signal?,stderr_tail?}], orphaned_sandboxes:[{handle,path,age_hours}]}`
`status` → the same record for one machine, plus `caps` and `warnings`
`close` → `{closed:true, exit_reason, cpu_seconds, instructions, dirty_disks:[{unit,bytes,flushed:bool}], transcript_path, transcript_bytes, files_out:[…], sandbox, sandbox_expires_at}`
`reset` → `{handle, boot_output, stopped, instructions, replay_exact:bool, replay_warnings:[string]}`
`gc` → `{removed:[{handle,path,bytes}], reclaimed_bytes}`

#### `x80_run` — needs-small-work. **The expect loop.**

> Advance a running guest a bounded slice and return what it printed since the last call.
> Type a command with `input`, read the reply, call again. **Never blocks.** Stops on
> `until` matched, the guest going idle at a prompt, `timeout_ms`, `max_steps`, a HLT, a
> breakpoint, or guest exit — reported in `stopped`. Input is **always paced**: the server
> waits for the backend's real idle signal or a prompt before sending, because unpaced
> bytes are eaten by the boot loader and a byte arriving during output truncates it (both
> measured).

Argument shape is altairsim's `run`, extended by `console`, `pager` and `idempotency_key`.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"from":{"type":"integer","description":"Optionally set PC before running. Hardware profiles only."},"input":{"type":"string","description":"Raw bytes to type first. Add a trailing \\r (not \\n) to submit a CP/M, MP/M or DOS line; the server rewrites a trailing \\n to \\r."},"until":{"type":"string","description":"Stop as soon as this literal substring appears, e.g. 'A>' or '3A>'."},"until_regex":{"type":"string","description":"As `until`, but a regex over the RENDERED SCREEN rather than the byte stream."},"timeout_ms":{"type":"integer","default":5000,"maximum":600000},"max_steps":{"type":"integer","description":"Instruction-count cap for this call."},"idle_quiet_ms":{"type":"integer","default":300,"description":"Output silence that counts as idle, used only where a backend has no real idle signal. romwbw_emu, mpm2 and emu88 all have one and it is preferred."},"console":{"type":"integer","minimum":0,"maximum":7,"description":"mpm2 only: which console to type at and read from. This is the console NUMBER the guest announced, not an attach index."},"pager":{"enum":["auto","stop","off"],"default":"auto","description":"CP/M 3's DIR paginates with 'Press RETURN to Continue' where CP/M 2.2's does not (measured), and a capture verb that ignores it truncates and then hangs. auto: answer it and keep going, counting in pager_answered. stop: return stopped:'pager'. off: do nothing."},"idempotency_key":{"type":"string","description":"A client-supplied nonce, not a hash. If the client retries this exact call after a transport error, the input is typed once, not twice. Two deliberate identical calls without a key are two calls."}}}
```

**Out:**
`{output:string, stopped:"match"|"idle"|"timeout"|"max_steps"|"halt"|"breakpoint"|"syscall_breakpoint"|"pager"|"exited"|"crashed"|"boot_timeout", pc?:int, steps:int, instructions:int, wall_ms:int, console?:int, pager_answered:int, breakpoint?:{id,addr,symbol?}, suspended:bool, exit_code?:int}`

`output` is the **delta only**; the full transcript is
`80mcp://machine/{handle}/transcript`, so a long session does not eat the context window.
`exit_code` is present only on `dos-hosted`. `stopped:"idle"` SIGSTOPs the child process
group; the next call SIGCONTs it.

#### `x80_send` — needs-small-work

> Type at a guest console without advancing it. Rarely what you want — prefer
> `x80_run{input}`. Exists for byte-exact control sequences and for feeding a console you
> are not about to read.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle","text"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"text":{"type":"string"},"encoding":{"enum":["utf8","hex"],"default":"utf8"},"console":{"type":"integer","minimum":0,"maximum":7},"wait_idle":{"type":"boolean","default":true,"description":"Wait for the guest to be idle before typing. Turning this off is how you deliberately test abort-on-keypress; it is also how you lose your input to the boot loader."},"idempotency_key":{"type":"string"}}}
```

**Out:** `{queued_bytes:int, waited_ms:int, console?:int, op_id:string}`

Bytes are recorded under `op_id` **before** queueing, so a retry resumes rather than
restarts.

#### `x80_recv` — needs-small-work

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"console":{"type":"integer","minimum":0,"maximum":7},"max_bytes":{"type":"integer","default":65536,"maximum":1048576}}}
```

**Out:** `{output:string, truncated:bool, remaining_bytes:int, console?:int}`

#### `x80_screen` — needs-small-work

Reports lossiness structurally, never in prose. See §5.6.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"console":{"type":"integer","minimum":0,"maximum":7},"format":{"enum":["text","cells","both"],"default":"text"},"dialect":{"enum":["xterm","z80cpmw","ioscpm","cpmdroid","cpmemu-adm3a"],"description":"Override the machine's dialect for this read. REQUIRED if you intend to assert on colour or attributes."},"include_scrollback":{"type":"integer","default":0,"maximum":2000}}}
```

**Out:**
`{rows:int, cols:int, text:[string], cells?:[[{ch,fg,bg,attrs}]], cursor:{row,col,visible}, dialect, source:"guest_ram"|"vt:<dialect>", lossy:["high_bit_masked"|"cr_dropped"|"tab_dropped"|"adm3a_translated"|"bios_banner_row0"], video_mode?:string}`

On an EGA/VGA planar mode this returns `isError` with `{"error":"unsupported_video_mode",
"mode":"0x12"}` rather than a confidently wrong image.

#### `x80_regs` — needs-small-work

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"include":{"type":"array","uniqueItems":true,"items":{"enum":["shadow","index","fpu","descriptors","dpmi","process"]},"default":[]}}}
```

**Out, Z80:** `{pc, sp, af, bc, de, hl, ix, iy, af_, bc_, de_, hl_, i, r, im, iff1, iff2, halted, flags:string, instructions, bank:{rom,ram,common}, running_process?:{name,console,pd_addr}}`
**Out, x86:** `{eip, eflags, eax..edi, cs,ds,es,fs,gs,ss with cached bases and limits, cr0..cr4, gdtr, idtr, ldtr, tr, cpl, cycles, halted, fpu?:{st0..st7,cw,sw,tags}}`

Parsed from `sim>`'s fixed-format reply on the stderr pipe. Demonstrated verbatim:

```
sim> r
  A=00  BC=001A  DE=0080  HL=D008  SP=D7A9  PC=0005
Flags: -Z0-0P--
```

`running_process` is populated on `mpm2` only, and it matters: on a preemptive multi-user
system "the registers" are meaningless without knowing whose they are.

#### `x80_step` — needs-small-work

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"count":{"type":"integer","default":1,"minimum":1,"maximum":1000000},"over":{"type":"boolean","default":false,"description":"Step over CALL by setting a temporary breakpoint at the return address."},"trace":{"type":"boolean","default":false,"description":"Return a per-instruction PC/opcode trace, bounded to 4096 entries."}}}
```

**Out:** `{stopped:"count"|"halt"|"breakpoint", pc, instructions, regs:{…}, next_disasm?:[{addr,bytes,text}], trace?:[{pc,opcode,bank}]}`

Demonstrated on `romwbw_emu`: `s 3` → `[Stepping 3 instruction(s)...] PC=D812`.

#### `x80_breakpoints` — needs-small-work

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle","op"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"op":{"enum":["set","clear","clear_all","list"]},"id":{"type":"string"},"addr":{"type":"integer","minimum":0},"symbol":{"type":"string","description":"Resolved against the --symbols file given at x80_open."},"bank":{"type":"integer","description":"Bank-qualified on mpm2 and romwbw_emu: the same address in two banks is two different programs."},"kind":{"enum":["exec","read","write"],"default":"exec","description":"read/write watchpoints are emu88d only, implemented server-side by overriding emu88_mem's virtual store_mem*."},"condition":{"type":"string","description":"Optional expression over registers, evaluated in the adapter, e.g. 'a==0x1a'."}}}
```

**Out:** `{breakpoints:[{id, addr, symbol?, bank?, kind, condition?, hits}]}`

Demonstrated end to end on `romwbw_emu` over a pty: `bp 0005` → `Breakpoint set at 0005`;
`bl` → `Breakpoints: 0005`; `g` plus one CR to wake the guest → `[Breakpoint hit at 0005]`
in 0.06 s, auto-re-entering console mode; `ba` → `Cleared 1 breakpoint(s)`.

#### `x80_disasm` — needs-small-work (Z80)

> Disassemble guest memory. Z80/8080 disassembly is a solved problem *inside this family*
> and x86 is not; the result says which decoder produced it.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle","addr"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"addr":{"type":"integer","minimum":0},"count":{"type":"integer","default":16,"maximum":4096},"hi":{"type":"integer","description":"Disassemble through this address inclusive instead of a fixed count."},"bank":{"type":"integer"},"cpu":{"enum":["8080","z80","16","32"],"description":"Defaults to the machine's active CPU and mode."},"data_ranges":{"type":"array","items":{"type":"string","pattern":"^[0-9A-Fa-f]{1,6}-[0-9A-Fa-f]{1,6}$"},"description":"Force ranges to data, e.g. '1A00-1A7F'. romwbw_emu --trace=FILE already emits exactly this list as a ud80 script."},"timeout_ms":{"type":"integer","default":10000,"maximum":60000}}}
```

**Out:** `{lines:[{addr,bytes,text,label?}], decoder:"ud80"|"zydis", decoder_version}`

**Why this is small work and not a week.** Three of the four competing designs called the
Z80 disassembler the one genuinely missing piece — *"600 lines"*, *"vendor Zydis"*, *"THE
ONE GENUINELY MISSING PIECE."* It is not, on the Z80 side, because **`ud80` already ships
in the family** and is installed on this machine
(`/Users/wohl/Library/Python/3.14/bin/ud80` — verified present). It is a flow-tracing
8080/Z80 disassembler from `avwohl/um80_and_friends` whose `-d RANGE` flag forces a range
to data — and `romwbw_emu`'s own `romwbw_mem.h:248-283 write_trace_script()` already emits
exactly `-d %04X-%04X` lines, with the header comment `# Use with: python3 -m um80.ud80
binary.bin $(cat …)`. So the Z80 half is **~80 lines of subprocess invocation and output
parsing**. x86 needs Zydis vendored into `backends/emu88d` and is phase 5; do not let it
block the x86 modes.

#### `x80_monitor` — ships-today

> Run one backend-native command and return its text verbatim. The escape hatch: anything
> the backend's own debugger can do, in one call, so the tool list never has to be
> exhaustive on day one.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle","command"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"command":{"type":"string"},"timeout_ms":{"type":"integer","default":3000,"maximum":60000,"description":"A command that never re-prompts would otherwise block forever; romwbw_emu has no wall clock. On expiry the partial text is returned with timed_out:true."}}}
```

**Out:** `{text:string, monitor:"sim"|"emu88d"|null, timed_out:bool, available_commands?:[string]}`

**This tool never shells out to the host.** altairsim's `monitor{command:"!…"}` executes an
arbitrary host shell as the launching user *and* corrupts the JSON-RPC stream with the
subprocess's inherited stdout (§1.3). `x80_monitor` passes the command only to the
backend's own debugger, and the backend's stderr is a separate pipe from the JSON-RPC
channel by construction.

**Demonstrated, and this is the whole basis of phase 2.** `romwbw_emu`'s `sim>` debugger is
tty-only (`emu_io_cli.cc:408`, `if (!stdin_is_tty) return false`), so over a pipe it is
unreachable *and* a script's own 0x05 leaks to the guest. Over a **pty with stderr on a
separate pipe** it works end to end: boot to `A>` in 0.24 s; writing `0x05` produced
`sim> ` on **stderr** in 0.11 s (`romwbw_emu.cc:341`), giving a clean machine-parseable
debugger channel with zero crosstalk against guest stdout; `help` listed 17 commands; `r`,
`s 3`, `bp`/`bl`/`g`/`ba` all worked. **That single decision — pty for stdin/stdout, pipe
for stderr — buys the entire debugger family for free.**

#### `x80_trace` — ships-today (stderr parser); `resolved` block is phase 5

> Turn on, turn off or read the backend's structured operating-system-call trace: CP/M
> BDOS 0-40, MP/M XDOS 128-164, DOS INT 21h/31h/67h. **This is the axis the entire existing
> MCP ecosystem lacks** — every CP/M-adjacent server drives the console with an expect loop
> and reaches memory through a monitor; none can answer "which host file did that FCB
> resolve to."

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle","op"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"op":{"enum":["on","off","read","clear"]},"layers":{"type":"array","uniqueItems":true,"items":{"enum":["bdos","bios","xdos","int21","int31","int67","int10","dpmi","exceptions","hbios","instructions"]},"default":["bdos","int21","xdos"]},"filter":{"type":"object","additionalProperties":false,"properties":{"funcs":{"type":"array","items":{"type":"integer"},"description":"BDOS/XDOS function numbers, or DOS AH values."},"names":{"type":"array","items":{"type":"string"},"description":"Symbolic: open, make, close, write_seq, delete, attach_console, read_queue."},"file_glob":{"type":"string"},"console":{"type":"integer","minimum":0,"maximum":7},"process":{"type":"string"},"bank":{"type":"integer"}}},"since_seq":{"type":"integer"},"max_records":{"type":"integer","default":500,"maximum":10000},"include":{"type":"array","uniqueItems":true,"items":{"enum":["args","returns","resolved_paths","fcb","dma","registers","caller_pc"]},"default":["args","returns"]}}}
```

**Out:**
`{active:bool, records:[{seq, t_instr, layer, func, name, console?, process?, bank?, caller_pc?, in:{…}, out:{a,h,carry,…}?, fcb?:{addr,drive,name,ex,cr,r0r1r2}, resolved?:{guest_name, host_path, mode, mechanism:"config_mapping"|"argv_automap"|"drive_letter"|"cwd_fallback", tried:[{path,mechanism,result}]}}], dropped:int, next_seq:int, unimplemented_seen:[{layer,func,count}], source:"stderr_parse"|"events_channel"}`

**Two implementations of one tool, and `source` says which you got.**

- **Phase 2, ships today, ~120 lines, zero upstream.** Parse the line-oriented stderr both
  translators already emit — `cpmemu`'s `CPM_DEBUG_BDOS` output (`bdos_call()` at
  `src/cpmemu.cc:1403` already has the `fprintf`) and `dosiz`'s `DOSIZ_TRACE` at
  `src/bridge.cc:4340` — against a BDOS/INT-21h name table. `source:"stderr_parse"`. The
  resolved host path is already on cpmemu's stderr in this form, so `resolved.host_path`
  is populated but `resolved.mechanism` and `resolved.tried[]` are not.
- **Phase 5, needs upstream, cpmemu-first.** An `--events` NDJSON channel at the same choke
  point, threading the resolved path out of `find_unix_file_ex()`
  (`src/cpmemu.cc:790-879`), gives the full `resolved` block with `mechanism` and `tried[]`.
  `source:"events_channel"`. **Ship it as a separate `cpmemu_dbg` target** that includes
  `cpmemu.cc`'s guts, so the binary three siblings compile from that tree is untouched.

Because environment variables are per-process, `op:"on"` after `x80_open` requires a
restart on the one-shot backends; the server does that transparently and replays via the
record log.

**One thing that will surprise you:** `dosiz`'s `DEBUGGING.md` documents `DOSIZ_CPU_TRACE`
and `DOSIZ_CPU_TRACE_LINES` as live instruction-tracing facilities. Both are **dead** —
their consumer was deleted in the emu88 migration and `g_debug.cpu_trace` is set and read
nowhere. Plan against `grep`, not against the docs.

### 6.6 Phase 3 — MP/M

#### `x80_consoles` — needs-small-work

> Per-console state for a multi-terminal machine: which console number the guest assigned,
> whether it is at a prompt, what it last printed. **No other MCP server in existence has a
> concept that maps onto "which of my four terminals is this output for."**

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle"],"additionalProperties":false,"properties":{"handle":{"type":"string"}}}
```

**Out:**
`{consoles:[{index:int, console_id:int, banner:string, connected:bool, idle:bool, pending_output_bytes:int, last_line:string, cursor:{row,col}}], active_consoles:int, max_consoles:int, banks:[{id,base,size,role}]}`

`index` is our attach order; `console_id` is the number the guest announced in its
`MP/M II Console N` banner; `banner` is that line verbatim. **They are not the same thing
and the schema does not pretend they are.** On a stock 4-console `mpm2_emu` build,
`active_consoles` is 4, `max_consoles` is 8, and `banks` reports one system bank plus seven
48K user banks — measured verbatim from the boot log as `MP/M II Sys 9100H 6F00H Bank 0`
and seven × `Memseg Usr 0000H C000H Bank 1..7`.

### 6.7 Phase 5 — the syscall debugger

#### `x80_syscall_break` — needs-medium-work

> Stop the machine the next time it makes a particular operating-system call — optionally
> only for a matching filename, a particular console, a particular process, the Nth
> occurrence, or **only when the call fails**. Every existing CP/M and DOS server breaks on
> *addresses*; nothing in the surveyed ecosystem can break on a failing syscall.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle","op"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"op":{"enum":["set","clear","clear_all","list"]},"id":{"type":"string"},"layer":{"enum":["bdos","bios","xdos","int21"]},"func":{"type":"integer"},"name":{"type":"string","description":"Symbolic alternative to func: open, make, close, write_seq, delete, attach_console, read_queue."},"when":{"type":"object","additionalProperties":false,"properties":{"file_glob":{"type":"string"},"console":{"type":"integer","minimum":0,"maximum":7},"process":{"type":"string"},"nth":{"type":"integer","minimum":1},"returns_error":{"type":"boolean","description":"Break only when the call FAILS. On MP/M this is how you catch a lock conflict at the instant it happens."}}},"on_hit":{"enum":["stop","log"],"default":"stop"}}}
```

**Out:** `{id, active:[{id,layer,func,name,when,hits,on_hit}]}`

"Break the next time anyone opens `*.LBR`." "Break when console 2's BDOS 15 **fails**."
Rides on `x80_trace`'s choke point: the predicate is evaluated at the same point and sets
the runner's stop flag, ~60 lines per backend on top of the trace. In `cpmemu` the run loop
is `main()`-owned (`src/cpmemu.cc:3074`) with no re-entrant step entry point, so "stop"
means returning from the run loop into a control-channel wait — the one structural change,
~80 lines, and the reason this must be a `cpmemu_dbg` target rather than a change to the
shipped binary.

### 6.8 Deliberately merged, and why

`x80_files` absorbs what other designs split into `file_put`/`file_get`/`file_list` plus
`file_handles`. The op names carry the direction (`to_guest`/`from_guest`), which is
*less* ambiguous than `put`/`get`, and the four operations share almost every argument.
`x80_session` absorbs `machine_list`/`machine_stop`/`machine_reset`. Both merges serve the
tool-count rule; neither loses a capability.

**Five `mpm_*` kernel-structure readers are deliberately not in the surface.** Walking DRI
PL/M declaration-order offsets through SYSDAT to the process descriptors has a failure mode
the proposing design itself named as the worst kind — *"plausible wrong output, not a
crash"* — in a server whose entire purpose is to be believed instead of a screen. MP/M gets
one `x80_consoles` tool and a `console` parameter, per the profile-not-a-tool rule. If they
are ever built, `datapg.json` emitted by `gensys.py` must land **in the same commit** as
the readers — never hardcode DATAPG offsets — plus a self-check that refuses to report when
the running process is unreachable from `rlr`.

### 6.9 `x80_mem_read` / `x80_mem_write`: the one tool everybody assumes ships and does not

**This is the largest correction in the document.** Every emulator MCP server in the survey
has `read_memory`/`write_memory` as its most conventional tool, and three of the four
competing designs booked it as "~60 lines of `dm`-reply parsing over the pty, ships today."
**That is wrong on RomWBW, and it was demonstrated wrong.**

The decisive test: at the CP/M `A>` prompt, enter `sim>`, then

```
sim> d 0000 FF FF FF FF FF FF FF FF
Deposited 8 byte(s) at 0000
sim> dm 0000 8
0000: FF FF FF FF FF FF FF FF
```

then `g`, then send `DIR` to the guest. **The guest was completely unaffected** — `DIR`
returned the normal directory listing. Overwriting CP/M's warm-boot vector at 0x0000 and
the BDOS entry at 0x0005 would be instantly fatal if that were guest memory. Independently,
`dm 0000 32`, `dm 0005 16` and `dm D000 32` all returned zeros while CP/M was demonstrably
running and the boot log said the CP/M image occupies 0xD000-0xFE00.

The cause, from source: `romwbw_emu.cc:460` (`e`), `:486` (`dm`) and `:521` (`d`) all do
`uint8_t* mem = cpu->get_mem();` — the qkz80 CPU's flat 64K array. The guest's real memory
is the bank-switched MMU in `src/romwbw_mem.h`, whose accessor is
`read_bank(uint8_t bank_id, uint16_t offset)` at `:211`, with RAM/ROM/shadow-RAM selection
at `:318-329`. **`get_mem` has exactly three call sites in the whole tree — all three are
these three debugger commands.** In RomWBW mode, the only mode that boots CP/M, `sim>`'s
`e`/`dm`/`d` operate on a dead scratch buffer: self-consistent, and disconnected from the
guest.

**Consequences for this spec.** `x80_mem_read` and `x80_mem_write` are **phase 4,
needs-upstream**, not phase 2 ships-today. They require §8 item 1: re-point the three
commands at `read_bank`/`write_bank` and add a bank argument, because a flat 16-bit address
is ambiguous on this machine. Both schemas therefore require `bank` to be either given or
explicitly defaulted to the current mapping, and both results echo the bank they used.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle","addr"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"addr":{"type":"integer","minimum":0},"count":{"type":"integer","default":256,"maximum":65536},"bank":{"type":"integer","description":"Z80 banked backends: which ROM/RAM bank. Omit for the CURRENT mapping. A flat 16-bit address is ambiguous on a RomWBW machine and the result always echoes the bank actually read."},"space":{"enum":["logical","linear","physical","seg_off"],"default":"logical","description":"x86 only: logical goes through the current segment and paging translation."},"segment":{"type":"integer"},"format":{"enum":["hexdump","base64","text","cells","both"],"default":"hexdump","description":"cells decodes an 80x25 char/attr region, for the x86 text screen at 0xB8000."}}}
```

**Out:** `{addr, count, bank, space, hexdump?:[string], base64?:string, text?:string, cells?:[[{ch,attr}]], ascii?:[string], truncated:bool, source:"read_bank"|"get_mem"|"guest_ram"}`

`source` exists so that if this ever regresses to the disconnected buffer, the result says
so instead of returning plausible zeros.

`x80_mem_write` takes the same addressing. It is destructive by construction: **there is
no snapshot to roll back to anywhere in this family**, so a bad patch means restarting the
machine.

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["handle","addr"],"additionalProperties":false,"properties":{"handle":{"type":"string"},"addr":{"type":"integer","minimum":0},"bank":{"type":"integer","description":"Omit for the current mapping. The result always echoes the bank actually written."},"space":{"enum":["logical","linear","physical","seg_off"],"default":"logical"},"segment":{"type":"integer"},"data_b64":{"type":"string"},"hex":{"type":"string","pattern":"^([0-9A-Fa-f]{2}[ ]?)+$"},"fill":{"type":"object","additionalProperties":false,"required":["byte","length"],"properties":{"byte":{"type":"integer","minimum":0,"maximum":255},"length":{"type":"integer","minimum":1,"maximum":65536}}},"idempotency_key":{"type":"string"}},"oneOf":[{"required":["data_b64"]},{"required":["hex"]},{"required":["fill"]}]}
```

**Out:** `{written:int, addr, bank, space, previous_b64:string, source:"write_bank"|"get_mem"|"guest_ram"}`

### 6.10 Replay, since there is no save-state

`x80_session{op:"reset", replay_to:N}` records `{instruction_count, console, bytes}` per
send into `session.jsonl` when `x80_open{record:true}` (the default), and re-drives them on
reboot. ~200 lines of server code, zero emulator changes. It is **exact** for CP/M and DOS
workloads with no wall-clock or RNG input and **not exact for `mpm2`**, whose 60 Hz
preemption is wall-clock driven — precisely where you would most want it. The result says
`replay_exact:false` with warnings in that case, which is honest and is not what a user
chasing a race wants to hear. That is the honest limit of the 80% substitute.

---

## 7. Worked examples

**Every value below was measured on this machine. Nothing is composed.** `_meta` is shown
in full once and elided after.

### 7.1 Batch — 80un unpacks a 23-member ARC under `cpm-hosted` (use case B, phase 1)

```json
{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{
  "name":"x80_cpm_run",
  "arguments":{
    "profile":"cpm-hosted",
    "cpu":"z80",
    "program":"/Users/wohl/src/80un/80un.com",
    "args":["M9.ARC"],
    "files_in":[{"guest_name":"M9.ARC","host_path":"/Users/wohl/src/80un/tests/samples/arc/method9.arc","mode":"binary"}],
    "default_mode":"binary",
    "eol_convert":false,
    "timeout_ms":10000,
    "assert":{
      "stdout_contains":["23 file(s) extracted"],
      "stdout_not_contains":["Error","Cannot open","Invalid"],
      "files_created":["TIME2.ASM","ZTIM-S3.CPM"],
      "no_unimplemented_bdos":true
    }
  },
  "_meta":{"protocolVersion":"2026-07-28","clientCapabilities":{}}
}}
```

The server does five things the caller never sees: mktemp a sandbox; synthesize a `.cfg`;
stage `M9.ARC`; snapshot the directory; exec `cpmemu <cfg> M9.ARC` in its own process group
under a 10 s deadline with stdin from `/dev/null`. The synthesized cfg, verbatim from the
demonstrated run:

```
program = /Users/wohl/src/80un/80un.com
cd = <sandbox>/guest
default_mode = binary
eol_convert = false
printer = <sandbox>/.lst
aux_output = <sandbox>/.pun
```

```json
{"jsonrpc":"2.0","id":7,"result":{
  "resultType":"complete",
  "isError":false,
  "structuredContent":{
    "pass":true,
    "exit_reason":"jmp_0",
    "wall_ms":630,
    "stdout":"\r\n80UN - CP/M Archive Unpacker v2.3\r\n\r\nExtracting:\r\n  -03MAR86 OK\r\n  B5-TIME.INF OK\r\n  B5C-2805.INS OK\r\n  ...\r\n  TIME2.ASM OK\r\n  ZTIM-S3.CPM OK\r\n\r\n23 file(s) extracted\r\n",
    "stderr":"CPU mode: Z80\nLoaded 21336 bytes from /Users/wohl/src/80un/80un.com\nProgram exit via JMP 0\n",
    "files_out":[
      {"guest_name":"B5-TIME.INF","host_name":"b5-time.inf","bytes":1664,"sha256":"…"},
      {"guest_name":"TIME2.ASM","host_name":"time2.asm","bytes":"…","sha256":"…"}
    ],
    "list_output":{"bytes":0},
    "punch_output":{"bytes":0},
    "assertions":[
      {"kind":"stdout_contains","value":"23 file(s) extracted","ok":true},
      {"kind":"stdout_not_contains","value":"Error","ok":true},
      {"kind":"files_created","value":"TIME2.ASM","ok":true},
      {"kind":"no_unimplemented_bdos","value":true,"ok":true}
    ],
    "diagnostics":{"unimplemented_bdos":[],"config_warnings":[]},
    "fidelity":{"tier":"hosted","divergences":[
      "setup_command_line() never writes the FCB at 0x5C, so a program testing fcb(1)=' ' for its usage banner takes the wrong branch. 80un's usage path is unreachable here.",
      "Created filenames are lowercased on the host: B5-TIME.INF -> b5-time.inf."
    ]}
  }
}}
```

`stderr` is verbatim from the run (`t3/guest_stderr.txt`). `"bytes":1664` is the whole point
of the fidelity block: the Python reference wrote **1537** bytes for the same member, and
`ceil(1537/128)*128 = 1664`. **`assert.file_sha256` against a golden is a trap unless you
know which convention the golden was made under.**

**The negative case, which is why there is no `exit_code` field.** The same fixture with
`default_mode:"auto"` and `eol_convert:true` — the pair cpmemu uses when it is handed no
config file at all:

```json
{"pass":false,"exit_reason":"jmp_0","wall_ms":"…",
 "stdout":"\r\n80UN - CP/M Archive Unpacker v2.3\r\n\r\nExtracting:\r\n  -03MAR86 OK\r\n  B5-TIME.INF\r\n  Error\r\n\r\n1 file(s) extracted\r\n",
 "stderr":"CPU mode: Z80\nLoaded 21336 bytes from …/80un.com\nProgram exit via JMP 0\n",
 "files_out":[{"guest_name":"B5-TIME.INF","host_name":"b5-time.inf","bytes":1437,"sha256":"…"}],
 "assertions":[{"kind":"stdout_contains","value":"23 file(s) extracted","ok":false,"actual":"1 file(s) extracted"}],
 "warnings":["default_mode was 'auto'; cpmemu's auto mode never resolves on write"]}
```

**1 of 23 files. Truncated (1437 vs 1664). And the process exit code was 0, with
`Program exit via JMP 0` on stderr.** A 96%-failed run is indistinguishable from success at
the process level. Any CP/M schema with a bare `exit_code` is lying.

### 7.2 Differential — the same algorithm as a CP/M `.COM` and as Python

```json
{"name":"x80_diff_run","arguments":{
  "guest":{"profile":"cpm-hosted","program":"/Users/wohl/src/80un/80un.com","args":["M9.ARC"],"default_mode":"binary","eol_convert":false},
  "reference":{"argv":["python3","-m","un80.cli","{input}","-o","{outdir}"],"env":{"PYTHONPATH":"src"},"cwd":"/Users/wohl/src/80un"},
  "inputs":[{"guest_name":"M9.ARC","host_path":"/Users/wohl/src/80un/tests/samples/arc/method9.arc","mode":"binary"}],
  "normalize":["lowercase_names","pad_to_record"],
  "compare":"bytes","timeout_ms":60000}}
```

```json
{"pass":true,"normalization_applied":["lowercase_names","pad_to_record"],
 "files_compared":23,"identical":23,"differ":0,
 "only_in_guest":[],"only_in_reference":[],"diffs":[]}
```

**Measured, and it is why `normalize` is an enum and not a boolean.** Without it, a raw
`diff -rq guest py` reports **46 lines, every member "Only in" on both sides**, because
cpmemu lowercases created names. Under `lowercase_names` alone, 2 members are byte-identical
and 21 differ. Under both, **23 of 23 match**: 0 content differences, guest size ==
`ceil(python_size/128)*128` for 23 of 23 members, pad byte `0x1A` throughout. Those are
exactly the two normalizations that are needed, and exactly no more.

### 7.3 Interactive — boot CP/M 3, break on the BDOS entry, and hit the pager

```json
{"name":"x80_open","arguments":{
  "profile":"cpm3",
  "rom":"emu_romwbw",
  "disks":[{"unit":0,"image_id":"hd1k_combo","write_protect":false}],
  "boot":"2.3",
  "dialect":"xterm",
  "boot_timeout_ms":30000,
  "mirror":true}}
```

The server CoW-clones the 51,380,224-byte combo image into the session directory, creates
`<session>/home` and `<session>/xdg`, and launches under a pty with stderr on its own pipe,
in a new process group:

```
HOME=<session>/home XDG_CONFIG_HOME=<session>/xdg \
  romwbw_emu --no-config --romwbw=<cache>/emu_romwbw.rom \
             --disk0=<session>/disks/0.img --boot=2.3 --escape=05
```

All four isolation elements are mandatory (§5.4, Invariant 1).

```json
{"resultType":"complete","structuredContent":{
  "handle":"m_4XQ7K2ZB9NTVW3RH",
  "profile":"cpm3","backend":"romwbw_emu","backend_version":"1.38",
  "caps":["run","send","recv","screen_text","regs","step","breakpoints","disasm","monitor","trace"],
  "consoles":[{"index":0,"label":"console","console_id":0}],
  "boot_output":"CP/M V3.0 Loader\r\nCopyright (C) 1998, Caldera Inc.\r\n BNKBIOS3 SPR  F600  0800\r\n BNKBIOS3 SPR  4700  3900\r\n RESBDOS3 SPR  F000  0600\r\n BNKBDOS3 SPR  1900  2E00\r\n\r\n 60K TPA\r\n\r\nCP/M v3.0 [BANKED] for HBIOS v3.5.1\r\n\r\nRAM Disk Initialized\r\n\r\nA>",
  "stopped":"idle",
  "dialect":"xterm",
  "escape_char":"^E",
  "sandbox":"<session>",
  "transcript_uri":"80mcp://machine/m_4XQ7K2ZB9NTVW3RH/transcript",
  "mirror_port":2323,
  "warnings":[],
  "image_pins":[{"unit":0,"image_id":"hd1k_combo","sha256":"…","romwbw_version":"3.5.1"}]
}}
```

`boot_output` is verbatim from the demonstrated run (`t2/stdout.txt`, lines 34-48). Note
`[BANKED]` and `60K TPA` — real banked CP/M 3. Note also that `caps` does **not** contain
`mem_read`, because on this backend it does not work (§6.9).

```json
{"name":"x80_run","arguments":{"handle":"m_4XQ7K2ZB9NTVW3RH","input":"DIR\r","until":"A>","timeout_ms":5000,"pager":"auto"}}
```

```json
{"output":"DIR\r\nA: DATE     COM : DEVICE   COM : DIR      COM : DUMP     COM : ED       COM \r\nA: ERASE    COM : GENCOM   COM : GET      COM : HELP     COM : HELP     HLP \r\n… \r\nA: LS       COM : LSWEEP   COM : MBASIC   COM : NULU     COM : PMARC    COM \r\nA: PMEXT    COM : RMXSUB1  COM : SUPERSUB COM : TDLBASIC COM : TE       COM \r\nA>",
 "stopped":"match","pager_answered":1,"instructions":1842771,"wall_ms":41,"suspended":false}
```

`pager_answered:1` is the CP/M 3 finding in action. The demonstrated raw capture ended at
`Press RETURN to Continue ` after 21 lines and went no further; with `pager:"off"` this
call would return `stopped:"timeout"` with a truncated listing, which is exactly the silent
failure the field exists to prevent.

Now the debugger, over the same pty, by sending `0x05` first:

```json
{"name":"x80_breakpoints","arguments":{"handle":"m_4XQ7K2ZB9NTVW3RH","op":"set","addr":5}}
```
→ `{"breakpoints":[{"id":"bp1","addr":5,"kind":"exec","hits":0}]}`   *(sim>: `Breakpoint set at 0005`)*

```json
{"name":"x80_run","arguments":{"handle":"m_4XQ7K2ZB9NTVW3RH","input":"SHOW\r","timeout_ms":5000}}
```
→ `{"output":"SHOW","stopped":"breakpoint","breakpoint":{"id":"bp1","addr":5},"wall_ms":60}`

The 0.06 s is measured: `g` plus one CR to wake the guest produced `[Breakpoint hit at
0005]` in 0.06 s, auto-re-entering console mode.

```json
{"name":"x80_regs","arguments":{"handle":"m_4XQ7K2ZB9NTVW3RH"}}
```
→ `{"pc":5,"sp":55209,"af":0,"bc":26,"de":128,"hl":53256,"halted":false,"flags":"-Z0-0P--","bank":{"rom":0,"ram":142,"common":143}}`

Parsed from `sim>`'s reply, which is verbatim:
`  A=00  BC=001A  DE=0080  HL=D008  SP=D7A9  PC=0005` / `Flags: -Z0-0P--`.

### 7.4 MP/M — four consoles, one Z80 (phase 3, unclaimed ground)

```json
{"name":"x80_open","arguments":{"profile":"mpm2","consoles":4,
  "disks":[{"unit":0,"image_id":"mpm2_system","write_protect":false}],
  "boot_timeout_ms":30000}}
```

The server picks two free loopback ports by binding and closing them (**never** passes 0 —
`parse_listen_address` rejects it despite `--help` saying `0 to disable`), mints an RSA host
key with `ssh-keygen` (none ships in the checkout), sets
`DYLD_LIBRARY_PATH=/Users/wohl/src/cpmemu/src` on macOS (`mpm2_emu` hard-links
`/usr/local/lib/libqkz80.4.dylib`; without it, RC 134 dyld failure), launches:

```
mpm2_emu -n -k <session>/keys/host -p 127.0.0.1:<p1> -w 127.0.0.1:<p2> \
         --log <session>/mpm2.log -t 0 -d A:<session>/disks/0.img
```

and then opens four `ssh -tt -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`
clients. **It does not use `-l`** (§3.4, Correction 2).

```json
{"structuredContent":{
  "handle":"m_9F2LP4TQ8CXKM6VN","profile":"mpm2","backend":"mpm2","backend_version":"0.3.4",
  "caps":["run","send","recv","screen_text","consoles"],
  "consoles":[{"index":0,"label":"MP/M II Console 3","console_id":3},
              {"index":1,"label":"MP/M II Console 2","console_id":2},
              {"index":2,"label":"MP/M II Console 1","console_id":1},
              {"index":3,"label":"MP/M II Console 0","console_id":0}],
  "boot_output":"MP/M II V2.1 Loader   \r\nCopyright (C) 1981, Digital Research\r\nNmb of consoles     =  4\r\nBreakpoint RST #    =  6\r\nMemory Segment Table:\r\nSYSTEM  DAT  FF00H  0100H\r\n…\r\nMP/M II Sys  9100H  6F00H  Bank 0\r\nMemseg  Usr  0000H  C000H  Bank 1\r\n…\r\nMemseg  Usr  0000H  C000H  Bank 7\r\n\r\nMP/M II V2.1\r\nCopyright (C) 1982, Digital Research\r\n",
  "stopped":"idle",
  "warnings":["mpm2_emu resolved libqkz80.4.dylib via DYLD_LIBRARY_PATH; the binary's install-name /usr/local/lib/libqkz80.4.dylib is not present on this host"]
}}
```

`boot_output` is verbatim from the demonstrated boot. **Note the console ordering**:
`ConsoleManager::find_free()` assigns highest-first, so the first SSH client gets console 3.
`index` is attach order; `console_id` is what the guest said. Any design that assumes
`0A>`..`3A>` numbered by attachment is wrong, and this schema does not make that assumption.

Two consoles at once — the thing no existing MCP server can express:

```json
{"name":"x80_run","arguments":{"handle":"m_9F2LP4TQ8CXKM6VN","console":3,"input":"DIR\r","until":"3A>"}}
{"name":"x80_run","arguments":{"handle":"m_9F2LP4TQ8CXKM6VN","console":2,"input":"USERS\r","until":"2A>"}}
```

Each returns only its own console's bytes. The demonstrated two-console transcript:

```
MP/M II Console 3            MP/M II Console 2
MP/M II V2.1                 MP/M II V2.1
3A>DIR                       2A>USERS
00:00:09 A:DIR     .PRL      00:00:10
Directory for User  3:       USERS?
A: $3$      SUP              2A>
3A>
```

**Four** concurrent consoles have never been run. That is the phase-3 acceptance gate, and
until it passes, `x80_profiles` reports `mpm2` with
`caps:["run","send","recv","consoles"]` and a `blocked_by` note saying so.

### 7.5 Batch — a real DOS toolchain under `dos-hosted` (use case B, phase 1)

```json
{"name":"x80_dos_run","arguments":{
  "profile":"dos-hosted",
  "program":"/Users/wohl/src/dosiz/tests/SORT.EXE",
  "files_in":[{"guest_name":"IN.TXT","host_path":"./in.txt","mode":"text"}],
  "stdin":"",
  "args":[],
  "timeout_ms":20000,
  "expect_exit_code":0,
  "assert":{"files_created":["OUT.TXT"],"no_unimplemented_int21":true}}}
```

The server copies `SORT.EXE` into the sandbox and runs
`dosiz SORT.EXE < IN.TXT > OUT.TXT` with **cwd = sandbox and a relative program name**
(§5.5). Demonstrated: RC 0 in **0.022 s** wall, `OUT.TXT` = `apple\r / banana\r / cherry\r
/ pear\r` — CR preserved, DOS convention — from an unsorted `IN.TXT`.

```json
{"pass":true,"exit_code":0,"exit_code_meaning":"guest_exit","wall_ms":22,
 "stdout":"","stderr":"",
 "files_out":[{"guest_name":"OUT.TXT","bytes":27,"sha256":"…","content_b64":"…"}],
 "assertions":[{"kind":"files_created","value":"OUT.TXT","ok":true},
               {"kind":"no_unimplemented_int21","value":true,"ok":true}],
 "diagnostics":{"unimplemented_int21":[],"stderr_filtered":[
   "dosiz: ethernet/slirp backend unavailable; INT 0x60 packet driver will accept guest calls but RX/TX will no-op.",
   "dosiz: Crynwr pktdrv installed at INT 60h, stub at 0060:0000"]}}
```

Those two lines are verbatim from `t4/err.txt` and print on **every single** `dosiz` run.
The server strips exactly those two known strings and records that it did, rather than
silently swallowing stderr.

**The failure case, where `x80_dos_run` earns its keep**, because unlike CP/M there is a
real exit code:

```json
{"name":"x80_dos_run","arguments":{"profile":"dos-hosted",
  "program":"/Users/wohl/src/dosiz/tests/DJ_PRINTF.exe","expect_exit_code":0}}
```
→
```json
{"pass":false,"exit_code":7,"exit_code_meaning":"guest_exit",
 "stdout":"int=42 str=hello\r\nflt=3.142 hex=0xDEADBEEF\r\ndj-printf=ok\r\n",
 "assertions":[{"kind":"expect_exit_code","value":0,"ok":false,"actual":7}]}
```

`rc 7` and that exact stdout are measured. And the argv[0] trap, also measured, which the
server exists to make impossible:

```
$ ./build/dosiz /abs/path/tests/DJ_PRINTF.exe
C:\PRIVATE\TMP\...\DJ_PRINTF.EXE: can't open      # rc 102
```

### 7.6 The full `dosiz` suite as one call

`dosiz`'s own `tests/djgpp/run.sh` is this tool already implemented in bash: **42 passed, 0
failed in 2.729 s**, covering FREECOM, MAKE, GREP, DIFF, SED, SORT, GAWK, GZIP, TAR, FLEX,
BC, M4 and the DPMI fixtures. The server's job is to expose that pattern as a tool call, not
to reinvent it. The same is true of `cpmemu`'s `tests/run_tests.sh` `check()` — 102 checks,
~30 s.

---

## 8. Upstream change table

Ordered by leverage. **Note how much of the column is empty: nothing in phases 1-3 needs
any of these.**

| # | repo | file | size | what it unlocks | phase |
|---|---|---|---|---|---|
| 1 | **romwbw_emu** | `src/romwbw_emu.cc:460` (`e`), `:486` (`dm`), `:521` (`d`) — re-point from `cpu->get_mem()` to `romwbw_mem.h:211 read_bank()` / a matching `write_bank`, and add a bank argument to each command | ~60 lines | **`x80_mem_read`/`x80_mem_write` at all.** Demonstrated broken: overwriting 0x0000-0x0007 with FF through `d`, then resuming, left the guest completely unaffected. `get_mem` has exactly three call sites in the tree and all three are these commands. This is the single most conventional tool in every emulator MCP server and it does not work on RomWBW guests today. | 4 |
| 2 | **80mcp** (new) | `backends/emu88d/main.cc`, `dos_io_mcp.cc`, `mbp.cc` | ~600 lines | **The `x86-bare` and `freedos` profiles exist at all.** There is no CLI runner in qxDOS — `boot()`'s only caller is `Emu88Emulator.mm:823`. Compiles `../qxDOS/emu88/*.cc` **by path** exactly as dosiz does, with all new code in **our** translation units, so qxDOS's fixed six-file dosiz link list is untouched. | 5 |
| 3 | **80mcp** | `backends/emu88d/dos_io_mcp.cc` — override the six `host_file_*` methods | ~30 lines | `INT E0h` and `R.COM`/`W.COM` start working on the emu88 path: byte-granular host file I/O with no disk-image surgery. Verified: the six methods are `virtual … { return false; }` at `qxDOS/emu88/dos_io.h:74-80` and nobody overrides them, so overriding them in **our** subclass is **zero qxDOS change** — contrary to the obvious reading. | 5 |
| 4 | **dosiz** | `src/bridge.cc` — drop the six `if (g_windowed)` guards at :4364, :4386, :4396, :4429, :4444, :4691, plus :8556; add `DOSIZ_TEXT_DUMP` | ~30 lines | **The single biggest unlock in the family.** The 80×25 buffer at 0xB8000 is real and `screen_putchar()` maintains it correctly; the comment at `bridge.cc:7753` already says it is *meant* to be unconditional and the call sites contradict it. Turns `dos-hosted` from line-oriented into screen-oriented. Today `DOSIZ_FRAME_DUMP` is the only way in and it *also* silently moves INT 10h teletype output off stdout. | 4 |
| 5 | **dosiz** | `src/bridge.cc` ~:8225-8300 and `src/compat/dosbox_compat.cc:657` | ~5 lines | Gates the two unconditional Crynwr packet-driver stderr lines that print on **every** run. Already an open item in dosiz's own `todo.txt`. | 4 |
| 6 | **romwbw_emu** | `src/romwbw_emu.cc` — new `--control=PATH` fifo carrying `sim>` commands | ~100 lines | Converts the romwbw backend from MBP adapter shape 2 (pty) to shape 1 (native), removing the tty-only debugger constraint. **Do it as a NEW flag** — `emu_console_check_escape()` is a contract three ports keep under `DOWNSTREAM.md`, so changing its semantics is a cross-port migration; adding a second control path beside it is not. | 4 |
| 7 | **cpmemu** | `src/cpmemu.cc` `bdos_make_file()` (~:2073-2126) — call `detect_file_mode()` | 2 lines | Fixes the silent corruption at source rather than by synthesized `.cfg`. `MODE_AUTO` is never resolved on **write**; measured, that aborts a 23-member extraction after one file and still exits 0. | 4 |
| 8 | **cpmemu** | `src/cpmemu.cc` `setup_command_line()` (~:625-632) — blank-fill the FCB at 0x5C | ~5 lines | Removes a real fidelity trap: 80un's usage-banner path tests `if fcb(1) = ' '` and is **unreachable** under cpmemu because 0x5C is never written. The harness currently green-lights behaviour that differs from real hardware, and only an interactive human would notice. | 4 |
| 9 | **romwbw_emu** | `disks/disks.xml:34` | 1 line | Still says CP/M 3 is *"NOT WORKING, under investigation."* It boots, banked, and runs `DIR`. `ioscpm/release_assets/disks.xml:39` was corrected in v1.4.1 and never propagated back. An agent that reads this catalog will refuse to try a mode that works. | 2 |
| 10 | **cpmemu** | `src/cpmemu.cc` — a `--default-mode=` flag | ~10 lines | Lets the server stop synthesizing a `.cfg` for every non-trivial batch run. Cosmetic; we synthesize regardless. | 4 |
| 11 | **cpmemu** | `src/cpmemu.cc`: `emit_event()` on a `--events` fd from `bdos_call()` (:1403); thread the resolved host path out of `find_unix_file_ex()` (:790-879); ship as a separate `cpmemu_dbg` target | ~120 lines | `x80_trace`'s full `resolved` block — `mechanism` and `tried[]`, the thing no other CP/M server has — plus `x80_files{op:"handles"\|"resolve"}` from `open_files` (:346). **Keep the shipped `cpmemu` three siblings compile from this tree untouched.** | 5 |
| 12 | **dosiz** | `src/bridge.cc:4340` — `DOSIZ_EVENTS=path` NDJSON, carrying the path from `resolve_path()`/`dos_to_host()` | ~150 lines | The same `resolved` block on the DOS side. | 5 |
| 13 | **dosiz** | `src/compat/dosbox_compat.cc:576-597` (`DOSBOX_RunMachine`) | ~5 lines | An instruction counter and a deadline check in the run loop plus a distinct exit code. There is **no timeout of any kind** today — a 2-byte `EB FE` spin ran until SIGKILL. We kill by process group regardless, so this only upgrades "killed" to "timed out" in the result. | 4 |
| 14 | **mpm2** | build: add an `@rpath`/`@loader_path` install-name for `libqkz80`, or link it statically | build change | **`mpm2_emu` runs at all on a machine that has not installed libqkz80.** Demonstrated: `otool -L` shows the hard install-name `/usr/local/lib/libqkz80.4.dylib`; without `DYLD_LIBRARY_PATH` it is RC 134, dyld failure. Until this lands, the server must set `DYLD_LIBRARY_PATH` and say so in `warnings`. | 3 |
| 15 | **romwbw_emu** | `src/romwbw_emu.cc` — wire `setHostCmdLine()` to a `--guest-args=` flag | 2 lines + a flag | `HBF_HOST_GETARG` (0xE7) is fully implemented in `hbios_dispatch.cc` but `setHostCmdLine()` has **no caller** in `src/` or `web/`, so a guest program cannot read a host argv today. Gives hardware-tier batch runs an argv. | 4 |
| 16 | **romwbw_disks** | `catalog/v0/*/catalog.json` — add an `mpm2` section | catalog data | MP/M images get the same sha256 pinning, provenance and licence fields as the other 23. The two repos already share `cpm_disk.py` through `$CPM_DISK`, so this is the natural home rather than a second catalog. Prefer `--tree=src`-built images for the licence story. | 3 |
| 17 | **mpm2** | new console transport beside SSH, against `include/console.h`'s `ConsoleQueue` | ~200-300 lines | Drops the `libssh` dependency for MP/M mode. The seam is already clean: SSH threads only touch queues and the Z80 stays on the main thread. **Purely optional — SSH works unmodified today.** | never, unless libssh becomes a problem |
| 18 | **z80cpmw** | a headless console exe exposing `TerminalView::cellAt()` | ~150 lines | A real Windows screen model for dialect verification. `EmulatorEngine.cpp`, `emu_io_windows.cpp`, `TerminalView.cpp` and `TerminalView.h` all `grep -c wx` → 0, and 516 headless checks already drive `outputChar()`/`cellAt()`. The only path to settling "is `ESC[31m` red" and the only pixel-verified VT model in the family. | 6 |
| 19 | **cpmemu** | `src/cpmemu.cc` — `--exit-codes` opt-in with distinct codes per termination reason | ~30 lines | Would let `x80_cpm_run` report *how* a run ended without parsing stderr sentinels. Must be behind a flag: the 102-check suite depends on the current always-0 contract. Low priority — the sentinels parse fine. | 4 |

**Explicitly not on this list:** any change to `qxDOS/emu88/*.cc` (a seventh `.cc` builds in
qxDOS and fails to LINK in dosiz — put new code in a header or in ours); any change to
`emu_io.h` (478-line contract, four ports, 39 KB `DOWNSTREAM.md`); any change to
`ioscpm/Core/` (21 symlinks that have been flattened into stale copies once already); and
anything at all in `ioscpm` (build 59 has never been compiled by any machine that touched
it, so its entire SwiftUI layer has never been type-checked).

---

## 9. Roadmap

**Nothing in phases 1-3 requires a change to any emulator repo.** That is the property that
makes the second year cheap: five upstream release cadences never gate your shipping.

### Phase 1 — week 1. Use case (B), complete. Zero emulator changes.

**Ships:** the `MachineStore` boundary (in-process), the MBP one-shot adapter, and 7 tools
— `x80_profiles`, `x80_cpm_run`, `x80_dos_run`, `x80_diff_run`, `x80_files` (sandbox
routes), `x80_probe`, `x80_images`. Backends: `cpmemu`, `dosiz`.

Also ships, and is more important than it looks: **`80mcp doctor`**, a table of every
backend — found/not found, version, pinned sha256, ROM/disk pairing, dynamic-library
resolution — exiting nonzero on any problem.

**Acceptance, as literal assertions:**
- `method9.arc` under `cpm-hosted` gives `23 file(s) extracted`, `exit_reason:"jmp_0"`, and
  `B5-TIME.INF` at exactly **1664** bytes.
- The same fixture with `default_mode:"auto"` **and `eol_convert:true`** gives
  `1 file(s) extracted`, `pass:false`, and **no `exit_code` key anywhere in the result**.
  Both halves of the combination are required — measured across the full matrix on cpmemu
  4.8.0, `default_mode:"binary"` gives 23/1664 whatever `eol_convert` says, and
  `"auto"`/`"text"` with `eol_convert:false` also gives 23/1664. Running `cpmemu` with no
  config file at all lands on the corrupting pair, because those are its built-in
  defaults; the synthesized `.cfg` is what prevents it. Assert on the parsed JSON **keys**
  for the `exit_code` half, not on a substring — `fidelity.divergences` legitimately
  contains the words "exit status" in the prose explaining the absence.
- `x80_diff_run` on the same fixture returns `identical:23, differ:0` with
  `normalize:["lowercase_names","pad_to_record"]`, and `identical:2, differ:21` with
  `lowercase_names` alone.
- `dosiz SORT.EXE` round-trips in under 100 ms; `DJ_PRINTF.exe` gives `exit_code:7`.
- Every `inputSchema` in the repo `json.loads` and validates against the 2020-12
  metaschema; the string `"tool_result"` and the string `"server"` appear nowhere as a
  `resultType` or a `cacheScope`.

**Prerequisites:** `cpmemu` and `dosiz` binaries; a `qxDOS` checkout for dosiz's build.

### Phase 2 — weeks 2-3. Interactive Z80. Still zero emulator changes.

**Ships:** the pty adapter for `romwbw_emu`, the VT emulator (xterm dialect), CoW disk
cloning, the four-part isolation recipe, SIGSTOP-on-idle, pager handling, and 12 more tools
— `x80_open`, `x80_session`, `x80_run`, `x80_send`, `x80_recv`, `x80_screen`, `x80_regs`,
`x80_step`, `x80_breakpoints`, `x80_disasm` (Z80 via `ud80`), `x80_monitor`, `x80_trace`
(stderr parser).

Profiles: `cpm22`, `cpm3`, `zsdos`, `zsystem`, `nzcom`. **`z80-bare` is not in this phase**
(§3.6).

**The one non-obvious piece:** everything interactive runs under a pty with stderr on its
own pipe, because `sim>` is tty-only. That single decision buys the entire debugger family.

**Acceptance:** boot `cpm22` and `cpm3` and reach the prompt in under 0.5 s each; set a
breakpoint at 0x0005 under CP/M 3 and catch a BDOS call; `x80_run` on a CP/M 3 `DIR`
returns the complete listing with `pager_answered:1`; `x80_regs` parses
`A=00 BC=001A DE=0080 HL=D008 SP=D7A9 PC=0005` into the typed shape; three concurrent
sessions cost under 0.1 core at idle; a run with a sandbox `XDG_CONFIG_HOME` but no
`HOME` override **fails the test** by loading the developer's NVRAM.

**Also in this phase, because it costs one line:** fix `romwbw_emu/disks/disks.xml:34`.

### Phase 3 — week 4. MP/M. The unclaimed ground. Zero emulator changes.

**Ships:** the mpm2 SSH adapter, `x80_consoles`, and the `console` parameter on
`x80_run`/`x80_send`/`x80_recv`/`x80_screen`/`x80_files`. Profile: `mpm2`.

**Acceptance:** boot to four TMPs; open **four** SSH consoles and label each by its
`MP/M II Console N` banner, with `console_id` not equal to `index`; drive `DIR` on one and
`USERS` on another concurrently and get cleanly separated output; `x80_consoles` reports 4
active of 8 and the seven 48K user banks; the launch never passes port 0 and never uses
`-l`.

**Prerequisites:** `libssh` and `cmake` (skippable if you do not want MP/M); an `mpm2`
section in the `romwbw_disks` catalog; either upstream item 14 or an explicit
`DYLD_LIBRARY_PATH` with a warning in the result.

### Phase 4 — weeks 5-6. The upstream wins, in priority order.

Each is independently shippable and none blocks a tool: upstream items 1 (memory — this is
what promotes `x80_mem_read`/`x80_mem_write` into the tool list), 4, 5, 6, 7, 8, 13, 15, 19.

### Phase 5 — weeks 7-10. x86 hardware and the syscall tier.

**Ships:** `backends/emu88d/` (upstream items 2 and 3), profiles `x86-bare` and `freedos`;
then `x80_trace`'s `resolved` block and `x80_syscall_break` via the `cpmemu_dbg` target
(items 11, 12); then x86 `disasm` with Zydis vendored.

**Budget one experiment first:** run `scripts/build_starter_disk.sh` and boot it headless
under `emu88d` *once* before promising the `freedos` profile. Until that passes, report it
as `ready:false`.

### Phase 6 — dialects and agent docs. **Not "later" for the docs.**

`DRIVING-WITH-AI.md` and `examples/agent/` ship in **phase 1**, not here. What ships here
is validating the VT emulator against `z80cpmw`'s 516-check `TerminalView::cellAt()` corpus
and adding the `z80cpmw`/`ioscpm`/`cpmdroid` dialects as diffs from the core parser.

### The phase-1 documentation deliverable

**`docs/DRIVING-WITH-AI.md`**, mirroring altairsim's structure section for section so a
reader of one recognises the other: starting and registering the server; the profile table;
a **"which profile do I want"** decision tree — *is it just a program and some files? →
`cpm-hosted` or `dos-hosted`. Does it touch a BIOS, a screen, or a boot? → `cpm3` or
`freedos`. Several terminals? → `mpm2`* — the expect loop; the two-tier explanation; and a
**Gotchas chapter that is better than altairsim's because ours is measured rather than
remembered**: no guest exit status anywhere except dosiz; unpaced input is eaten and
truncates output; `default_mode=auto` aborts the run and exits 0; the dosiz argv[0] trap;
nothing has a timeout; a polled guest burns 95% of a core; CR is dropped and the high bit is
stripped on the Z80 console; filenames are lowercased on create; the FCB at 0x5C is not
written on `cpm-hosted`; CP/M 3's DIR paginates; ROM and disk must be paired; MP/M consoles
name themselves and you cannot pick one; `sim>`'s memory commands do not reach a RomWBW
guest.

**`examples/agent/`** — three ready-made working directories, each registerable in place:
- `examples/agent/80un/` — the batch exercise. An archive, a reference output tree, and a
  deliberately truncated `.arc`, so the agent learns `x80_cpm_run`, the file manifest, and
  *why* `normalize:["pad_to_record"]` exists.
- `examples/agent/hello-cpm/` — **deliberately the same exercise altairsim ships**: a buggy
  `HELLO.ASM` assembled inside CP/M 3 with the period toolchain, found with
  `x80_monitor`/`x80_breakpoints`/`x80_step`/`x80_regs`, fixed, reassembled. Same shape,
  different machine. That is the convergence thesis made testable: an agent that did
  altairsim's version should complete ours without reading the docs.
- `examples/agent/mpm-4up/` — the one nobody else can offer. Four consoles, a file-locking
  experiment, and a human invited to `ssh` onto one console while the agent drives the
  others.

### Why the second year is cheap

- **No emulator repo changes for phases 1-3**, so five release cadences never gate you.
- **The MBP adapter boundary** means an upstream improvement swaps an adapter, never a tool.
  Item 6 rewrites how `x80_regs` is served and the schema does not move.
- **The tool list is frozen within a build**, which the spec requires anyway, so adding a
  backend never changes the wire contract — it only fills in more `caps`.
- **One profile string per operating system, never a tool.**
- **`caps` + `doctor` + the MBP conformance suite** make drift visible on the day it happens
  instead of in a bug report six months later.
- **The `MachineStore` boundary** means the daemon is a v2 implementation swap, not a
  rewrite.

---

## 10. Risks

**1. altairsim might simply be enough for the real need.** The honest test: *does the
debugging in question concern the guest program, or our emulator?* If it is the guest
program on generic period hardware, use altairsim and build nothing. If it is "why does
`cpmdroid` render this differently from `z80cpmw`" or "run this package and hand me the
files", only 80mcp helps. Phases 1-3 are scoped to be worth building **even if the answer
is altairsim**. But if the honest answer is "I mostly want to debug 80un-class programs,"
**phase 1 alone is the whole project** and phases 2-5 should not be built.

**2. The most conventional tool in the survey does not work on our main backend.** §6.9.
`sim>`'s `e`/`dm`/`d` operate on a disconnected buffer in RomWBW mode. That is a small fix
(upstream item 1) and it is on the critical path for `read_memory`/`write_memory`, which is
the first thing an agent trained on any other emulator MCP server will reach for. Until it
lands, `caps` must omit `mem_read` on romwbw profiles and the structured error must say
*why* rather than "not supported".

**3. Six backends × 23 tools is 138 cells and most are empty.** `regs`, `step`,
`mem_read` and `breakpoints` are unsupported on `cpmemu` and `mpm2` — and cpmemu's case is
**structural**, not a missing feature: `CPMEmulator` is declared inside a 3429-line
`cpmemu.cc` with no header, so nothing can reach it from outside the process. Mitigation:
`x80_profiles` returns `caps` up front, unsupported ops return a structured error with an
`escalation` block, and `DRIVING-WITH-AI.md` leads with the capability matrix. Residual
risk: agents do not read it.

**4. The VT emulator makes us the family's fourth divergent parser.** §5.6. A screen
assertion that quietly renders under the wrong semantics is a *confidently wrong* answer,
which is worse than no answer. Mitigations are in the schema (`dialect`, `source`,
`lossy[]`) and in validation against the only pixel-verified corpus. Residual risk: the
`lossy[]` list is only as complete as what has been found so far.

**5. No save-state, and replay is not exact for MP/M.** §6.10. The 80% substitute is exact
for CP/M and DOS and **not** for `mpm2`, whose 60 Hz preemption is wall-clock driven —
which is exactly where you would want it when chasing a race. `replay_exact:false` is
honest and is not what the user wants to hear.

**6. Cross-repo pinning rot.** Five repos with independent cadences plus a hard RomWBW
v3.5.1 pin (`emu_validate_rom_hcb` refuses a 3.6.0 ROM; NVRAM checksums silently reset
across a version change). A `backends.toml` nobody updates becomes a lie within months.
Mitigations: `doctor` refuses mismatched pairs rather than letting the CBIOS warning reach
a transcript; weekly CI that builds every pinned SHA and files an issue on drift.
**Residual risk: this is real maintenance work forever, and it is the most likely thing to
be quietly abandoned.**

**7. Batch fidelity green-lights wrong behaviour.** The `cpmemu` FCB-at-0x5C gap is the
concrete instance: 80un's usage-banner path is unreachable, so the harness reports success
on behaviour that differs from real hardware, and only an interactive human would have
caught it. Every result carries a `fidelity` block, but **the list is only as complete as
what has been found so far, and there are certainly more.** The discipline the docs must
state: **fast inner loop on the hosted tier, acceptance gate on `cpm22`/`cpm3`.**

**8. Licensing.** §11.

**9. Scope creep into being a general DOS MCP server.** Spice86 has 65+ tools and six
DOSBox-X servers exist; each is a feature list to be envious of. Resisting is a discipline
problem, not a technical one. The framing that holds the line: **our x86 side exists to
debug our emulators and run our packages, and nothing else.**

**10. This is a lot of surface for one maintainer.** Twenty-three tools, six backends, a VT
emulator, a disk catalog client and a new C++ runner. The phasing is the mitigation — phase
1 is 7 tools, 2 backends, zero emulator changes, and is genuinely useful alone — but the
failure mode is stopping at phase 5 with half the caps matrix empty and no
`DRIVING-WITH-AI.md`, which is **worse for an agent than a smaller server that is fully
documented.**

**11. `mpm2` at four consoles has never been run**, and neither has `freedos`, and neither
has `z80-bare`. Two consoles over SSH were demonstrated and the guest self-identifies, so
the mechanism is proven; four is bookkeeping. But this document does not claim it works.

---

## 11. Licensing and distribution

**Code, outbound.** `80mcp` should be GPL-3.0, matching every other repo in the family
(`romwbw_emu`, `cpmemu`, `ioscpm`, `cpmdroid`, `z80cpmw`, `romwbw_disks`, `dosiz`, `qxDOS`,
`80un` — all verified GPL-3.0). Since 80mcp never contains emulator source (§5.1) and execs
built binaries, the licence question is simple in the common case.

**Code, inbound from altairsim.** altairsim is MIT (© Patrick Linstruth), and **MIT → GPL-3
vendoring is fine**. If you ever want a piece of it, you may take it. **The reverse is not
true**: contributing your GPL-3 code into MIT altairsim requires relicensing your own work
first, which is why §1.3 says do not upstream. SDL3 is zlib and optional — a full altairsim
build plus 61/61 tests ran with it absent.

**Media is the real constraint, not code.**
- CP/M 2.2 is **genuinely redistributable** — the 2022 Bryan Sparks grant, cited in
  altairsim's `docs/sources.md:194`.
- CP/M 3 images are murkier. altairsim's `examples/dualide/README.md` calls its image
  "DR-supplied CP/M 3, hobbyist/educational" and its `docs/DRIVING-WITH-AI.md:253` says
  flatly *"the `.dsk` files are not redistributable."* **Depend on altairsim as a tool you
  run, never as media you ship.**
- `mpm2`'s default build ships **original Digital Research MP/M II binaries** — Caldera-era
  abandonware on the same footing as the `Mixed`/`Abandonware` entries `romwbw_disks`
  already carries. But a pip wheel that redistributes a bootable MP/M image is a different
  posture from a catalog that links to one. **Prefer `--tree=src` builds** (`uplm80`,
  `um80`, `ul80` compile the whole OS from DRI source) and mark them separately in the
  catalog.
- Third-party ROMs carry live copyrights — altairsim's `docs/roms.md:116` records the
  judgement explicitly: *"'freely circulating' is not the same as 'licensed to
  redistribute' … This is Patrick's call … reversible to a path"* — with e.g. CUTER 1.3,
  © 1977 Software Technology Corp.

**The distribution rule, from all of the above:**

> **80mcp never bundles a disk image or a ROM.** `x80_images` fetches from the sha256-pinned
> `romwbw_disks` catalog on an explicit `allow_fetch:true`, surfaces the `licence` field
> **verbatim** in the result, and is the only tool in the server with
> `openWorldHint:true`. No other tool may trigger a download.

**Security posture, learned from altairsim's two measured issues (§1.3):** `x80_monitor`
never shells out to the host; host-side paths given to `export_to`, `host_path` and
`file_path` are the caller's responsibility and are documented as such, but the *guest* is
always confined to its sandbox; and no subprocess ever inherits the JSON-RPC stdout — the
backends' stderr is a separate pipe by construction (§5.3).

---

## Appendix A — measured-facts index

Every number in this document, with where it came from. The raw transcripts are
committed under [`evidence/`](evidence/) — run labels `t1`..`t6` below map to the
directories there, and the pty drivers are `evidence/t6*.py`.

| fact | value | where |
|---|---|---|
| CP/M 2.2 boot to prompt, romwbw_emu | under 0.1 s (`real 0.12` at `sleep 0.1`) | t1 |
| CP/M 2.2 batch, stdout / stderr | 2039 B / 644 B, zero crosstalk, RC 0 | t1 |
| idle romwbw_emu session, 4 s wall | 0.03 s CPU (it blocks, it does not spin) | t1 |
| unpaced `DIR` | lost to romldr autoboot; output ends `A>^C` | t1/nb.txt |
| NVRAM leak under sandbox XDG + `--no-config` | `Loaded NVRAM setting 'C' from /Users/wohl/.config/romwbw_emu/nvram` | t1/stderr2.txt |
| CP/M 3 banner | `CP/M v3.0 [BANKED] for HBIOS v3.5.1`, `60K TPA` | t2 |
| CP/M 3 pager | `Press RETURN to Continue` after 21 lines | t2/stdout.txt |
| 80un on `method9.arc`, binary mode | 23/23 in 0.63 s / 99% CPU | t3 |
| same, `default_mode=auto` | 1/23, `Error`, truncated file, exit 0 | t3/auto_stdout.txt |
| `B5-TIME.INF` guest / python / auto | 1664 / 1537 / 1437 bytes | t3 |
| normalization needed | `lowercase_names` + `pad_to_record`; 0 content diffs, pad byte 0x1A | t3 |
| dosiz djgpp suite | 42 passed, 0 failed, 2.729 s | dosiz repo |
| `DJ_PRINTF.exe` | rc 7, `int=42 str=hello / flt=3.142 hex=0xDEADBEEF / dj-printf=ok` | dosiz repo |
| sandboxed `SORT.EXE` | RC 0 in 0.022 s | t4 |
| dosiz absolute argv[0] | rc 102, `C:\…\DJ_PRINTF.EXE: can't open` | dosiz repo |
| dosiz unconditional stderr | 2 Crynwr lines, every run | t4/err.txt |
| MP/M II boot | `Nmb of consoles = 4`, 7 × 48K user banks, under 5 s | t5 |
| MP/M `-t 9` instruction count | 1,237,927 (paced to a 60 Hz tick) | t5/cmd.txt |
| MP/M `-l` console attribution | 3 prompts for 4 consoles; DIR at `1A>` answered `Directory for User 3:` | t5/cmd.txt |
| `mpm2_emu` without `DYLD_LIBRARY_PATH` | RC 134, dyld failure | otool -L |
| pty boot to `A>` | 0.24 s | t6d.py |
| `sim>` on stderr after 0x05 | 0.11 s | t6d.py |
| breakpoint hit at 0x0005 | 0.06 s | t6d.py |
| `sim> d`/`dm` vs the guest | guest unaffected after smashing 0x0000-0x0007 | t6g.py |
| altairsim build + tests | exit 0; 61/61 ctest; 214,254 checks, 0 failed | altairsim |
| altairsim tool count | 31 live (docs say 19) | live `tools/list` |
| altairsim CP/M 2.2 boot | 0.04 s, `stopped:"match"` | live MCP |
| altairsim CP/M 3 idle heuristic | never fires; 20.00 s timeout at the `A>` prompt | live MCP |
| altairsim protocol | `2024-11-05`, five methods, no annotations | live `initialize` |
| altairsim `monitor {!…}` | executes host shell, corrupts JSON-RPC stdout | live MCP |
| `ud80` present | `/Users/wohl/Library/Python/3.14/bin/ud80` | `which` |
| `romwbw_emu --help` has no `--start` | v1.38 option list | `--help` |

## Appendix B — the six things every design in the round got wrong, so they are not repeated

1. `XDG_CONFIG_HOME` + `--no-config` is **not** sufficient isolation. It is four things
   (§5.4, Invariant 1).
2. `printer =` / `aux_output =` empty emits a warning every run; `/dev/null` silently
   destroys the LST:/PUN: streams. Route to files and report them (§5.5).
3. MP/M consoles cannot be requested; they are announced by the guest and assigned
   highest-first (§3.4).
4. `romwbw_emu --start=ADDR` does not exist (§3.6).
5. `sim>`'s `e`/`dm`/`d` do not reach a RomWBW guest's memory (§6.9).
6. `resultType:"tool_result"` and `cacheScope:"server"` are not legal values. They are
   `"complete"` and `"public"`/`"private"` (§4.1).
