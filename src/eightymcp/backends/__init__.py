"""Backend adapters. One module per backend binary.

SPEC.md 5.1, rule 1: "80mcp never contains a copy of any emulator source." The
modules here exec already-built binaries found at runtime by config or PATH.
An absent backend is a profile reporting ``ready:false`` with a ``blocked_by``
string, never a crash.

Phase 1 ships two, both one-shot adapters (SPEC.md 5.3 shape 3):
``cpmemu`` and ``dosiz``.
"""
