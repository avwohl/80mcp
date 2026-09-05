"""80mcp -- an MCP server for the avwohl Z80 and x86 emulator family.

The distribution is ``eightymcp`` and the console script is ``80mcp``; "80mcp"
is not a legal Python identifier, so the import package cannot carry that name.

This module holds the version constant and nothing else. Importing it must not
drag in the JSON-RPC loop, the schemas or a backend: ``80mcp doctor`` and the
test suite both read the version without starting a server.
"""

__all__ = ["__version__", "SERVER_NAME"]

#: Server version, reported in x80_profiles' `server_version` and in the
#: serverInfo of both `server/discover` and the legacy `initialize` reply.
__version__ = "0.1.0"

#: The name reported in serverInfo. Matches the console script, not the
#: import package.
SERVER_NAME = "80mcp"
