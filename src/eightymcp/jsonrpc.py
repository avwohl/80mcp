"""Newline-delimited JSON-RPC 2.0 over stdio, with dual-revision negotiation.

Two things this module exists to get right.

**1. Nothing but JSON-RPC ever reaches stdout.**

SPEC.md 1.4 records the failure it is guarding against. altairsim's own code
knows the rule -- its ``--mirror`` bind error is deliberately sent *"to STDERR,
never out, which is the JSON-RPC channel a stray line would corrupt"* -- and
its ``monitor {!...}`` escape violates it by executing a host shell whose output
lands on stdout and desyncs strict clients. 80mcp execs emulator binaries that
write to stdout by design, so the rule needs teeth rather than discipline:
:func:`claim_stdout` dups fd 1 to a private stream and points fd 1 itself at
fd 2. After that call, a stray ``print()``, a library banner, or a subprocess
that inherited fd 1 all land on stderr, and the only writer to the real channel
is :meth:`JsonRpcServer.write`.

**2. Dual revision support.**

SPEC.md section 4 targets MCP revision 2026-07-28, which removed the
``initialize`` handshake, added ``server/discover``, moved the protocol version
into every request's ``_meta``, and made ``resultType`` on every result and
``ttlMs`` + ``cacheScope`` on the list methods required. No client ships that
yet, and every client that ships today opens with ``initialize``. A server that
only speaks 2026-07-28 connects to nothing.

So the connection negotiates, once, and remembers:

=================  ==========================================================
revision           what the server emits
=================  ==========================================================
``2026-07-28``     ``resultType`` on every result; ``ttlMs`` + ``cacheScope``
                   on ``tools/list``; ``server/discover`` answered
``2025-11-25``     no ``resultType``, no ``ttlMs``, no ``cacheScope``
``2025-06-18``     as above
=================  ==========================================================

Everything else -- the tool list, the schemas, the annotations, the result
bodies -- is identical across revisions. Only the envelope moves.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, BinaryIO, Callable, Mapping

from . import SERVER_NAME, __version__
from .types import ProtocolRevision, ResultType

__all__ = [
    "SUPPORTED_REVISIONS",
    "LATEST_REVISION",
    "OLDEST_REVISION",
    "METADATA_REVISION",
    "ErrorCode",
    "JsonRpcError",
    "Connection",
    "ServerIdentity",
    "MethodHandler",
    "JsonRpcServer",
    "claim_stdout",
    "log",
    "negotiate",
]


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------

#: Newest first. The values are ISO dates, so string comparison is revision
#: comparison.
SUPPORTED_REVISIONS: tuple[ProtocolRevision, ...] = (
    ProtocolRevision.V2026_07_28,
    ProtocolRevision.V2025_11_25,
    ProtocolRevision.V2025_06_18,
)

LATEST_REVISION: ProtocolRevision = SUPPORTED_REVISIONS[0]
OLDEST_REVISION: ProtocolRevision = SUPPORTED_REVISIONS[-1]

#: The revision from which ``resultType``, ``ttlMs`` and ``cacheScope`` are
#: required (SPEC.md 4.1). Below it they are omitted: a 2025-06-18 client has
#: no schema slot for them and some validate strictly.
METADATA_REVISION: ProtocolRevision = ProtocolRevision.V2026_07_28


def negotiate(requested: str | None) -> ProtocolRevision:
    """Pick the revision to speak, given what the client asked for.

    * exact match -> that revision;
    * a newer revision than anything we know -> :data:`LATEST_REVISION`, since
      a client that asked for the future can be assumed to understand our
      present, and MCP's own rule is that the server answers with a version it
      supports;
    * something between our known revisions -> the highest we support that is
      not newer than the request;
    * older than everything we support -> :class:`JsonRpcError`, listing what
      we do support, rather than pretending.
    """
    if requested is None:
        return LATEST_REVISION
    text = str(requested)
    for rev in SUPPORTED_REVISIONS:
        if rev.value == text:
            return rev
    if text > LATEST_REVISION.value:
        return LATEST_REVISION
    for rev in SUPPORTED_REVISIONS:  # newest first
        if rev.value <= text:
            return rev
    raise JsonRpcError(
        ErrorCode.INVALID_PARAMS,
        "Unsupported MCP protocol revision",
        data={
            "requested": text,
            "supported": [r.value for r in SUPPORTED_REVISIONS],
        },
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ErrorCode(IntEnum):
    """JSON-RPC 2.0 codes, plus MCP's use of them.

    SPEC.md 4.1: the 2026-07-28 revision moved resource-not-found from
    ``-32002`` to ``-32602``, so there is no MCP-specific code left in the
    range this server uses.
    """

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    """A protocol-level failure: the request itself was wrong.

    This is one of the two error channels and they are kept rigorously apart.
    A ``JsonRpcError`` means the client sent something the server cannot act on
    -- unparseable JSON, an unknown method, an unknown tool name, arguments
    that violate the tool's inputSchema. A tool that ran and failed returns a
    normal result with ``isError:true`` instead; see
    :class:`eightymcp.types.ToolExecutionError`. Confusing the two is how an
    agent ends up unable to tell "I called it wrong" from "it did not work".
    """

    def __init__(self, code: ErrorCode | int, message: str, data: Any = None):
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.data = data

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------

@dataclass
class Connection:
    """Per-connection protocol state.

    One stdio server process serves one connection, but the state is an object
    rather than a module global so the tests can drive several.

    SPEC.md 4.1 removed protocol-level sessions and ``Mcp-Session-Id``:
    "Servers that need cross-call state use explicit, server-minted handles
    passed as ordinary tool arguments." Nothing here is guest state; this is
    only what the envelope needs.
    """

    revision: ProtocolRevision = LATEST_REVISION
    #: True once an ``initialize`` request or a ``_meta.protocolVersion`` has
    #: set :attr:`revision`. Before that the server assumes the latest, which
    #: is right for a 2026-07-28 client that opens straight with ``tools/list``.
    negotiated: bool = False
    #: True once a legacy ``initialize`` has been seen. It is authoritative
    #: from then on: a later stray ``_meta.protocolVersion`` does not upgrade a
    #: connection that already handshook, because the client's parser is fixed.
    initialize_seen: bool = False
    initialized_notified: bool = False
    client_info: dict[str, Any] = field(default_factory=dict)
    client_capabilities: dict[str, Any] = field(default_factory=dict)

    @property
    def emits_result_metadata(self) -> bool:
        """Does this connection get ``resultType`` / ``ttlMs`` / ``cacheScope``?

        SPEC.md 4.1 makes them required from 2026-07-28. Emitting them to a
        2025-06-18 client is at best noise and at worst a validation failure,
        so the answer is per-connection.
        """
        return self.revision.value >= METADATA_REVISION.value

    def apply_meta(self, meta: Mapping[str, Any] | None) -> None:
        """Negotiate from a request's ``_meta``, the 2026-07-28 carrier.

        SPEC.md 4.1: "Every request carries its protocol version and client
        capabilities in ``_meta``." Ignored once ``initialize`` has been seen.
        """
        if not meta or self.initialize_seen:
            return
        version = meta.get("protocolVersion")
        if version is None:
            return
        self.revision = negotiate(version)
        self.negotiated = True
        caps = meta.get("clientCapabilities")
        if isinstance(caps, dict):
            self.client_capabilities = caps


# ---------------------------------------------------------------------------
# stdout protection
# ---------------------------------------------------------------------------

def claim_stdout() -> BinaryIO:
    """Take exclusive ownership of fd 1 and point the old fd 1 at stderr.

    Returns the private, unbuffered binary stream that is now the only way to
    reach the JSON-RPC channel. After this call:

    * ``print()``, ``sys.stdout.write`` and any library that writes to
      ``sys.stdout`` go to stderr, because :data:`sys.stdout` is rebound;
    * anything writing to *file descriptor* 1 -- a C extension, a subprocess
      that inherited it -- also goes to stderr, because fd 1 now duplicates
      fd 2.

    The second half is the one that matters here. 80mcp execs cpmemu, dosiz and
    romwbw_emu, all of which write guest output to their own stdout; the
    sandbox captures those through pipes, but a mistake in that plumbing must
    corrupt a log rather than the protocol.
    """
    private_fd = os.dup(1)
    os.dup2(2, 1)
    stream = os.fdopen(private_fd, "wb", buffering=0)
    try:
        sys.stdout.flush()
    except Exception:  # pragma: no cover - a closed stdout is not fatal here
        pass
    sys.stdout = sys.stderr
    return stream


def log(message: str) -> None:
    """Diagnostics go to stderr. There is no other option."""
    try:
        sys.stderr.write(f"{SERVER_NAME}: {message}\n")
        sys.stderr.flush()
    except Exception:  # pragma: no cover
        pass


def _reject_constant(token: str) -> Any:
    """``json.loads``'s hook for ``NaN``/``Infinity``/``-Infinity``.

    Python accepts all three by default and RFC 8259 defines none of them. A
    server that parses them will sooner or later re-emit one -- an echoed
    request ``id`` is enough -- and hand a strict client a line it cannot
    parse. Refusing at the door is cheaper than sanitising every value that
    might have come from one.
    """
    raise ValueError(f"{token} is not a JSON value (RFC 8259 defines no such token)")


# ---------------------------------------------------------------------------
# The server loop
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServerIdentity:
    """What ``initialize`` and ``server/discover`` report about this server."""

    name: str = SERVER_NAME
    version: str = __version__
    title: str | None = "80mcp"
    instructions: str | None = None
    #: MCP capabilities object. 80mcp advertises tools and resources; SPEC.md
    #: 4.1 records that Roots, Sampling and Logging are deprecated and that
    #: ``ping`` and ``logging/setLevel`` are removed, so none is advertised.
    capabilities: Mapping[str, Any] = field(
        default_factory=lambda: {"tools": {"listChanged": False}}
    )

    def server_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.title:
            info["title"] = self.title
        return info


#: A method handler takes ``(params, connection)`` and returns the *result*
#: object. It raises :class:`JsonRpcError` for a protocol-level failure. For a
#: notification it returns ``None`` and nothing is written.
MethodHandler = Callable[[Mapping[str, Any], Connection], Any]


class JsonRpcServer:
    """Newline-delimited JSON-RPC 2.0 over a byte stream pair.

    This class owns the envelope: framing, ids, error objects, and the three
    protocol methods that are not tools (``initialize``,
    ``notifications/initialized``, ``server/discover``). Everything else is
    supplied as ``methods`` by :mod:`eightymcp.server`, which keeps the tool
    registry out of the transport.
    """

    def __init__(
        self,
        identity: ServerIdentity,
        methods: Mapping[str, MethodHandler],
        *,
        connection: Connection | None = None,
    ):
        self.identity = identity
        self.connection = connection or Connection()
        self._methods: dict[str, MethodHandler] = dict(methods)
        # The three protocol methods are registered here rather than in
        # server.py: they are envelope, not surface, and two of the three exist
        # only for revisions SPEC.md does not target.
        self._methods.setdefault("initialize", self._handle_initialize)
        self._methods.setdefault("notifications/initialized", self._handle_initialized)
        self._methods.setdefault("server/discover", self._handle_discover)

    # -- protocol methods -------------------------------------------------

    def _handle_initialize(
        self, params: Mapping[str, Any], conn: Connection
    ) -> dict[str, Any]:
        """The legacy handshake, removed in 2026-07-28 and still sent by every
        shipping client.

        The reply pins the connection to the negotiated revision. From here on
        ``_meta.protocolVersion`` is ignored on this connection
        (:attr:`Connection.initialize_seen`), because the client that sent
        ``initialize`` has a parser built for the revision it just agreed to.
        """
        requested = params.get("protocolVersion")
        revision = negotiate(requested)
        conn.revision = revision
        conn.negotiated = True
        conn.initialize_seen = True
        info = params.get("clientInfo")
        if isinstance(info, dict):
            conn.client_info = info
        caps = params.get("capabilities")
        if isinstance(caps, dict):
            conn.client_capabilities = caps
        log(
            f"initialize: client asked {requested!r}, speaking {revision.value}"
            + ("" if conn.emits_result_metadata else " (no resultType/ttlMs on this connection)")
        )
        result: dict[str, Any] = {
            "protocolVersion": revision.value,
            "capabilities": dict(self.identity.capabilities),
            "serverInfo": self.identity.server_info(),
        }
        if self.identity.instructions:
            result["instructions"] = self.identity.instructions
        # No resultType here even when the negotiated revision is 2026-07-28:
        # a client that sent `initialize` is by definition not a 2026-07-28
        # client, since that revision has no such request.
        return result

    def _handle_initialized(
        self, params: Mapping[str, Any], conn: Connection
    ) -> None:
        """``notifications/initialized``. Nothing to do but note it."""
        conn.initialized_notified = True
        return None

    def _handle_discover(
        self, params: Mapping[str, Any], conn: Connection
    ) -> dict[str, Any]:
        """``server/discover`` -- the 2026-07-28 replacement for the handshake.

        SPEC.md 4.1 names the method and says every request carries its
        protocol version in ``_meta``; it does not give the result shape, so
        this mirrors ``initialize``'s and adds ``protocolVersions`` listing
        everything the server can speak. That list is the honest answer to
        "what do you support", and it costs one array.
        """
        result: dict[str, Any] = {
            "protocolVersion": conn.revision.value,
            "protocolVersions": [r.value for r in SUPPORTED_REVISIONS],
            "capabilities": dict(self.identity.capabilities),
            "serverInfo": self.identity.server_info(),
        }
        if self.identity.instructions:
            result["instructions"] = self.identity.instructions
        return self.decorate_result(result, conn)

    # -- envelope ---------------------------------------------------------

    def decorate_result(
        self, result: dict[str, Any], conn: Connection | None = None
    ) -> dict[str, Any]:
        """Add ``resultType`` when the negotiated revision requires it.

        SPEC.md 4.1: "``resultType`` required on every result. Legal values:
        ``"complete"`` and ``"input_required"``." Appendix B item 6 names a
        third value that is not legal; it is nowhere in this package as a
        quoted literal, because the SPEC.md 6.0 conformance test greps the
        source for it and fails the build on a hit.
        """
        conn = conn or self.connection
        if conn.emits_result_metadata and "resultType" not in result:
            result = {"resultType": ResultType.COMPLETE.value, **result}
        return result

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, message: Any) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC message. Returns the response, or None
        for a notification."""
        if isinstance(message, list):
            # JSON-RPC batching was removed in MCP 2025-06-18 and 80mcp speaks
            # nothing older, so an array is a malformed request rather than a
            # batch to run.
            return self._error_response(
                None,
                JsonRpcError(
                    ErrorCode.INVALID_REQUEST,
                    "JSON-RPC batching is not supported",
                    data={"hint": "send one request object per line"},
                ),
            )
        if not isinstance(message, dict):
            return self._error_response(
                None,
                JsonRpcError(ErrorCode.INVALID_REQUEST, "Request must be a JSON object"),
            )

        request_id = message.get("id")
        is_notification = "id" not in message

        try:
            if message.get("jsonrpc") != "2.0":
                raise JsonRpcError(
                    ErrorCode.INVALID_REQUEST,
                    'Missing or wrong "jsonrpc" member; must be "2.0"',
                    data={"got": message.get("jsonrpc")},
                )
            method = message.get("method")
            if not isinstance(method, str):
                raise JsonRpcError(
                    ErrorCode.INVALID_REQUEST, 'Missing "method"'
                )
            params = message.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise JsonRpcError(
                    ErrorCode.INVALID_PARAMS,
                    '"params" must be an object',
                    data={"method": method},
                )

            # 2026-07-28 carries the protocol version and client capabilities
            # in _meta on every request (SPEC.md 4.1).
            meta = params.get("_meta")
            if not isinstance(meta, dict):
                meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else None
            self.connection.apply_meta(meta)

            handler = self._methods.get(method)
            if handler is None:
                raise JsonRpcError(
                    ErrorCode.METHOD_NOT_FOUND,
                    f"Unknown method: {method}",
                    data={"method": method, "known": sorted(self._methods)},
                )
            result = handler(params, self.connection)
        except JsonRpcError as exc:
            if is_notification:
                log(f"error in notification {message.get('method')!r}: {exc.message}")
                return None
            return self._error_response(request_id, exc)
        except Exception as exc:  # a bug in the server, not in the request
            traceback.print_exc(file=sys.stderr)
            if is_notification:
                return None
            return self._error_response(
                request_id,
                JsonRpcError(
                    ErrorCode.INTERNAL_ERROR,
                    f"{type(exc).__name__}: {exc}",
                    data={"method": message.get("method")},
                ),
            )

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result if result is not None else {}}

    def _error_response(
        self, request_id: Any, exc: JsonRpcError
    ) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": exc.to_json()}

    # -- framing ----------------------------------------------------------

    @staticmethod
    def encode(obj: Any) -> bytes:
        """One compact JSON value, one newline. No pretty-printing on the
        wire: a newline inside a framed message ends the message.

        **This function must never raise.** Measured: a client sending the
        perfectly legal JSON text ``{"profile":"\\ud800"}`` -- a lone
        surrogate, which RFC 8259 permits as a ``\\u`` escape and which
        ``json.loads`` accepts -- got that string echoed into the ``got`` field
        of a schema violation, and ``str.encode("utf-8")`` then raised
        ``UnicodeEncodeError`` inside :meth:`serve`, killing the server process
        with no reply written. One request, permanent desync. The same string
        can arrive from a filesystem name decoded with ``surrogateescape``.

        The fallback is ``ensure_ascii=True``, which emits the surrogate as the
        ``\\udXXX`` escape the client itself sent. That is the same JSON value,
        it is pure ASCII so the encode cannot fail, and it costs nothing on the
        normal path because it is only reached after one has failed.

        ``allow_nan=False`` is the other half of the same rule, in the other
        direction. Python's ``json`` emits the bare tokens ``NaN`` and
        ``Infinity`` by default, and neither exists in RFC 8259 -- measured, a
        request with ``"id": NaN`` came back as
        ``{"jsonrpc":"2.0","id":NaN,"result":...}``, which Go, Rust and a
        browser's ``JSON.parse`` all reject, and a rejected frame is a lost id.
        With the flag this raises instead, and :meth:`_write_response` turns
        the raise into a well-formed error frame.
        """
        text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError:
            data = json.dumps(
                obj, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            ).encode("utf-8")
        assert b"\n" not in data
        return data + b"\n"

    @staticmethod
    def _write_all(out: BinaryIO, data: bytes) -> None:
        """Write every byte or raise.

        :func:`claim_stdout` hands back an unbuffered ``FileIO``, whose
        ``write`` performs exactly one ``write(2)`` and returns the count. A
        short write on a large frame -- a 4 MB inline ``content_b64`` is inside
        the schema's ``max_kb`` ceiling -- would truncate the frame and desync
        the client forever, so the count is checked rather than assumed.
        """
        view = memoryview(data)
        while view:
            n = out.write(view)
            if n is None:  # a non-blocking raw stream with nothing to give
                continue
            if n <= 0:
                raise OSError("short write to the JSON-RPC channel")
            view = view[n:]

    def write(self, out: BinaryIO, obj: Any) -> None:
        self._write_all(out, self.encode(obj))
        try:
            out.flush()
        except (AttributeError, ValueError):  # unbuffered fdopen has no flush
            pass

    def serve(self, inp: BinaryIO, out: BinaryIO) -> int:
        """Read framed requests from ``inp`` until EOF, writing to ``out``.

        ``out`` must be the private stream from :func:`claim_stdout`, not
        ``sys.stdout``.
        """
        for raw in inp:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line, parse_constant=_reject_constant)
            except ValueError as exc:
                # JSONDecodeError is a ValueError; so is what
                # _reject_constant raises for the three tokens Python's parser
                # accepts and RFC 8259 does not.
                where = (
                    f" at position {exc.pos}" if isinstance(exc, json.JSONDecodeError) else ""
                )
                detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
                self._write_response(
                    out,
                    self._error_response(
                        None,
                        JsonRpcError(ErrorCode.PARSE_ERROR, f"Invalid JSON: {detail}{where}"),
                    ),
                )
                continue
            response = self.dispatch(message)
            if response is not None:
                self._write_response(out, response)
        return 0

    def _write_response(self, out: BinaryIO, response: dict[str, Any]) -> None:
        """Write one response, or a frame saying why it could not be written.

        Serialising a result is the last place a bug can still reach the
        client, and the client is a state machine waiting for an id. Dying here
        -- which is what an un-caught ``UnicodeEncodeError`` did -- costs the
        whole session; answering with an error for that id costs one call. The
        replacement frame is built from the id and two ASCII literals, so the
        only way it can fail is a dead pipe, which nothing can fix.
        """
        try:
            self.write(out, response)
            return
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            log(f"could not serialise a response: {type(exc).__name__}: {exc}")
        request_id = response.get("id")
        if not isinstance(request_id, (str, int, float)) or isinstance(request_id, bool):
            request_id = None
        elif isinstance(request_id, float) and request_id != request_id:  # NaN
            request_id = None
        elif isinstance(request_id, float) and request_id in (float("inf"), float("-inf")):
            request_id = None
        try:
            self.write(
                out,
                self._error_response(
                    request_id,
                    JsonRpcError(
                        ErrorCode.INTERNAL_ERROR,
                        "the result could not be serialised as JSON-RPC",
                        data={"hint": "the server logged a traceback to stderr"},
                    ),
                ),
            )
        except Exception:  # pragma: no cover - the channel itself is gone
            traceback.print_exc(file=sys.stderr)
