"""The tool registry, ``tools/list``, ``tools/call``, and argument validation.

Three things are load-bearing here.

**Deterministic order.** SPEC.md 4.1: ``tools/list`` MUST NOT vary
per-connection. Identical means byte-identical, so the order comes from
:data:`eightymcp.schemas.CANONICAL_TOOL_ORDER` (SPEC.md 6.1's numbering) rather
than from dict iteration or from whatever order the tools happened to register
in. SPEC.md 4.6 pins the list's cache metadata too: ``ttlMs: 86400000``,
``cacheScope: "public"`` -- identical for every user of the same install,
changing only when ``backends.toml`` changes, and the server restarts on that.

**Two error channels, kept apart.**

===============================  ==========================================
the request was wrong            the run failed
===============================  ==========================================
JSON-RPC ``error`` object        normal result, ``isError:true``
unknown method, unknown tool,    bad handle, timeout, unsupported op,
arguments violating the          backend missing, a guest that produced
inputSchema                      the wrong bytes
:class:`~eightymcp.jsonrpc.JsonRpcError`
                                 :class:`~eightymcp.types.ToolExecutionError`
===============================  ==========================================

SPEC.md 4.2 makes the second column normative for at least one case -- "expired
handle -> tool execution error, not JSON-RPC error" -- and SPEC.md 4.7 gives
the structured body an agent is expected to act on. An agent that cannot tell
"I called it wrong" from "it did not work" retries the wrong thing.

**Argument validation before the handler runs.** 80mcp has no third-party
runtime dependencies, so :func:`validate_and_fill` implements the JSON Schema
2020-12 subset the phase-1 schemas actually use. It fills declared defaults, so
a handler reads ``args["default_mode"]`` and gets ``"binary"`` without
re-encoding SPEC.md 5.4 Invariant 3 in seven places.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import SERVER_NAME, __version__
from .jsonrpc import (
    Connection,
    ErrorCode,
    JsonRpcError,
    JsonRpcServer,
    ServerIdentity,
    log,
)
from .schemas import ANNOTATIONS, CANONICAL_TOOL_ORDER, INPUT_SCHEMAS, TOOL_DESCRIPTIONS
from .types import CacheScope, ResultType, ToolAnnotations, ToolError, ToolExecutionError

__all__ = [
    "TOOLS_LIST_TTL_MS",
    "TOOLS_LIST_CACHE_SCOPE",
    "SchemaViolation",
    "validate_and_fill",
    "CallContext",
    "ToolResult",
    "ToolHandler",
    "Tool",
    "ToolRegistry",
    "Server",
]


#: SPEC.md 4.6. The list changes only when ``backends.toml`` changes, and the
#: server restarts on that.
TOOLS_LIST_TTL_MS = 86400000

#: SPEC.md 4.6. Identical for every user of the same install.
TOOLS_LIST_CACHE_SCOPE = CacheScope.PUBLIC


# ---------------------------------------------------------------------------
# JSON Schema 2020-12, the subset the phase-1 schemas use
# ---------------------------------------------------------------------------

class SchemaViolation(Exception):
    """One or more arguments do not match the tool's inputSchema.

    Carries every violation, not the first: an agent fixing a call wants the
    whole list. Turned into a JSON-RPC ``-32602`` by
    :meth:`Server.handle_tools_call`, because this is the "you called it wrong"
    channel.
    """

    def __init__(self, violations: Sequence[Mapping[str, Any]]):
        super().__init__(f"{len(violations)} schema violation(s)")
        self.violations = list(violations)


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # bool is a subclass of int in Python and is not an integer in JSON Schema.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _resolve_ref(ref: str, root: Mapping[str, Any]) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"only local $refs are supported, got {ref!r}")
    node: Any = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        node = node[token]
    return node


def _validate(
    schema: Mapping[str, Any],
    value: Any,
    root: Mapping[str, Any],
    path: str,
    out: list[dict[str, Any]],
    *,
    fill: bool,
) -> Any:
    """Check ``value`` against ``schema``, appending violations to ``out``.

    Returns ``value`` with declared defaults filled in when ``fill`` is true.
    Trial validations (``oneOf`` branches, ``if`` conditions) run with
    ``fill=False`` so a discarded branch cannot leave a default behind.
    """
    if "$ref" in schema:
        target = _resolve_ref(schema["$ref"], root)
        merged = {k: v for k, v in schema.items() if k != "$ref"}
        value = _validate(target, value, root, path, out, fill=fill)
        if merged:
            value = _validate(merged, value, root, path, out, fill=fill)
        return value

    declared = schema.get("type")
    if declared is not None:
        types = [declared] if isinstance(declared, str) else list(declared)
        if not any(_TYPE_CHECKS.get(t, lambda _v: True)(value) for t in types):
            out.append({
                "path": path or "$",
                "message": f"expected type {'|'.join(types)}",
                "got": type(value).__name__,
            })
            return value

    if "enum" in schema and value not in schema["enum"]:
        out.append({
            "path": path or "$",
            "message": "value is not one of the allowed values",
            "allowed": schema["enum"],
            "got": value,
        })

    if "const" in schema and value != schema["const"]:
        out.append({
            "path": path or "$",
            "message": "value must be the declared const",
            "expected": schema["const"],
            "got": value,
        })

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            out.append({
                "path": path or "$",
                "message": f"does not match pattern {pattern}",
                "got": value,
            })

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append({"path": path or "$", "message": f"must be >= {schema['minimum']}", "got": value})
        if "maximum" in schema and value > schema["maximum"]:
            out.append({"path": path or "$", "message": f"must be <= {schema['maximum']}", "got": value})

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append({"path": path or "$", "message": f"needs at least {schema['minItems']} item(s)"})
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append({"path": path or "$", "message": f"allows at most {schema['maxItems']} item(s)"})
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for item in value:
                if item in seen:
                    out.append({"path": path or "$", "message": "items must be unique", "duplicate": item})
                    break
                seen.append(item)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            value = [
                _validate(item_schema, item, root, f"{path}[{i}]", out, fill=fill)
                for i, item in enumerate(value)
            ]

    if isinstance(value, dict):
        properties: Mapping[str, Any] = schema.get("properties") or {}
        for name in schema.get("required", ()):
            if name not in value:
                out.append({
                    "path": f"{path}.{name}" if path else name,
                    "message": "required property is missing",
                })
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for name in value:
                if name not in properties:
                    out.append({
                        "path": f"{path}.{name}" if path else name,
                        "message": "additional properties are not allowed",
                        "allowed": sorted(properties),
                    })
        result = dict(value)
        for name, sub in properties.items():
            child = f"{path}.{name}" if path else name
            if name in result:
                result[name] = _validate(sub, result[name], root, child, out, fill=fill)
            elif fill and isinstance(sub, dict) and "default" in sub:
                result[name] = _fill_defaults(sub, sub["default"], root)
        if isinstance(additional, dict):
            for name, item in result.items():
                if name in properties:
                    continue
                child = f"{path}.{name}" if path else name
                result[name] = _validate(additional, item, root, child, out, fill=fill)
        value = result

    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            trial: list[dict[str, Any]] = []
            _validate(branch, value, root, path, trial, fill=False)
            if not trial:
                matches += 1
        if matches != 1:
            out.append({
                "path": path or "$",
                "message": f"must match exactly one of the alternatives, matched {matches}",
                "alternatives": schema["oneOf"],
            })

    if "allOf" in schema:
        for branch in schema["allOf"]:
            value = _validate(branch, value, root, path, out, fill=fill)

    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            trial = []
            _validate(branch, value, root, path, trial, fill=False)
            if not trial:
                break
        else:
            out.append({"path": path or "$", "message": "must match at least one alternative"})

    if "if" in schema:
        trial = []
        _validate(schema["if"], value, root, path, trial, fill=False)
        branch = schema.get("then") if not trial else schema.get("else")
        if isinstance(branch, dict):
            value = _validate(branch, value, root, path, out, fill=fill)

    return value


def _fill_defaults(schema: Mapping[str, Any], value: Any, root: Mapping[str, Any]) -> Any:
    """Recursively fill defaults into a value taken from a ``default`` keyword.

    A default that is an object may itself have properties with defaults --
    ``collect`` is not defaulted at all, but ``collect.return_content`` is, so
    an explicitly supplied ``{}`` must come back filled. Copies, so the schema
    literal is never handed to a handler.
    """
    if isinstance(value, dict):
        filled = {k: _fill_defaults(schema.get("properties", {}).get(k, {}), v, root)
                  for k, v in value.items()}
        for name, sub in (schema.get("properties") or {}).items():
            if name not in filled and isinstance(sub, dict) and "default" in sub:
                filled[name] = _fill_defaults(sub, sub["default"], root)
        return filled
    if isinstance(value, list):
        return list(value)
    return value


def validate_and_fill(schema: Mapping[str, Any], arguments: Any) -> dict[str, Any]:
    """Validate ``arguments`` against ``schema`` and return them with defaults.

    Raises :class:`SchemaViolation` listing every problem found.

    The supported keyword set is exactly what the schemas in
    :mod:`eightymcp.schemas` use, plus ``allOf``/``if``/``then`` for the phase-2
    ``x80_open`` schema: ``$ref`` (local), ``$defs``, ``type``, ``enum``,
    ``const``, ``properties``, ``required``, ``additionalProperties`` (false or
    a schema), ``items``, ``minItems``, ``maxItems``, ``uniqueItems``,
    ``minimum``, ``maximum``, ``pattern``, ``oneOf``, ``anyOf``, ``allOf``,
    ``if``/``then``/``else``, ``default``. An unknown keyword is ignored rather
    than rejected, which is what JSON Schema requires of a validator.
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise SchemaViolation([
            {"path": "$", "message": "arguments must be an object", "got": type(arguments).__name__}
        ])
    violations: list[dict[str, Any]] = []
    filled = _validate(schema, arguments, schema, "", violations, fill=True)
    if violations:
        raise SchemaViolation(violations)
    return filled


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@dataclass
class CallContext:
    """What a tool handler is told about the call it is serving."""

    tool: str
    #: Arguments after schema validation and default filling.
    arguments: dict[str, Any]
    connection: Connection
    #: The request's ``_meta``, if any. 2026-07-28 carries the protocol version
    #: and client capabilities here (SPEC.md 4.1).
    meta: dict[str, Any] = field(default_factory=dict)
    request_id: Any = None
    server: "Server | None" = None


