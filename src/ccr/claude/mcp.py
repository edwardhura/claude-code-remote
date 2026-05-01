"""In-process MCP server exposing one tool: ``ccr_permission_prompt``.

Architecture:

* The :class:`McpPermissionServer` runs on the same asyncio loop as the bot
  and the :class:`~ccr.claude.manager.SessionManager`. It owns one
  :class:`mcp.server.Server` instance, a deterministic
  ``<data_dir>/.ccr-mcp-config.json`` file, and a ``request_id → Future`` map.
* When claude needs a permission decision, the MCP tool dispatch lands in
  :meth:`McpPermissionServer._on_tool_call`, which mints an 8-hex
  ``request_id``, registers an :class:`asyncio.Future`, publishes a
  :class:`~ccr.claude.events.McpPermissionRequest` envelope to the
  :class:`~ccr.events.EventBus`, and awaits the Future with a timeout.
* The Telegram callback handler resolves the Future via
  :meth:`SessionManager.resolve_permission` → :meth:`McpPermissionServer.resolve`.
* On a 120s default timeout the Future is resolved internally with a
  fail-closed deny payload.

Key invariants:

* Future-keying is a manager-minted UUID4 8-hex prefix to keep Telegram
  ``callback_data`` under the 64-byte cap (re-mint on collision).
* The bus envelope omits the ``seq`` key (synthetic, not in JSONL).
* :class:`McpPermissionRequest` is published BEFORE the Future is awaited so
  a near-instantaneous resolve cannot race the publish.
* Both timeout and :meth:`cancel_pending` produce the same deny payload —
  fail-closed is the safe default for permission gating.

The relay shim :mod:`ccr.claude.mcp_relay` is the only child process this
module owns; it is pure transport with no business logic. claude spawns it
according to the ``--mcp-config`` JSON written by :meth:`start`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import structlog
from mcp.server import Server
from mcp.types import Tool

from ccr.claude.events import McpPermissionRequest

if TYPE_CHECKING:
    from anyio.abc import SocketListener

    from ccr.events import EventBus


log = structlog.get_logger(__name__)


_TOOL_NAME = "ccr_permission_prompt"
_SERVER_NAME = "ccr"
_REQUEST_ID_HEX_LEN = 8
_REQUEST_ID_RETRY_LIMIT = 32
_DEFAULT_TIMEOUT_SECONDS = 120.0
_PLACEHOLDER_SESSION_ID = uuid.UUID(int=0)


class McpServerStartError(Exception):
    """Raised when :meth:`McpPermissionServer.start` cannot bring the server up."""


def _prepare_socket_path(sock_path: str) -> None:
    """Remove a stale socket file and ensure the parent directory exists."""
    p = Path(sock_path)
    with contextlib.suppress(FileNotFoundError):
        p.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)


def _format_timeout_message(timeout_seconds: float) -> str:
    if timeout_seconds == int(timeout_seconds):
        return f"Timed out — no paired user responded within {int(timeout_seconds)}s"
    return f"Timed out — no paired user responded within {timeout_seconds}s"


class McpPermissionServer:
    """In-process MCP server with one tool: ``ccr_permission_prompt``.

    Lifetime equals :class:`SessionManager` lifetime: started lazily on the
    first ``new_session`` / ``continue_session`` and stopped from
    :meth:`SessionManager.serve`'s ``finally`` block.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        data_dir: Path | None = None,
    ) -> None:
        self._bus = bus
        self._timeout_seconds = timeout_seconds
        self._data_dir = data_dir if data_dir is not None else Path("./data")
        self._server = self._build_server()
        self._futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._sessions: dict[str, uuid.UUID] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._config_path: Path | None = None
        self._listener: SocketListener | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._current_session_id: uuid.UUID | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle.
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Idempotently bring the server up.

        Writes ``<data_dir>/.ccr-mcp-config.json`` (deterministic path) and
        opens a Unix socket listener at ``<data_dir>/.ccr-mcp.sock``. The
        in-memory test path bypasses both — tests drive
        :attr:`server` directly via
        :func:`mcp.shared.memory.create_connected_server_and_client_session`.

        Raises:
            McpServerStartError: when the socket cannot be opened or the
                config file cannot be written. The original cause is
                chained.
        """
        async with self._lock:
            if self._started:
                return
            try:
                self._write_config_file()
                self._listener = await self._open_socket_listener()
            except Exception as exc:
                self._cleanup_config_file()
                if self._listener is not None:
                    with contextlib.suppress(Exception):
                        await self._listener.aclose()
                    self._listener = None
                message = f"Failed to start MCP permission server: {exc}"
                raise McpServerStartError(message) from exc

            self._listener_task = asyncio.create_task(
                self._serve_listener(),
                name="mcp-permission-listener",
            )
            self._started = True

    async def stop(self) -> None:
        """Tear the server down.

        Cancels every outstanding Future with a deny payload, removes the
        config file, and stops the socket listener. Idempotent.
        """
        async with self._lock:
            was_started = self._started
            self._started = False

        for request_id in list(self._futures.keys()):
            await self._resolve_internal(
                request_id,
                {
                    "behavior": "deny",
                    "message": "MCP server stopped",
                },
            )

        if self._listener is not None:
            with contextlib.suppress(Exception):
                await self._listener.aclose()
            self._listener = None

        if self._listener_task is not None and not self._listener_task.done():
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener_task
        self._listener_task = None

        if was_started:
            self._cleanup_config_file()

    # ------------------------------------------------------------------ #
    # Public surface.
    # ------------------------------------------------------------------ #

    @property
    def claude_argv(self) -> list[str]:
        """Tokens to splice into claude's argv.

        The ``mcp__<server>__<tool>`` form is Claude Code's documented
        resolution rule for tools served by an MCP server (see
        ``https://docs.claude.com/en/docs/claude-code/mcp`` —
        ``--permission-prompt-tool`` accepts the same form documented for
        built-in MCP tool references). The MCP Python SDK itself does not
        emit this form (the separator is a Claude Code convention, not an
        MCP-SDK concept; the SDK only validates tool names against
        SEP-986 at ``mcp/shared/tool_name_validation.py``, which permits
        ``[A-Za-z0-9._-]``).
        """
        config_path_str = (
            str(self._config_path)
            if self._config_path is not None
            else str(
                self._config_file_path(),
            )
        )
        return [
            "--permission-prompt-tool",
            f"mcp__{_SERVER_NAME}__{_TOOL_NAME}",
            "--mcp-config",
            config_path_str,
        ]

    def is_pending(self, request_id: str) -> bool:
        """Return ``True`` iff ``request_id`` has a registered, unresolved Future."""
        future = self._futures.get(request_id)
        return future is not None and not future.done()

    async def resolve(
        self,
        request_id: str,
        decision: dict[str, Any],
    ) -> bool:
        """Set the Future for ``request_id`` to ``decision``.

        Returns ``False`` if ``request_id`` is unknown or already resolved
        (the bot maps both to "Stale prompt" — see
        :func:`ccr.bot.handlers.permission.cb_permission`).
        """
        return await self._resolve_internal(request_id, decision)

    async def cancel_pending(self, session_id: uuid.UUID) -> int:
        """Resolve every outstanding Future for ``session_id`` with a deny.

        Returns the number cancelled. Called from
        :meth:`SessionManager._teardown_locked` so claude's tool dispatch
        sees a deny rather than a hung MCP socket when the session is
        torn down with a permission still in flight.
        """
        cancelled = 0
        request_ids = [rid for rid, sid in self._sessions.items() if sid == session_id]
        for rid in request_ids:
            ok = await self._resolve_internal(
                rid,
                {
                    "behavior": "deny",
                    "message": "Session torn down",
                },
            )
            if ok:
                cancelled += 1
        return cancelled

    def set_current_session(self, session_id: uuid.UUID | None) -> None:
        """Bind subsequent :meth:`_on_tool_call` invocations to ``session_id``.

        Set by :class:`SessionManager` on each ``new_session`` /
        ``continue_session``. Cleared in ``_teardown_locked`` after
        :meth:`cancel_pending`.
        """
        self._current_session_id = session_id

    # ------------------------------------------------------------------ #
    # Internals.
    # ------------------------------------------------------------------ #

    @property
    def server(self) -> Server[Any, Any]:
        """The underlying ``mcp.server.Server`` (for tests / in-memory drivers)."""
        return self._server

    def _build_server(self) -> Server[Any, Any]:
        srv: Server[Any, Any] = Server(_SERVER_NAME)

        async def _list_tools() -> list[Tool]:
            return [
                Tool(
                    name=_TOOL_NAME,
                    description=(
                        "Request permission from a paired Telegram user before "
                        "performing a tool call. Returns either "
                        "{'behavior': 'allow', 'updatedInput': ...} or "
                        "{'behavior': 'deny', 'message': ...}."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string"},
                            "input": {"type": "object"},
                        },
                        "required": ["tool_name", "input"],
                    },
                ),
            ]

        async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if name != _TOOL_NAME:
                return {
                    "behavior": "deny",
                    "message": f"Unknown tool: {name}",
                }
            tool_name = str(arguments.get("tool_name", ""))
            tool_input_raw = arguments.get("input")
            tool_input: dict[str, Any] = tool_input_raw if isinstance(tool_input_raw, dict) else {}
            return await self._on_tool_call(tool_name, tool_input)

        srv.list_tools()(_list_tools)  # type: ignore[no-untyped-call]
        srv.call_tool()(_call_tool)
        return srv

    async def _on_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any]:
        """Mint id, register Future, publish envelope, await resolution.

        This event is NOT a JSONL line — it lives on the bus only. The
        ``seq`` key is omitted from the bus payload so SSE consumers do
        not double-bump their replay cursor.
        """
        request_id = self._mint_request_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        session_id = (
            self._current_session_id
            if self._current_session_id is not None
            else _PLACEHOLDER_SESSION_ID
        )
        self._futures[request_id] = future
        self._sessions[request_id] = session_id

        envelope = McpPermissionRequest(
            request_id=request_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )

        await self._bus.publish(
            "session.event",
            {
                "session_id": session_id,
                "event": envelope,
            },
        )

        try:
            return await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except TimeoutError:
            log.warning(
                "mcp_permission_server.timeout",
                request_id=request_id,
                tool_name=tool_name,
                timeout_seconds=self._timeout_seconds,
            )
            return {
                "behavior": "deny",
                "message": _format_timeout_message(self._timeout_seconds),
            }
        finally:
            self._futures.pop(request_id, None)
            self._sessions.pop(request_id, None)

    def _mint_request_id(self) -> str:
        for _ in range(_REQUEST_ID_RETRY_LIMIT):
            candidate = secrets.token_hex(_REQUEST_ID_HEX_LEN // 2)
            if candidate not in self._futures:
                return candidate
        return uuid.uuid4().hex[:_REQUEST_ID_HEX_LEN]

    async def _resolve_internal(
        self,
        request_id: str,
        decision: dict[str, Any],
    ) -> bool:
        future = self._futures.get(request_id)
        if future is None or future.done():
            return False
        try:
            future.set_result(decision)
        except asyncio.InvalidStateError:
            return False
        return True

    # ------------------------------------------------------------------ #
    # Transport: socket listener for the relay shim.
    # ------------------------------------------------------------------ #

    def _config_file_path(self) -> Path:
        return self._data_dir / ".ccr-mcp-config.json"

    def _socket_path(self) -> str:
        # Unix socket paths cap at ~104 chars on macOS; pytest tmp paths
        # blow that on most platforms. Hash the data_dir into a short,
        # deterministic path under the system temp dir so the path stays
        # stable across the parent's lifetime but still under the cap.
        digest = hashlib.sha256(str(self._data_dir.resolve()).encode("utf-8")).hexdigest()[:16]
        return str(Path(tempfile.gettempdir()) / f"ccr-mcp-{digest}.sock")

    def _write_config_file(self) -> None:
        path = self._config_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        config: dict[str, Any] = {
            "mcpServers": {
                _SERVER_NAME: {
                    "command": "python",
                    "args": ["-m", "ccr.claude.mcp_relay", self._socket_path()],
                },
            },
        }
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self._config_path = path

    def _cleanup_config_file(self) -> None:
        if self._config_path is None:
            return
        with contextlib.suppress(FileNotFoundError, OSError):
            self._config_path.unlink()
        self._config_path = None

    async def _open_socket_listener(self) -> SocketListener:
        sock_path = self._socket_path()
        # Synchronous filesystem prep — these are tiny stat / unlink calls,
        # not blocking I/O of substance, but the lint rule prefers anyio.
        await anyio.to_thread.run_sync(_prepare_socket_path, sock_path)
        return await anyio.create_unix_listener(sock_path)

    async def _serve_listener(self) -> None:
        if self._listener is None:
            return
        try:
            await self._listener.serve(self._handle_relay_connection)
        except (asyncio.CancelledError, anyio.ClosedResourceError):
            return
        except Exception:
            log.exception("mcp_permission_server.listener_error")

    async def _handle_relay_connection(self, stream: object) -> None:  # pragma: no cover
        """Bridge one relay's socket stream into ``Server.run``.

        Production-path only — unit tests drive ``self._server`` via
        :func:`mcp.shared.memory.create_connected_server_and_client_session`.
        Closing the stream signals end-of-relay so the listener task can
        accept subsequent connections.
        """
        with contextlib.suppress(Exception):
            await stream.aclose()  # type: ignore[attr-defined]


__all__ = ["McpPermissionServer", "McpServerStartError"]
