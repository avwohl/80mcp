# The 80un batch exercise

**The task.** You are handed a 54,842-byte 1986 CP/M archive, `method9.arc`,
and a 21,336-byte CP/M `.COM` that claims to unpack it. Prove that the
extraction is byte-correct — not "it printed OK", byte-correct — and then learn
the four ways this goes wrong, before you meet them in anger.

This is the phase-1 exercise, and it uses five of the seven tools:
`x80_profiles`, `x80_probe`, `x80_cpm_run`, `x80_files`, `x80_diff_run`.

## Why this fixture

[80un](https://github.com/avwohl/80un) ships the *same unpacker twice*: as a
CP/M `.COM` for an 8080/Z80, and as a Python package. That makes it the one
fixture in the family that can validate both the batch verb (does the guest run
produce the right files?) and the differential verb (does the guest agree with
a host implementation of the same algorithm, byte for byte?). It is also the
fixture SPEC.md 7.1 and 7.2 measured against, so the numbers are comparable.

## Prerequisites

Nothing here is vendored. This repo does not ship third-party CP/M content —
see [`evidence/README.md`](../../../evidence/README.md) — and the archive is a
1986 third-party file.

```
git clone https://github.com/avwohl/80un        # the fixture and the reference
git clone https://github.com/avwohl/cpmemu      # the backend; build it
pip install -e /path/to/80mcp
80mcp doctor                                    # cpm-hosted must say ready
```

The truncated archive that trap 2 uses is **generated at run time** from your
own copy, in a temporary directory. It is not committed.

## Run it

```
python3 run.py --un80 /path/to/80un
```

`--un80` may be omitted if the checkout is at `$X80_UN80_SRC`, `~/src/80un`, or
beside this repo. `cpmemu` is found by the server itself. `--keep` leaves the
sandbox and the generated fixture on disk so you can look at them.

Exit status is 0 when all 21 measured expectations hold. The recorded run is
[`expected/transcript.txt`](expected/transcript.txt).

## The nine calls, and what each one teaches

### 1. `x80_profiles` — call this first, always

```json
{"name":"x80_profiles","arguments":{"family":"z80","only_ready":true}}
```

One profile is ready: `cpm-hosted`, on `cpmemu 4.8.0`. The part worth reading
is `fidelity.divergences`, five strings that are each a way your assertions can
be wrong on this backend — the missing FCB at 0x5C, the lowercased filenames,
the absent exit status, the `auto` write mode, the 128-byte record padding.
Every one of them shows up later in this exercise.

### 2. `x80_probe` — what does this program actually need?

```json
{"name":"x80_probe","arguments":{"program":"…/80un.com","args":["M9.ARC"],
  "files_in":[{"guest_name":"M9.ARC","host_path":"…/method9.arc"}]}}
```

```
verdict sufficient  recommended cpm-hosted
  exit_reason jmp_0 after 658 ms on cpm-hosted
  first line of output: 80UN - CP/M Archive Unpacker v2.3
  the run completed on cpm-hosted with no unimplemented syscalls
escalation: {"tool":"x80_cpm_run","arguments":{"profile":"cpm-hosted",…}}
```

The verdict comes from the syscalls the program made, not from reading its
banner. `escalation` is a literal next call.

### 3. `x80_cpm_run` — the batch verb

```json
{"profile":"cpm-hosted","cpu":"z80","program":"…/80un.com","args":["M9.ARC"],
 "files_in":[{"guest_name":"M9.ARC","host_path":"…/method9.arc","mode":"binary"}],
 "default_mode":"binary","eol_convert":false,"timeout_ms":10000,
 "assert":{"stdout_contains":["23 file(s) extracted"],
           "stdout_not_contains":["Error","Cannot open","Invalid"],
           "files_created":["TIME2.ASM","ZTIM-S3.CPM"],
           "no_unimplemented_bdos":true},
 "keep_sandbox":true}
```

```
pass=True  exit_reason=jmp_0  wall_ms=658  files_out=23
'exit_code' among the result keys: False        <- SPEC.md 5.4 Invariant 4
B5-TIME.INF -> host b5-time.inf, 1664 bytes
stderr: Program exit via JMP 0
```

Three things to notice.

**There is no `exit_code` key.** Not null, not conditional — absent. Traps 1
and 2 below are why.

**`B5-TIME.INF` came back as `b5-time.inf`.** `cpmemu` lowercases on create.
That is why every manifest row carries `guest_name` *and* `host_name`. Assert
against `guest_name`; touch the filesystem with `host_name`.

**1664 bytes**, when the Python reference writes 1537 for the same member.
`ceil(1537/128)*128 = 1664`. CP/M writes whole 128-byte records and pads with
`0x1A`. Call 5 is about this.

### 4. `x80_files` — what did the run *really* create?

```json
{"op":"list","sandbox":"/tmp/80mcp-…","since":"start"}
```

23 files, with `guest_name` and `host_name` side by side. This comes from the
sandbox manifest, not from `files_out`, so a `collect.exclude` in the run above
would shape the report without rewriting history.

### 5. `x80_diff_run` — the same algorithm, twice, byte for byte

Three runs of the same comparison, differing only in `normalize`:

| `normalize` | compared | identical | differ | only-in |
|---|---|---|---|---|
| `[]` | 0 | 0 | 0 | **46** |
| `["lowercase_names"]` | 23 | **2** | **21** | 0 |
| `["lowercase_names","pad_to_record"]` | 23 | **23** | **0** | 0 |

With no normalization the two trees appear to share **not one file**: 23
lowercase names on the guest side, 23 uppercase on the reference side, 46
"only in" lines. Match the names and you get 2 of 23, because the other 21 have
the record padding. Add `pad_to_record` and all 23 are byte-identical.

The tool explains each difference rather than just reporting it:

```
b5-time.inf: guest 1664 vs reference 1537
  1664 == ceil(1537/128)*128: one side wrote whole CP/M records; adding
  "pad_to_record" to normalize makes these two identical
```

**This is why `normalize` is an enum and not a boolean.** There is no single
"normalize: true" that is right — you have to say which conventions you are
willing to paper over, and the result tells you which ones it used.

### 6. Trap 1 — `default_mode:"auto"`

Same fixture, `default_mode:"auto"` and `eol_convert:true`:

```
pass=False  exit_reason=jmp_0  files_out=2      process rc was 0 either way
stdout ends: '1 file(s) extracted'
  -03MAR86         0 bytes
  B5-TIME.INF   1437 bytes                      (should be 1664)
warning: default_mode 'auto' with eol_convert true is the measured corruption:
  BDOS 22 Make keeps MODE_AUTO (cpmemu.cc:2115) and write_with_conversion takes
  the text branch for it (cpmemu.cc:952-964), truncating every written file at
  its first 0x1A.
```

One file of 23, that one file truncated, `exit_reason` still `jmp_0`, stderr
still `Program exit via JMP 0`, **process rc still 0**. A 96%-failed run that
is indistinguishable from success at the process level. This is the reason
`x80_cpm_run` has no `exit_code` field and the reason `default_mode` defaults
to `binary`.

(SPEC.md 9's acceptance line attributes this to `auto` alone. On cpmemu 4.8.0
`auto` by itself gives 23 files and passes; the corruption needs `auto` **and**
`eol_convert:true`. The warning fires on `auto` either way.)

### 7. Trap 2 — a truncated archive

Half the bytes of the same archive:

```
pass=False  exit_reason=timeout  files_out=15 of 23
last stdout line: '  B5C-MORE.INS'      <- no error message anywhere
```

Fifteen cheerful `OK` lines, then the guest blocks forever reading past the end
of a file and the server's deadline kills it by process group. **Stdout looked
healthy right up to the cut.** An agent asserting only on the absence of the
word "Error" would call this a pass. `exit_reason` and the file count are what
catch it — which is the whole argument for asserting on stdout *and* the
manifest.

### 8. Trap 3 — an op that does not ship yet

```json
{"error":"unsupported","op":"files:resolve","backend":"cpmemu",
 "reason":"SPEC.md 6.4: `handles` and `resolve` are phase 5. Both read
   CPMEmulator::open_files (src/cpmemu.cc:346), which is declared inside a
   3429-line .cc with no header, so nothing outside the process can reach it
   until the backend speaks MBP on fd 3",
 "escalation":{"tool":"x80_files","arguments":{"op":"list","sandbox":"…"}}}
```

`isError:true`, but with a reason you can act on and a call that works. A bare
`"not supported"` cannot be acted on.

### 9. The expectation check

All 21 values are checked against `EXPECTED` at the top of `run.py`. Each was
measured, not derived. If your run disagrees, the diff is printed and the exit
status is 1 — that is a real signal about your build, not a flaky test.

## Files

```
README.md                  this file
run.py                     the driver; EXPECTED holds every measured value
expected/transcript.txt    a recorded run, paths elided
```

## The exercises this repo does not ship yet

SPEC.md's phase-1 documentation deliverable names three example directories.
Only this one can exist in a phase-1 build:

- `examples/agent/hello-cpm/` — assemble and single-step a buggy `HELLO.ASM`
  inside CP/M 3. Needs `x80_monitor`, `x80_breakpoints`, `x80_step`,
  `x80_regs`: **phase 2**. Until then, [altairsim ships exactly this
  exercise](https://github.com/deltecent/altairsim) on period S-100 hardware
  and you should use it.
- `examples/agent/mpm-4up/` — four MP/M consoles and a file-locking
  experiment. Needs `x80_consoles`: **phase 3**.
