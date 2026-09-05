# tests/

340-odd tests over the phase-1 surface. The suite is designed so that **a
machine with no emulator binaries at all still runs it**: everything that needs
a backend or a fixture is gated behind a `skipif` that names what is missing
and how to supply it. A skip is the expected outcome of a missing prerequisite;
a failure never is.

## Running it

```
pip install -e ".[dev]"
pytest
```

`pyproject.toml` sets `pythonpath = ["src"]` and `testpaths = ["tests"]`, so
plain `pytest` from the repo root is the whole invocation. It also sets
`filterwarnings = ["error"]`: a warning from anywhere fails the run, which is
deliberate — this server's job is to notice things.

The `dev` extra pulls in `pytest` and `jsonschema`. `jsonschema` is used by one
test, `test_every_input_schema_validates_against_the_2020_12_metaschema`, which
is half of the SPEC.md 6.0 CI gate. The server itself never imports it, and the
runtime has no third-party dependencies at all.

Measured on this machine:

| environment | result |
|---|---|
| every backend and fixture present | **354 passed, 0 skipped** in 18 s |
| `cpmemu` and a `romwbw_disks` checkout, no `dosiz`, no `80un` | **329 passed, 25 skipped** in 10 s |

## What each test file needs

| file | tests | needs | without it |
|---|---|---|---|
| `test_protocol.py` | 44 | nothing | all run |
| `test_sandbox.py` | 45 | nothing | all run |
| `test_normalize.py` | 36 | `80un` + `cpmemu` for 2 | 34 run, 2 skip |
| `test_profiles.py` | 55 | `romwbw_emu` for 1; a `romwbw_disks` catalog for 2 | see the caveat below |
| `test_cpmemu.py` | 43 | `cpmemu` + `80un` for 6 | 37 run, 6 skip |
| `test_dosiz.py` | 77 | a `dosiz` binary and its `tests/` fixtures for 16 | 61 run, 16 skip |
| `test_tools.py` | 54 | `cpmemu` + `80un` for 6, `dosiz` for 1 | 47 run, 7 skip |

Counts are of collected tests, which exceeds the number of `def test_`
functions because several are parametrised.

## Supplying the backends and fixtures

Nothing is vendored. Clone and build what you want to exercise, then point the
suite at it. **Two env-var families are in use** and both are honoured; they
are duplicates of each other and that is a wart, not a design:

| what | variable | also accepted |
|---|---|---|
| the `cpmemu` binary | `EIGHTYMCP_CPMEMU` | `X80_CPMEMU` |
| a [`80un`](https://github.com/avwohl/80un) checkout | `X80_UN80_SRC` | `EIGHTYMCP_80UN` |
| the `dosiz` binary | `EIGHTYMCP_DOSIZ` | — |
| the `dosiz` `tests/` fixture directory | `EIGHTYMCP_DOSIZ_TESTS` | — |
| the `romwbw_emu` binary | `EIGHTYMCP_ROMWBW_EMU` | — |
| a `romwbw_disks` catalog directory | `EIGHTYMCP_CATALOG_DIR` | — |

Set both names of a pair when you want a fully green run; different files reach
for different ones. A full-fixture invocation looks like:

```
X80_UN80_SRC=$HOME/src/80un        EIGHTYMCP_80UN=$HOME/src/80un \
X80_CPMEMU=$HOME/src/cpmemu/src/cpmemu \
EIGHTYMCP_CPMEMU=$HOME/src/cpmemu/src/cpmemu \
EIGHTYMCP_DOSIZ=$HOME/src/qxDOS/build/dosiz \
EIGHTYMCP_DOSIZ_TESTS=$HOME/src/dosiz/tests \
pytest
```

Absent an explicit variable, each finder falls back to `$PATH` and then to a
sibling checkout — `~/src/cpmemu/src/cpmemu`, `~/src/80un`, and so on — which
is why a developer with the family checked out beside this repo usually needs
no variables at all. `80mcp doctor` prints exactly which path each backend
resolved through; run it first when a test skips and you did not expect it to.

`EIGHTYMCP_NO_SIBLING_SEARCH=1` turns that last fallback off. See the caveat.

## See the skip reasons

```
pytest -rs
```

Every skip names its own prerequisite:

```
SKIPPED [1] tests/test_dosiz.py:727: no dosiz binary; set $EIGHTYMCP_DOSIZ or put it on PATH
SKIPPED [1] tests/test_tools.py:549: needs a 80un checkout (X80_UN80_SRC) and a cpmemu binary (X80_CPMEMU)
SKIPPED [1] tests/test_normalize.py:539: no 80un checkout; set X80_UN80_SRC
```

## Two known caveats

**1. Two tests fail rather than skip when no `romwbw_disks` catalog is
reachable.** `test_x80_images_never_fetches_without_both_flags`, in both
`test_profiles.py` and `test_tools.py`, asserts that `x80_images` with
`dry_run:false` and `allow_fetch:false` reports `fetch_not_allowed`. With no
catalog on the machine the id `hd1k_combo` is not known, so the argument check
fires first and the error is `bad_argument`:

```json
{"error":"bad_argument","argument":"image_ids","reason":"unknown catalog id(s)",
 "unknown":["hd1k_combo"],"known":["freedos_starter","mpm2_system"],
 "catalog_version":"3.5.1"}
```

Both are still `isError`, so the behaviour is defensible; the tests are not
gated on the catalog being present, which they should be. Until that is fixed,
clone [`romwbw_disks`](https://github.com/avwohl/romwbw_disks) beside this repo
or point `EIGHTYMCP_CATALOG_DIR` at a catalog.

**2. `EIGHTYMCP_NO_SIBLING_SEARCH=1` fails three tests, not two.** The extra one
is `test_romwbw_emu_really_has_no_start_flag`, whose `skipif` guard checks
`shutil.which` and `~/src/romwbw_emu/src/romwbw_emu` directly while the code
under test asks the `Prober`, which honours the variable. Guard and subject
disagree about what "installed" means, so the test runs and then cannot find
the binary. Do not set that variable when running the suite.

## What the suite actually guards

Worth knowing, because these are the assertions that would otherwise rot:

- **SPEC.md 5.4 Invariant 4** — `test_a_cpm_result_has_no_exit_code_field_anywhere`
  serialises a `RunResult` and walks its keys. Not a substring search: the
  `cpm-hosted` fidelity text contains the words "no exit_code field", so a
  substring check passes for the wrong reason.
- **SPEC.md Appendix B item 6** — every `resultType` is `complete` or
  `input_required` and every `cacheScope` is `public` or `private`, scanned
  across `src/**/*.py`. The 6.0 conformance text also names the docs; the scan
  covers source only today.
- **SPEC.md 6.0** — every `inputSchema` `json.loads` and validates against the
  JSON Schema 2020-12 metaschema.
- **SPEC.md 4.1** — `tools/list` is byte-identical and in the same order on
  both protocol revisions, and the 2026-only envelope fields are omitted on a
  legacy connection.
- **SPEC.md 5.5** — deadlines are enforced and children are killed by process
  group. Verified on both backends with a 3-byte `JMP $` and a 2-byte `EB FE`:
  killed at 1002 ms and 1202 ms against 1000 ms and 1200 ms budgets, with no
  orphan processes left behind.
- **SPEC.md 7.1 / 7.2** — the 80un numbers: 23 files, `B5-TIME.INF` at 1664
  bytes, `exit_reason:"jmp_0"`, and the three normalization outcomes
  (23/23 identical, 2/21, and 46 only-in lines).

The last of those is also available as a standalone driver that does not need
pytest: [`examples/agent/80un/run.py`](../examples/agent/80un/README.md).
