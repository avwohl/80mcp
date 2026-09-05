# examples/

Ready-made working exercises for an agent driving this server. Each is a real
task with the calls that solve it and the output they actually produced.

| directory | exercise | phase | state |
|---|---|---|---|
| [`agent/80un/`](agent/80un/) | unpack a 1986 CP/M archive and prove it byte-correct against a Python implementation of the same algorithm; then break it four ways on purpose | 1 | **runnable** |
| `agent/hello-cpm/` | assemble, debug and single-step a buggy `HELLO.ASM` inside CP/M 3 — deliberately the same exercise altairsim ships, on a different machine | 2 | not built; needs `x80_open`/`x80_step`/`x80_regs` |
| `agent/mpm-4up/` | four MP/M consoles, a file-locking experiment, and a human invited to `ssh` onto one console while the agent drives the others | 3 | not built; needs `x80_consoles` |

Only the first exists. The other two are listed because SPEC.md 9 names them
and because their absence is the honest shape of a phase-1 build: the tools
they need are not registered, so the exercises cannot be written yet.

Read [`../DRIVING-WITH-AI.md`](../DRIVING-WITH-AI.md) first. The Gotchas
chapter is what the 80un exercise is a hands-on version of.
