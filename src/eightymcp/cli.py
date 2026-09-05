"""``80mcp`` -- the console script.

Two subcommands, both named in SPEC.md 9 phase 1:

``80mcp``            serve the phase-1 tool surface as an MCP server on stdio.
``80mcp doctor``     the same profile data as a table, exiting nonzero on any
                     problem. "The first thing every support conversation for
                     the next two years should start with" (SPEC.md 6.4).

The distribution and the import package are ``eightymcp`` because ``80mcp`` is
not a legal Python identifier; only this script carries the real name.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

from . import SERVER_NAME, __version__
from .jsonrpc import LATEST_REVISION, OLDEST_REVISION, claim_stdout, log
from .profiles import Config, Prober, doctor_report
from .sandbox import SANDBOX_PREFIX
from .tools import SANDBOX_MARKER, build_server

__all__ = ["main", "serve", "doctor", "reap_old_sandboxes"]

#: SPEC.md 4.3, quoted verbatim in x80_open's description: a sandbox directory
#: "outlives [its machine]: on disk for 24 hours". Nothing else in the package
#: enforces the second half of that sentence, so the server does it at startup.
SANDBOX_RETENTION_S = 24 * 60 * 60


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=SERVER_NAME,
        description=(
            "MCP server for the avwohl Z80 and x86 emulator family. With no "
            "subcommand, speaks MCP JSON-RPC on stdio."
        ),
        epilog=(
            f"protocol revisions: {LATEST_REVISION} (native) back to "
            f"{OLDEST_REVISION} (legacy initialize handshake)"
        ),
    )
    p.add_argument("--version", action="version", version=f"{SERVER_NAME} {__version__}")
    p.add_argument(
        "--config",
        metavar="PATH",
        help="config file to use instead of $EIGHTYMCP_CONFIG / "
             "$XDG_CONFIG_HOME/80mcp/config.json",
    )
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser(
        "doctor",
        help="print the backend and profile table; exit nonzero on any problem",
        description=(
            "Every backend: found or not, version, binary path, dynamic-library "
            "resolution, required images and their sha256 pins, and the "
            "ROM/disk pairing check. Exit 0 clean, 1 at least one profile "
            "blocked, 2 nothing runnable."
        ),
    )
    d.add_argument("-v", "--verbose", action="store_true",
                   help="every path searched, and each profile's capabilities")
    d.add_argument("--json", action="store_true",
                   help="the x80_profiles result body instead of the table")

    sub.add_parser("serve", help="speak MCP on stdio (the default)")
    return p


def doctor(*, config_path: str | None = None, verbose: bool = False, as_json: bool = False) -> int:
    prober = Prober(Config.load(config_path))
    if as_json:
        body = prober.result().to_json()
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0 if all(p["ready"] for p in body["profiles"]) else 1
    text, code = doctor_report(prober, verbose=verbose)
    print(text)
    return code


def reap_old_sandboxes(
    *, max_age_s: float = SANDBOX_RETENTION_S, now: float | None = None
) -> list[str]:
    """Remove kept sandboxes older than the retention window. Best effort.

    Only ``keep_sandbox:true`` leaves a tree behind, and only a tree carrying
    this server's marker file is touched, so nothing here can remove a
    directory another program made. The age comes from the marker's own
    ``created_ns`` rather than from the directory mtime, because an
    ``x80_files{op:"to_guest"}`` moves the mtime and the retention clock starts
    at the run, not at the last read.

    Nothing older than 24 hours can belong to a call still in flight: the
    largest ``timeout_ms`` any phase-1 schema accepts is 600000.
    """
    now = time.time() if now is None else now
    removed: list[str] = []
    try:
        entries = sorted(Path(tempfile.gettempdir()).glob(SANDBOX_PREFIX + "*"))
    except OSError:
        return removed
    for entry in entries:
        marker = entry / SANDBOX_MARKER
        if not entry.is_dir() or not marker.is_file():
            continue
        try:
            created_ns = int(json.loads(marker.read_text()).get("created_ns", 0))
            age_s = now - (created_ns / 1e9 if created_ns else marker.stat().st_mtime)
        except (OSError, ValueError, TypeError):
            continue
        if age_s < max_age_s:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed.append(str(entry))
    return removed


def serve(*, config_path: str | None = None) -> int:
    """Serve on stdio until EOF.

    :func:`~eightymcp.jsonrpc.claim_stdout` runs first and is not optional: it
    moves fd 1 out of the way and points ``sys.stdout`` at stderr, so that a
    stray ``print`` -- ours, a backend's, or a library's -- cannot land in the
    middle of a JSON-RPC frame. Every launch under this server also inherits
    that fd 1, so a chatty emulator writes to the log rather than to the
    client.
    """
    if config_path:
        # Config is loaded per call inside the tools, so the only way a
        # command-line config reaches them is the environment they read.
        os.environ["EIGHTYMCP_CONFIG"] = config_path
    out = claim_stdout()
    server = build_server()
    reaped = reap_old_sandboxes()
    if reaped:
        log(f"reaped {len(reaped)} sandbox(es) past the 24-hour retention of SPEC.md 4.3")
    log(f"{SERVER_NAME} {__version__} serving {len(server.registry)} tools on stdio "
        f"(protocol {LATEST_REVISION}, legacy initialize accepted back to {OLDEST_REVISION})")
    try:
        return server.serve(sys.stdin.buffer, out)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except BrokenPipeError:
        # The client went away mid-write. Nothing to report to, and a traceback
        # on stderr would be the last thing in the log for a normal shutdown.
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        return doctor(config_path=args.config, verbose=args.verbose, as_json=args.json)
    return serve(config_path=args.config)


if __name__ == "__main__":            # python -m eightymcp.cli
    sys.exit(main())