@dataclass
class ToolResult:
    """What a tool handler returns.

    ``structured`` becomes ``structuredContent``; SPEC.md 4.1 notes the
    2026-07-28 revision lets it be any JSON value. ``content`` is the
    human-readable fallback and is filled in from ``structured`` when a handler
    does not supply one, so a client that ignores ``structuredContent`` still
    sees the result.
    """

    structured: Any = None
    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False
    result_type: ResultType = ResultType.COMPLETE

    @classmethod
    def ok(cls, structured: Any, *, text: str | None = None) -> "ToolResult":
        content = [{"type": "text", "text": text}] if text is not None else []
        return cls(structured=structured, content=content)

    @classmethod
    def error(cls, err: ToolError, *, text: str | None = None) -> "ToolResult":
        """An ``isError:true`` result -- the run failed, the call was fine."""
        body = err.to_json()
        content = [{"type": "text", "text": text}] if text is not None else []
        return cls(structured=body, content=content, is_error=True)

    def to_json(self, connection: Connection) -> dict[str, Any]:
        content = list(self.content)
        if not content and self.structured is not None:
            content = [{
                "type": "text",
                "text": json.dumps(self.structured, indent=2, ensure_ascii=False),
            }]
        out: dict[str, Any] = {}
        if connection.emits_result_metadata:
            # SPEC.md 4.1 / Appendix B item 6: "complete" or "input_required",
            # never the rejected third value.
            out["resultType"] = self.result_type.value
        out["content"] = content
        if self.structured is not None:
            out["structuredContent"] = self.structured
        out["isError"] = self.is_error
        return out


