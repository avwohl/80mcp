# Driving 80mcp with an AI agent

This is the operating manual for an agent, not a tour for a human. It mirrors
[altairsim's `docs/DRIVING-WITH-AI.md`](https://github.com/deltecent/altairsim)
section for section, so that a reader of one recognises the other. Where a
number appears here it was measured on a real run of this server against a real
backend; where something has not been measured this file says so.

**This build is phase 1.** Seven batch tools over two backends. The interactive
Z80 tools (`x80_open`, `x80_run`, `x80_step`, `x80_regs`, …) and the MP/M tools
are *not registered* — not stubbed, not erroring, absent. A tool that is listed
and always fails is a lie in `tools/list` (SPEC.md 4.7). Everything below is
about the seven that exist.

---

## Contents

1. [Starting and registering the server](#1-starting-and-registering-the-server)
2. [Call `x80_profiles` first](#2-call-x80_profiles-first)
3. [The two tiers: hosted and hardware](#3-the-two-tiers-hosted-and-hardware)
4. [Which profile do I want](#4-which-profile-do-i-want)
5. [The seven tools](#5-the-seven-tools)
6. [Asserting on a run](#6-asserting-on-a-run-there-is-no-expect-loop-yet)
7. [Gotchas](#7-gotchas) — the chapter to read before writing your first call
8. [Reading an error](#8-reading-an-error)
9. [A complete session](#9-a-complete-session)

---

## 1. Starting and registering the server

```
pip install -e .
80mcp doctor        # always run this first
80mcp               # speak MCP JSON-RPC on stdio
```

`80mcp` frames **newline-delimited JSON** on stdin/stdout. One compact JSON
value, one newline. Nothing else is ever written to fd 1: the server moves the
real stdout aside at startup and points `sys.stdout` at stderr, so a chatty
emulator cannot land in the middle of a frame.

Register it the way any stdio MCP server is registered:

```json
{"mcpServers": {"80mcp": {"command": "80mcp", "args": []}}}
```

### Both protocol revisions work

The server speaks MCP **2026-07-28** natively and still accepts the legacy
`initialize` handshake. You do not need to know which one your client uses, but
the results differ in one visible way, so here is the ground truth, measured
over two separate subprocesses:

| you send | you get back |
|---|---|
| `initialize` with `protocolVersion: "2025-06-18"` | `protocolVersion: "2025-06-18"`; `tools/list` returns `{tools}` and nothing else; `tools/call` returns `{content, structuredContent, isError}` |
| `server/discover` (no handshake) | `protocolVersions: ["2026-07-28","2025-11-25","2025-06-18"]`; `tools/list` also carries `resultType`, `ttlMs: 86400000`, `cacheScope` |

The **tool list is identical and in the same order on both**:

```
x80_profiles  x80_cpm_run  x80_dos_run  x80_diff_run  x80_files  x80_probe  x80_images
```

On the 2026 revision, put the version in `_meta` on each call:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
  "name":"x80_profiles","arguments":{},
  "_meta":{"protocolVersion":"2026-07-28"}}}
```

### Backends are not vendored

`cpmemu` and `dosiz` are found at runtime, in this order: an explicit
`$EIGHTYMCP_CPMEMU` / `$EIGHTYMCP_DOSIZ`, the config file, `$PATH`, then a
sibling checkout next to this repo. A backend that is absent is **never** a
crash. It is a profile with `ready:false` and a `blocked_by` string you can act
on:

```
dosiz was not found on PATH, in EIGHTYMCP_DOSIZ, in the config file, or in a
sibling checkout; clone and build avwohl/qxDOS
```

Config file, all keys optional, at `$EIGHTYMCP_CONFIG` or
`$XDG_CONFIG_HOME/80mcp/config.json`:

```json
{
  "backends": {"dosiz": {"path": "/opt/dosiz", "env": {"DYLD_LIBRARY_PATH": "/opt/lib"}}},
  "image_dirs": ["/srv/romwbw"],
  "catalog_dirs": ["/srv/romwbw_disks/catalog/v0"],
  "romwbw_version": "3.5.1"
}
```

A malformed config is reported as a `problem`, never silently ignored — "the
server ignored my config" is the hardest support question in this domain.

---

## 2. Call `x80_profiles` first

Not as etiquette. Capabilities differ enormously between backends, and a run
launched at a profile whose binary is missing wastes a turn. `x80_profiles` is
the only `readOnlyHint:true` tool in phase 1 and it costs milliseconds.

```json
{"name":"x80_profiles","arguments":{"family":"z80","only_ready":true}}
```

What comes back per profile: `backend`, `backend_version`, `binary_path`, `os`,
`caps`, `consoles`, `images.required[]` with `present`, `fidelity`, `ready`,
and `blocked_by`. Plus `external_servers_recommended`, which names altairsim
for generic CP/M-on-S-100 and Spice86 for general DOS — this server is
deliberately narrow and will tell you when the job belongs elsewhere.

**Read `fidelity.divergences`.** It is carried per profile rather than buried
in documentation, because every one of those strings is a way your assertions
can be wrong. `cpm-hosted` carries five, verbatim from a live call:

```
setup_command_line() never writes the FCB at 0x5C, so a program testing
fcb(1)=' ' for its usage banner takes the wrong branch.

Created filenames are lowercased on the host: TEST.TXT becomes test.txt,
B5-TIME.INF becomes b5-time.inf. guest_name and host_name are reported
separately for that reason.

There is no exit status. cpmemu exit(0)s on every normal path including its
runaway watchdog, so x80_cpm_run has no exit_code field at all.

default_mode=auto never resolves on write: the same 23-member ARC extracted
1 of 23, printed 'Error', truncated the one file it wrote to 1437 bytes
against 1664, and exited 0 (measured).

The guest writes whole 128-byte records, so a file is padded to the next
record boundary with 0x1A against a host reference's exact size; compare
under normalize:['pad_to_record'].
```

Each of those has a Gotchas entry below.

### The profile table in this build

Eleven profiles are described; **two are runnable in phase 1**.

| profile | family | tier | backend | phase 1 |
|---|---|---|---|---|
| `cpm-hosted` | z80 | hosted | cpmemu | **runnable** |
| `dos-hosted` | x86 | hosted | dosiz | **runnable** |
| `cpm22` `cpm3` `zsdos` `zsystem` `nzcom` | z80 | hardware | romwbw_emu | phase 2 |
| `z80-bare` | z80 | hardware | romwbw_emu | phase 2, and blocked on its own account |
| `mpm2` | z80 | hardware | mpm2_emu | phase 3 |
| `freedos` `x86-bare` | x86 | hardware | emu88d | phase 5 |

The blocked ones report why, one reason per line. A phase-2 profile says so
even when the binary is present:

```
cpm3 blocked:
  - the romwbw_emu adapter ships in phase 2; this build is phase 1 and ships
    the one-shot adapter only (SPEC.md 9)
```

---

## 3. The two tiers: hosted and hardware

This is the single idea that makes the profile list make sense, and it is the
thing no other emulator MCP server in the survey has.

**Hosted** (`cpm-hosted`, `dos-hosted`) translates the operating system's
**API** to the host filesystem. There is no disk image, no boot, no BIOS. A
CP/M `BDOS 15 Open` becomes an `open(2)`; an `INT 21h AH=3Dh` becomes an
`open(2)`. Runs start in milliseconds and files pass straight through. This is
what "here is a package, run it, give me the files back" wants.

**Hardware** (`cpm22`, `cpm3`, `mpm2`, `freedos`, …) boots the **real
operating system on an emulated machine**. A real BIOS, real disk images, a
real boot sequence, real timing. This is what "why does the screen render
differently" and "break on the BDOS entry point" want.

Hosted is faster and file-oriented; hardware is faithful. Choosing hosted when
you needed hardware shows up as a `fidelity.divergences` entry biting you —
that is exactly why the block is in every result and not in a footnote.

**Phase 1 ships only the hosted tier.** Everything hardware is phase 2 or later.

---

## 4. Which profile do I want

```
Is it just a program and some files, and you want the files back?
  z80/CP/M ......................... cpm-hosted     (ships now)
  x86/DOS .......................... dos-hosted     (ships now)

Does it touch a BIOS, a screen, a boot sequence, or the hardware?
  z80 .............................. cpm3 / cpm22   (phase 2)
  x86 .............................. freedos        (phase 5)

Several terminals at once?
  .................................. mpm2           (phase 3)

Don't know what it needs?
  .................................. x80_probe      (ships now)

Generic CP/M on Altair or S-100 hardware, right now?
  .................................. use altairsim, not this server
```

If you cannot answer the first question, do not guess. `x80_probe` runs the
program on the cheapest profile and reports what it actually needed, from the
syscalls it made rather than from a screen read, and hands you a literal next
call.

---

## 5. The seven tools

| tool | what it is for | destructive | openWorld |
|---|---|---|---|
| `x80_profiles` | what this installation can run. Call it first. | no | no |
| `x80_cpm_run` | **the batch verb.** Run a CP/M package, get the files back. | no | no |
| `x80_dos_run` | the same for DOS, and it *does* have an exit code. | no | no |
| `x80_diff_run` | run the same input through the guest and a host reference, compare bytes. | no | no |
| `x80_files` | move files in and out of a kept sandbox, list what was created. | **yes** | no |
| `x80_probe` | "what OS does this actually need?" — evidence, plus the next call. | no | no |
| `x80_images` | fetch pinned disk images. **The only tool that touches the network.** | no | **yes** |

`x80_files` is the only `destructiveHint:true` tool and `x80_images` the only
`openWorldHint:true` tool. The MCP defaults for both hints are the dangerous
value, so every other tool sets them explicitly to false.

### `x80_cpm_run`

The shape, with the arguments that matter:

```json
{"name":"x80_cpm_run","arguments":{
  "profile":"cpm-hosted",
  "cpu":"z80",
  "program":"/path/to/80un.com",
  "args":["M9.ARC"],
  "files_in":[{"guest_name":"M9.ARC","host_path":"/path/to/method9.arc","mode":"binary"}],
  "default_mode":"binary",
  "timeout_ms":10000,
  "collect":{"return_content":"inline_if_under_kb","max_kb":64},
  "assert":{"stdout_contains":["23 file(s) extracted"],
            "files_created":["TIME2.ASM"],
            "no_unimplemented_bdos":true},
  "keep_sandbox":false}}
```

Out: `pass`, `exit_reason`, `wall_ms`, `stdout`, `stderr`, `files_out[]`,
`list_output`, `punch_output`, `assertions[]`, `diagnostics`, `warnings`,
`fidelity`, `sandbox`.

**There is no `exit_code`.** See Gotcha 1.

`exit_reason` is parsed from the backend's stable stderr sentinels and is one
of `jmp_0`, `bdos_0`, `wboot`, `instruction_limit`, `eof_giveup`, `ctrl_c`,
`timeout`, `boot_timeout`, `emulator_error`. A clean 80un run gives `jmp_0`.

`diagnostics.unimplemented_bdos` is the cheapest signal for *"this package
needs a richer CP/M than this one"*. An empty list means the hosted BDOS
surface was enough.

`list_output` and `punch_output` are the LST: and PUN: byte streams. They are
routed to real files inside the sandbox and reported with byte counts, because
routing them to `/dev/null` silently destroys the output of any CP/M utility
that writes its report to the list device, and leaving them empty prints a
warning on every single run.

### `x80_dos_run`

Same shape, plus `expect_exit_code`, `env`, and a real `exit_code`:

```json
{"pass": true, "exit_code": 0, "exit_code_meaning": "guest_exit", "wall_ms": 27,
 "diagnostics": {"unimplemented_int21": [], "stderr_filtered": [
   "dosiz: ethernet/slirp backend unavailable; INT 0x60 packet driver will accept guest calls but RX/TX will no-op.",
   "dosiz: Crynwr pktdrv installed at INT 60h, stub at 0060:0000"]}}
```

Those two stderr lines print on **every single** dosiz run. The server strips
exactly those two known strings and records in `stderr_filtered` that it did,
rather than swallowing stderr silently.

`exit_code_meaning` is `guest_exit`, `loader_failure`, `pm_fault`, `timeout`,
or `none_available`. It exists because rc 1 is ambiguous — see Gotcha 5.

### `x80_diff_run`

Runs the guest and a host reference on the same inputs and compares. The
reference `argv` gets `{input}` and `{outdir}` substituted; a reference argv
with no `{outdir}` is refused rather than compared against a directory nobody
wrote to.

```json
{"name":"x80_diff_run","arguments":{
  "guest":{"profile":"cpm-hosted","program":"…/80un.com","args":["M9.ARC"],"default_mode":"binary"},
  "reference":{"argv":["python3","-m","un80.cli","{input}","-o","{outdir}"],
               "env":{"PYTHONPATH":"src"},"cwd":"…/80un"},
  "inputs":[{"guest_name":"M9.ARC","host_path":"…/method9.arc","mode":"binary"}],
  "normalize":["lowercase_names","pad_to_record"],
  "compare":"bytes","timeout_ms":60000}}
```

`normalize` is an enum array, never a boolean. See Gotcha 4, which is the whole
reason this tool exists in this shape.

### `x80_files`

Five ops. **Three ship in phase 1**: `list`, `to_guest`, `from_guest`.
`handles` and `resolve` are phase 5 and return a structured `unsupported` you
can act on rather than a bare failure.

It works on a **kept sandbox**: pass `keep_sandbox:true` to a batch verb, take
the `sandbox` path out of the result, and hand it to `x80_files`. The tool
refuses to write into a directory that does not carry this server's marker
file, so it can only touch trees a batch verb produced.

`op:"list"` with `since:"start"` reports the run's **true** created set, taken
from the sandbox manifest — not from `files_out`. That matters: a
`collect.exclude` shapes what `files_out` reports and must not rewrite history.
Measured on one 23-member extraction with `collect.exclude:["*.INS"]`,
`files_out` had 4 entries and `x80_files{op:"list", since:"start"}` had 23.

### `x80_probe`

```json
{"name":"x80_probe","arguments":{"program":"…/80un.com","args":["M9.ARC"],
  "files_in":[{"guest_name":"M9.ARC","host_path":"…/method9.arc"}]}}
```

```json
{"ran_on":"cpm-hosted","verdict":"sufficient","unimplemented":[],
 "evidence":["exit_reason jmp_0 after 631 ms on cpm-hosted",
             "first line of output: 80UN - CP/M Archive Unpacker v2.3",
             "the run completed on cpm-hosted with no unimplemented syscalls"],
 "recommended_profile":"cpm-hosted",
 "escalation":{"tool":"x80_cpm_run","arguments":{"profile":"cpm-hosted","program":"…","args":["M9.ARC"]}}}
```

`escalation` is a literal call. Make it. That is the difference between an agent
that reruns on a richer profile and one that gives up.

### `x80_images`

`dry_run` defaults to `true` and `allow_fetch` defaults to `false`. **Both must
be flipped** for anything to be downloaded, and no other tool may trigger a
download implicitly. `licence` is surfaced verbatim from the catalog, which
already carries `Mixed` and `Abandonware` values — read it before redistributing
anything.

---

## 6. Asserting on a run (there is no expect loop yet)

altairsim's `run{from, input, until, timeout_ms, max_steps}` expect loop is
**phase 2** here. It arrives with `x80_open` and the pty adapter, and its
argument shape is deliberately borrowed name for name so the muscle memory
transfers. It does not exist in this build.

What phase 1 gives you instead is a declarative `assert` block evaluated after
the run, which for a batch package is the better tool anyway:

| assertion | checks |
|---|---|
| `stdout_contains: [...]` | each string appears in stdout |
| `stdout_not_contains: [...]` | none of them do |
| `files_created: [...]` | each guest name is in the manifest |
| `file_sha256: {name: hex}` | exact content — read Gotcha 4 first |
| `no_unimplemented_bdos` / `no_unimplemented_int21` | the guest never hit a syscall the backend lacks |
| `expect_exit_code: N` | DOS only |

Every assertion comes back in `assertions[]` with `kind`, `value`, `ok`, and on
failure an `actual`. A failed `stdout_contains` reports the **closest stdout
line** as `actual`, by similarity, so the diagnosis is in the result:

```json
{"kind":"stdout_contains","value":"23 file(s) extracted","ok":false,
 "actual":"1 file(s) extracted"}
```

`pass` is the AND of every assertion **and** the run having completed. That
second clause is not decoration: an empty AND is true, so a call with no
`assert` block that timed out would otherwise report `pass:true`. A `timeout`,
`boot_timeout` or `emulator_error` vetoes `pass` and writes itself into
`warnings`.

---

## 7. Gotchas

Every item here was measured on this machine against a real backend. This is
the chapter to read before writing your first call.

### 1. There is no exit code on CP/M. Assert on stdout **plus** the file manifest.

`x80_cpm_run` has no `exit_code` field. Not a nullable one, not one guarded by
an `if`. CP/M has no exit-status concept; `cpmemu` `exit(0)`s on every normal
path including its runaway watchdog; `romwbw_emu` always returns 0.

The proof, measured twice on the same 23-member archive:

| run | stdout ends | files extracted | `B5-TIME.INF` | process rc | stderr |
|---|---|---|---|---|---|
| correct | `23 file(s) extracted` | 23 | 1664 bytes | 0 | `Program exit via JMP 0` |
| corrupted | `1 file(s) extracted` | 1 | 1437 bytes | 0 | `Program exit via JMP 0` |

A 96%-failed run is **indistinguishable from success at the process level**.
Any CP/M tool schema with a bare `exit_code` is lying to you.

So: assert on stdout, and assert on the manifest, and prefer both.

```json
"assert":{"stdout_contains":["23 file(s) extracted"],
          "stdout_not_contains":["Error","Cannot open","Invalid"],
          "files_created":["TIME2.ASM","ZTIM-S3.CPM"],
          "no_unimplemented_bdos":true}
```

Stdout alone is not enough either. A truncated archive gave, measured:

```
pass: false   exit_reason: timeout   files_out: 15
stdout: ...  B5C-KPRO.INS OK / B5C-LEG2.INS OK / B5C-MORE.INS
```

Fifteen cheerful `OK` lines and then nothing, because the guest blocked forever
reading past the end of a file. Stdout looked healthy right up to the cut.
`exit_reason` and the file count are what caught it.

### 2. `default_mode` must be `binary`.

It already defaults to `binary` and you should not change it. The full matrix,
measured against cpmemu 4.8.0 on the same 23-member fixture:

```
default_mode   eol_convert   result
binary         false         23 file(s), b5-time.inf 1664   <- the default
binary         true          23 file(s), b5-time.inf 1664
auto           false         23 file(s), b5-time.inf 1664
auto           true           1 file(s), b5-time.inf 1437   <- silent, total
text           true           1 file(s), b5-time.inf 1437
(no cfg at all)               1 file(s), b5-time.inf 1437
```

Two things follow. `binary` protects you on its own, whatever `eol_convert`
says — that is why it is the default and why you should leave it. And running
`cpmemu` yourself with no config file lands you on the corrupting combination,
because `auto` + conversion is its built-in default; the synthesized `.cfg` is
the whole reason reaching past this tool to the binary is a bad idea.

There is no CLI flag for this in `cpmemu`; only a config file can set it, and
this server synthesizes that config for you. If you do pass `auto`, the result
carries a warning naming the exact cause:

```
default_mode 'auto' with eol_convert true is the measured corruption: BDOS 22
Make keeps MODE_AUTO (cpmemu.cc:2115) and write_with_conversion takes the text
branch for it (cpmemu.cc:952-964), truncating every written file at its first
0x1A.
```

Set `mode:"text"` on individual `files_in` entries when you genuinely want a
text file translated on the way in. Leave the global default alone.

### 3. `cpmemu` lowercases created filenames.

The guest asks CP/M to create `B5-TIME.INF`. What lands on the host is
`b5-time.inf`. This is why every manifest row carries **two** names:

```json
{"guest_name":"B5-TIME.INF","host_name":"b5-time.inf","bytes":1664,"sha256":"b811d072…"}
```

Write your `assert.files_created` and your `x80_files{names}` against
`guest_name` — the uppercase 8.3 name the guest used. Anything that touches the
host filesystem directly must use `host_name`. A caller writing case-sensitive
assertions against host names will be wrong on every file.

### 4. `normalize` is an enum, and `pad_to_record` is required for LBR and ARC.

`normalize` is an array of `lowercase_names`, `pad_to_record`, `strip_cpm_eof`,
`crlf_to_lf`. It is not a boolean. Here is why, measured three times on the
same 23-member archive against the same Python reference:

| `normalize` | files_compared | identical | differ | only-in lines |
|---|---|---|---|---|
| `[]` | 0 | 0 | 0 | **46** — every member on both sides |
| `["lowercase_names"]` | 23 | **2** | **21** | 0 |
| `["lowercase_names","pad_to_record"]` | 23 | **23** | **0** | 0 |

With no normalization the two trees do not appear to have a single file in
common, because the guest wrote 23 lowercase names and the reference wrote 23
uppercase ones. Name-matching alone gets you 2 of 23.

The remaining 21 differ because **CP/M writes whole 128-byte records.** The
guest's `b5-time.inf` is 1664 bytes; the reference's is 1537; and
`ceil(1537/128)*128 = 1664`. The tail is `0x1A` padding. That is not corruption
and it is not a bug in either side — it is the file format. Every LBR and ARC
member has it. `pad_to_record` pads the shorter side to the next boundary
before comparing, and the tool tells you so in the diff note:

```
b5-time.inf: guest 1664 vs reference 1537
  1664 == ceil(1537/128)*128: one side wrote whole CP/M records; adding
  "pad_to_record" to normalize makes these two identical
```

Normalization is applied to the **reported** sha256 and content only, never to
the file on disk.

**Corollary, and this is the trap:** `assert.file_sha256` against a golden hash
is only meaningful if you know which convention the golden was made under. A
hash taken from a host-side reference will not match a guest-written file of
the same content. Use `x80_diff_run` with a declared `normalize`, or hash the
guest side once and pin that.

### 5. `dosiz` must be given a **relative** program name from the right cwd.

This is the sharpest trap in the DOS half, and the server exists partly to make
it impossible. `dosiz` builds `argv[0]` as `"C:"` plus the uppercased,
backslashed host path, and DJGPP's go32 stub reopens that string to load its
COFF payload. Measured, all three, same binary:

```
$ dosiz /abs/path/to/tests/DJ_PRINTF.exe
C:\PRIVATE\TMP\...\DJ_PRINTF.EXE: can't open          rc 102

$ cd /somewhere/else && dosiz DJ_PRINTF.exe
                                                      rc 1     <-- loader failure

$ cd /abs/path/to/tests && dosiz DJ_PRINTF.exe
int=42 str=hello ...                                  rc 7     <-- the guest's own code
```

Note the middle row. **rc 1 is ambiguous** between "the guest exited 1" and
"dosiz could not load the program", which is exactly why `x80_dos_run` reports
`exit_code_meaning` alongside `exit_code`, disambiguated from stderr.

`x80_dos_run` always copies the program into the sandbox, `chdir`s there, and
passes the bare name. You give it an absolute `program` path and it does the
right thing. Do not try to shortcut this by shelling out to `dosiz` yourself.

### 6. Nothing in this family has a timeout of its own.

`dosiz`'s run loop has no instruction counter and no wall clock — a 2-byte
`EB FE` spin ran until SIGKILL. `cpmemu`'s only guard is a hardcoded
9e9-instruction watchdog that does not fire on a guest blocked on an idle pipe.
`romwbw_emu`'s is 10e9.

Every launch here is externally deadlined and killed **by process group**, so
a guest that spawns children cannot leave orphans. Verified on both backends
with a 3-byte `JMP $` and a 2-byte `EB FE`: killed at 1002 ms and 1202 ms
against 1000 ms and 1200 ms budgets, no orphan processes.

`timeout_ms` defaults to 10000 for `x80_cpm_run`, 20000 for `x80_dos_run`,
60000 for `x80_diff_run`, and caps at 600000. Set it deliberately. A run that
hits the deadline comes back `exit_reason:"timeout"` with `pass:false` and
everything that was produced up to the cut — that is a result, not an error.

### 7. `files_out` is shaped by `collect`; the sandbox manifest is not.

`collect.exclude`, `collect.return_content` and `collect.max_kb` change what
the **result** reports. They do not change what the run created. If you need
the true created set, keep the sandbox and ask:

```json
{"name":"x80_files","arguments":{"op":"list","sandbox":"/tmp/80mcp-…","since":"start"}}
```

Measured: `collect.exclude:["*.INS"]` gave `files_out` with 4 entries;
`x80_files{op:"list", since:"start"}` on the same sandbox gave 23.

### 8. Sandboxes are temporary, and are yours only if you ask.

`keep_sandbox` defaults to `false` and the tree is removed when the call
returns. With `keep_sandbox:true` the result carries a `sandbox` path, the tree
survives, and `80mcp` reaps it **24 hours** after the run — measured from the
run, not from the last read, so an `x80_files` call does not extend its life.

Only trees carrying this server's `.80mcp-sandbox.json` marker are ever reaped,
and only those are writable by `x80_files`.

### 9. `handles` and `resolve` are phase 5, and say so properly.

```json
{"error":"unsupported","op":"files:resolve","backend":"cpmemu",
 "reason":"SPEC.md 6.4: `handles` and `resolve` are phase 5. Both read CPMEmulator::open_files (src/cpmemu.cc:346), which is declared inside a 3429-line .cc with no header, so nothing outside the process can reach it until the backend speaks MBP on fd 3",
 "escalation":{"tool":"x80_files","arguments":{"op":"list","sandbox":"/tmp/80mcp-…"}}}
```

The `via:"hostfile"` and `via:"image"` routes answer the same way. Take the
`escalation` — it is a call that works.

### 10. Hardware profiles are not in this build.

`cpm3` will not boot here, however much the profile table describes it. It
reports `blocked_by` naming phase 2. Do not write a fallback that retries a
CP/M 3 run on failure; there is nothing to fall back to yet. If you need real
CP/M hardware **today**, `x80_profiles` hands you the altairsim URL for exactly
that reason.

### 11. `x80_dos_run` has no output-redirection argument.

SPEC.md 7.5 shows `dosiz SORT.EXE < IN.TXT > OUT.TXT` with `OUT.TXT` in
`files_out`. The phase-1 schema has no argument that can request that
redirection. `stdin` is fed to the guest and the guest's stdout comes back as
`stdout`; a program that writes a file writes it into the sandbox and shows up
in `files_out` normally. Measured:

```json
{"stdin":"pear\r\nbanana\r\napple\r\ncherry\r\n"}
→ {"pass":true,"exit_code":0,"wall_ms":27,"files_out":[],
   "stdout":"apple\r\nbanana\r\ncherry\r\npear\r\n"}
```

If you need a redirect, have the guest program take a filename argument, or
wait for the phase-2 interactive tools.

### 12. Two things the spec measured that this build reports differently.

Reported rather than bent, because a doc that quietly matches the spec is worse
than one that says where reality moved:

- SPEC.md 9's acceptance line for `default_mode:"auto"` does not reproduce on
  cpmemu 4.8.0 with `auto` alone: that gives 23 files and `pass:true`. The
  measured 1-of-23 corruption needs `default_mode:"auto"` **and**
  `eol_convert:true`. The `auto` warning fires either way.
- `strip_cpm_eof` on the guest's 1664-byte `B5-TIME.INF` reports **1472**, not
  the reference's 1537, because the Python reference's own file ends in 65
  `0x1A` bytes and stripping all trailing `0x1A` goes past where the reference
  stopped. Neither layer is wrong; `strip_cpm_eof` is simply the wrong
  normalization for this comparison, and `pad_to_record` is the right one.

---

## 8. Reading an error

Two channels, and they mean different things.

**A JSON-RPC error** means the call was malformed — unknown tool, schema
violation, bad argument. Fix the call.

**`isError: true` in a result** means the call was well-formed and the thing it
asked for could not be done. These are structured and always carry enough to
act on:

```json
{"error":"unsupported","op":"…","backend":"cpmemu","reason":"…","escalation":{"tool":"…","arguments":{…}}}
```

A run that failed its assertions is **neither**. It is a normal result with
`pass:false`, and everything the run produced is in it. Do not treat
`pass:false` as an error; read `assertions[]`, `exit_reason` and `warnings`.

`warnings[]` is where the server puts things that did not stop the run but will
change how you read it — an `auto` `default_mode`, a deadline veto, an unpaced
stdin. Read it every time.

---

## 9. A complete session

A worked, runnable end-to-end exercise lives in
[`examples/agent/80un/`](examples/agent/80un/): unpack a 23-member 1986 CP/M
archive, prove the extraction is byte-correct against a Python implementation
of the same algorithm, and then break it four different ways on purpose so the
failure signatures are familiar before you meet them in anger.

Run it with:

```
python3 examples/agent/80un/run.py --un80 /path/to/80un
```

The short version, in six calls:

```
1. x80_profiles {"family":"z80","only_ready":true}
     → cpm-hosted ready, backend cpmemu 4.8.0, and five fidelity divergences

2. x80_probe {"program":"80un.com","args":["M9.ARC"],"files_in":[…]}
     → verdict "sufficient", recommended cpm-hosted, escalation = the next call

3. x80_cpm_run {…, "default_mode":"binary", "assert":{…}, "keep_sandbox":true}
     → pass true, exit_reason jmp_0, 667 ms, 23 files, B5-TIME.INF at 1664 bytes

4. x80_files {"op":"list","sandbox":"…","since":"start"}
     → the 23 the run actually created, guest_name and host_name side by side

5. x80_diff_run {…, "normalize":["lowercase_names","pad_to_record"]}
     → 23 compared, 23 identical, 0 differ

6. x80_diff_run {…, "normalize":["lowercase_names"]}
     → 23 compared, 2 identical, 21 differ, each one a record-padding note
```

---

## See also

- [SPEC.md](SPEC.md) — the full design, all 23 tools, and the measured-facts
  index in Appendix A.
- [examples/agent/80un/](examples/agent/80un/) — the runnable exercise.
- [tests/README.md](tests/README.md) — the suite, and which tests need which
  backend binary.
- [deltecent/altairsim](https://github.com/deltecent/altairsim) — 31 tools over
  real S-100 hardware, and the 606-line `DRIVING-WITH-AI.md` this file is
  modelled on. If your job is generic CP/M on period hardware, go there.