#: ``(context) -> ToolResult``. Raise
#: :class:`~eightymcp.types.ToolExecutionError` for an actionable failure;
#: raise :class:`~eightymcp.jsonrpc.JsonRpcError` only when the *request* is
#: wrong, which validation has usually caught already.
ToolHandler = Callable[[CallContext], ToolResult]


@dataclass(frozen=True)
class Tool:
    """One registered tool."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    annotations: ToolAnnotations
    handler: ToolHandler
    output_schema: Mapping[str, Any] | None = None
    title: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.title:
            out["title"] = self.title
        out["description"] = self.description
        out["inputSchema"] = dict(self.input_schema)
        if self.output_schema is not None:
            out["outputSchema"] = dict(self.output_schema)
        out["annotations"] = self.annotations.to_json()
        return out

    @classmethod
    def from_spec(
        cls,
        name: str,
        handler: ToolHandler,
        *,
        output_schema: Mapping[str, Any] | None = None,
        title: str | None = None,
    ) -> "Tool":
        """Build a tool from the transcribed SPEC.md tables.

        The schema, the description and the annotation row all come from
        :mod:`eightymcp.schemas`, so no caller retypes them and a name that is
        not in SPEC.md 6.1 fails loudly here rather than shipping an
        undocumented tool.
        """
        if name not in INPUT_SCHEMAS:
            raise KeyError(f"{name}: no inputSchema transcribed from SPEC.md 6.4")
        if name not in ANNOTATIONS:
            raise KeyError(f"{name}: no row in the SPEC.md 6.2 annotation table")
        return cls(
            name=name,
            description=TOOL_DESCRIPTIONS[name],
            input_schema=INPUT_SCHEMAS[name],
            annotations=ANNOTATIONS[name],
            handler=handler,
            output_schema=output_schema,
            title=title,
        )


class ToolRegistry:
    """Name -> tool, iterated in SPEC.md 6.1 order.

    SPEC.md 4.7: "a tool enters the list on the release where at least one
    backend actually serves it. A tool that is listed and always errors is a
    lie in ``tools/list``." So the registry lists what was registered, and
    registration is the deliberate act.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"{tool.name} is already registered")
        if tool.name not in CANONICAL_TOOL_ORDER:
            raise ValueError(
                f"{tool.name} is not one of the 23 tools in SPEC.md 6.1; "
                "adding a tool means adding it to the spec first"
            )
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise JsonRpcError(
                ErrorCode.INVALID_PARAMS,
                f"Unknown tool: {name}",
                data={"tool": name, "available_tools": [t.name for t in self.ordered()]},
            ) from None

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def ordered(self) -> list[Tool]:
        """Registered tools in SPEC.md 6.1 order, always the same order."""
        index = {name: i for i, name in enumerate(CANONICAL_TOOL_ORDER)}
        return sorted(self._tools.values(), key=lambda t: index[t.name])


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class Server:
    """Tool surface on top of :class:`~eightymcp.jsonrpc.JsonRpcServer`."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        identity: ServerIdentity | None = None,
        connection: Connection | None = None,
    ):
        self.registry = registry or ToolRegistry()
        self.identity = identity or ServerIdentity(
            name=SERVER_NAME,
            version=__version__,
            instructions=(
                "Emulator supervisor for the avwohl Z80 and x86 family. Call "
                "x80_profiles first: capabilities differ enormously between "
                "backends, and a profile with a missing image or a ROM/disk "
                "version mismatch will fail. CP/M results deliberately carry "
                "no exit code -- assert success from stdout plus the file "
                "manifest."
            ),
        )
        self.rpc = JsonRpcServer(
            self.identity,
            {
                "tools/list": self.handle_tools_list,
                "tools/call": self.handle_tools_call,
            },
            connection=connection,
        )

    # -- registration -----------------------------------------------------

    def register(self, tool: Tool) -> Tool:
        return self.registry.register(tool)

    def register_spec_tool(
        self,
        name: str,
        handler: ToolHandler,
        *,
        output_schema: Mapping[str, Any] | None = None,
        title: str | None = None,
    ) -> Tool:
        return self.registry.register(
            Tool.from_spec(name, handler, output_schema=output_schema, title=title)
        )

    # -- methods ----------------------------------------------------------

    def handle_tools_list(
        self, params: Mapping[str, Any], conn: Connection
    ) -> dict[str, Any]:
        """``tools/list``.

        SPEC.md 4.1: MUST NOT vary per-connection. Nothing here reads
        ``conn`` except to decide whether the cache metadata is emitted, and
        the metadata is a property of the revision, not of the caller.

        Pagination is not implemented: seven tools in phase 1, 23 at the end of
        the roadmap. A ``cursor`` argument would be a lie about a page that
        does not exist, so an unexpected one is a JSON-RPC error.
        """
        cursor = params.get("cursor")
        if cursor is not None:
            raise JsonRpcError(
                ErrorCode.INVALID_PARAMS,
                "tools/list is not paginated on this server",
                data={"cursor": cursor, "tool_count": len(self.registry)},
            )
        result: dict[str, Any] = {"tools": [t.to_json() for t in self.registry.ordered()]}
        if conn.emits_result_metadata:
            # SPEC.md 4.6: ttlMs 86400000, cacheScope "public".
            result["ttlMs"] = TOOLS_LIST_TTL_MS
            result["cacheScope"] = TOOLS_LIST_CACHE_SCOPE.value
        return self.rpc.decorate_result(result, conn)

    def handle_tools_call(
        self, params: Mapping[str, Any], conn: Connection
    ) -> dict[str, Any]:
        """``tools/call``. The two error channels part company here."""
        name = params.get("name")
        if not isinstance(name, str):
            raise JsonRpcError(
                ErrorCode.INVALID_PARAMS,
                'tools/call requires a string "name"',
                data={"got": name},
            )
        tool = self.registry.get(name)          # JSON-RPC error if unknown

        raw_arguments = params.get("arguments", {})
        try:
            arguments = validate_and_fill(tool.input_schema, raw_arguments)
        except SchemaViolation as exc:
            # Channel 1: the call was malformed. The agent must change the
            # call, so this is a JSON-RPC error and not an isError result.
            raise JsonRpcError(
                ErrorCode.INVALID_PARAMS,
                f"Arguments do not match {name}'s inputSchema",
                data={"tool": name, "violations": exc.violations},
            ) from None
        except ValueError as exc:               # a broken schema is our bug
            raise JsonRpcError(
                ErrorCode.INTERNAL_ERROR, f"{name}: {exc}", data={"tool": name}
            ) from None

        meta = params.get("_meta")
        context = CallContext(
            tool=name,
            arguments=arguments,
            connection=conn,
            meta=dict(meta) if isinstance(meta, dict) else {},
            server=self,
        )

        try:
            result = tool.handler(context)
        except ToolExecutionError as exc:
            # Channel 2: the call was fine and the run failed. SPEC.md 4.2 and
            # 4.7: a structured, actionable body inside a normal result.
            result = ToolResult.error(exc.err)
        except JsonRpcError:
            raise
        except Exception as exc:
            # A bug in a handler is still a tool execution failure from the
            # agent's point of view; the traceback goes to stderr, where the
            # operator can find it, and never into the protocol.
            traceback.print_exc(file=sys.stderr)
            log(f"{name}: unhandled {type(exc).__name__}: {exc}")
            result = ToolResult.error(
                ToolError(
                    "internal_error",
                    {
                        "tool": name,
                        "exception": type(exc).__name__,
                        "reason": str(exc),
                        "hint": "the server logged a traceback to stderr",
                    },
                )
            )

        if not isinstance(result, ToolResult):
            raise JsonRpcError(
                ErrorCode.INTERNAL_ERROR,
                f"{name} handler returned {type(result).__name__}, not a ToolResult",
            )
        return result.to_json(conn)

    # -- running ----------------------------------------------------------

    def serve(self, inp: Any, out: Any) -> int:
        return self.rpc.serve(inp, out)
