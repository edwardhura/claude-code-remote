"""Tests for ``ccr.claude.mcp.McpPermissionServer``.

Drives the in-process server with the SDK's
``create_connected_server_and_client_session`` helper (in-memory streams)
in place of the real claude binary. Asserts:

* a tool call publishes the right :class:`McpPermissionRequest` envelope to
  the bus,
* :meth:`McpPermissionServer.resolve` lets the in-flight tool return the
  matching ``{"behavior": ..., "updatedInput": ...}`` payload,
* unknown / already-resolved request_ids are rejected,
* :meth:`cancel_pending` resolves outstanding Futures with deny,
* the timeout path returns the canned deny payload,
* concurrent tool calls do not share state,
* the ``claude_argv`` property returns the canonical token list with the
  deterministic ``--mcp-config`` path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.shared.message import SessionMessage

from ccr.claude.events import McpPermissionRequest
from ccr.claude.mcp import McpPermissionServer
from ccr.events import EventBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


async def _collect_first_envelope(
    bus: EventBus,
    captured: list[McpPermissionRequest],
    *,
    expected: int = 1,
) -> None:
    async for payload in bus.subscribe("session.event"):
        if not isinstance(payload, dict):
            continue
        event = payload.get("event")
        if isinstance(event, McpPermissionRequest):
            captured.append(event)
            if len(captured) >= expected:
                return


def _tool_result_payload(result: Any) -> dict[str, Any]:
    """Extract the structured payload out of an MCP CallToolResult."""
    if getattr(result, "structuredContent", None):
        return dict(result.structuredContent)
    # Fallback: parse the JSON-encoded text content.
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return dict(json.loads(text))
    msg = "no structured content on tool result"
    raise AssertionError(msg)


async def _client_for(server: McpPermissionServer) -> AsyncIterator[ClientSession]:
    """Yield a ``ClientSession`` connected to ``server`` over in-memory streams."""
    async with create_connected_server_and_client_session(server.server) as client:
        yield client


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #


async def test_mcp_tool_call_publishes_envelope_and_resolves_with_user_choice(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=5.0, data_dir=tmp_path)
    server.set_current_session(_SESSION_ID)

    captured: list[McpPermissionRequest] = []
    collector = asyncio.create_task(_collect_first_envelope(bus, captured))
    await asyncio.sleep(0)

    async with create_connected_server_and_client_session(server.server) as client:

        async def _resolver() -> dict[str, Any]:
            # Wait until the envelope has been published before resolving.
            await collector
            assert len(captured) == 1
            envelope = captured[0]
            ok = await server.resolve(
                envelope.request_id,
                {"behavior": "allow", "updatedInput": {"echo": "ok"}},
            )
            assert ok is True
            return {"resolved": True}

        resolver_task = asyncio.create_task(_resolver())
        result = await client.call_tool(
            "ccr_permission_prompt",
            {"tool_name": "Bash", "input": {"cmd": "ls"}},
        )
        await resolver_task

    assert len(captured) == 1
    env = captured[0]
    assert env.tool_name == "Bash"
    assert env.tool_input == {"cmd": "ls"}
    assert env.session_id == _SESSION_ID
    assert env.options == ["approve", "deny"]
    assert len(env.request_id) == 8

    payload = _tool_result_payload(result)
    assert payload == {"behavior": "allow", "updatedInput": {"echo": "ok"}}


async def test_mcp_tool_call_deny_returns_deny_payload(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=5.0, data_dir=tmp_path)
    server.set_current_session(_SESSION_ID)

    captured: list[McpPermissionRequest] = []
    collector = asyncio.create_task(_collect_first_envelope(bus, captured))
    await asyncio.sleep(0)

    async with create_connected_server_and_client_session(server.server) as client:

        async def _resolver() -> None:
            await collector
            envelope = captured[0]
            await server.resolve(
                envelope.request_id,
                {"behavior": "deny", "message": "no thanks"},
            )

        resolver_task = asyncio.create_task(_resolver())
        result = await client.call_tool(
            "ccr_permission_prompt",
            {"tool_name": "Write", "input": {"path": "/tmp/x"}},
        )
        await resolver_task

    payload = _tool_result_payload(result)
    assert payload == {"behavior": "deny", "message": "no thanks"}


async def test_mcp_tool_call_unknown_request_id_resolve_returns_false(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=5.0, data_dir=tmp_path)
    ok = await server.resolve("deadbeef", {"behavior": "allow", "updatedInput": None})
    assert ok is False
    assert server.is_pending("deadbeef") is False


async def test_mcp_tool_call_double_resolve_returns_false_second_time(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=5.0, data_dir=tmp_path)
    server.set_current_session(_SESSION_ID)

    captured: list[McpPermissionRequest] = []
    collector = asyncio.create_task(_collect_first_envelope(bus, captured))
    await asyncio.sleep(0)

    async with create_connected_server_and_client_session(server.server) as client:

        async def _resolver() -> tuple[bool, bool]:
            await collector
            request_id = captured[0].request_id
            first = await server.resolve(
                request_id,
                {"behavior": "allow", "updatedInput": None},
            )
            second = await server.resolve(
                request_id,
                {"behavior": "deny", "message": "late"},
            )
            return first, second

        resolver_task = asyncio.create_task(_resolver())
        await client.call_tool(
            "ccr_permission_prompt",
            {"tool_name": "Edit", "input": {"path": "/x"}},
        )
        first, second = await resolver_task

    assert first is True
    assert second is False


async def test_mcp_tool_call_timeout_returns_deny(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=0.05, data_dir=tmp_path)
    server.set_current_session(_SESSION_ID)

    async with create_connected_server_and_client_session(server.server) as client:
        result = await client.call_tool(
            "ccr_permission_prompt",
            {"tool_name": "Bash", "input": {"cmd": "rm -rf /"}},
        )

    payload = _tool_result_payload(result)
    assert payload["behavior"] == "deny"
    assert "Timed out" in payload["message"]


async def test_mcp_cancel_pending_resolves_session_futures_with_deny(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=5.0, data_dir=tmp_path)
    server.set_current_session(_SESSION_ID)

    captured: list[McpPermissionRequest] = []
    collector = asyncio.create_task(_collect_first_envelope(bus, captured))
    await asyncio.sleep(0)

    async with create_connected_server_and_client_session(server.server) as client:

        async def _canceller() -> int:
            await collector
            return await server.cancel_pending(_SESSION_ID)

        cancel_task = asyncio.create_task(_canceller())
        result = await client.call_tool(
            "ccr_permission_prompt",
            {"tool_name": "Bash", "input": {}},
        )
        cancelled = await cancel_task

    assert cancelled == 1
    payload = _tool_result_payload(result)
    assert payload == {"behavior": "deny", "message": "Session torn down"}
    assert server.is_pending(captured[0].request_id) is False


async def test_concurrent_tool_calls_share_no_state(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=5.0, data_dir=tmp_path)
    server.set_current_session(_SESSION_ID)

    captured: list[McpPermissionRequest] = []
    collector = asyncio.create_task(_collect_first_envelope(bus, captured, expected=2))
    await asyncio.sleep(0)

    async with (
        create_connected_server_and_client_session(server.server) as client_a,
        create_connected_server_and_client_session(server.server) as client_b,
    ):

        async def _resolver() -> None:
            await collector
            first, second = sorted(captured, key=lambda e: e.tool_name)
            # Resolve in reverse order — the second envelope first.
            await server.resolve(
                second.request_id,
                {"behavior": "deny", "message": "no"},
            )
            await server.resolve(
                first.request_id,
                {"behavior": "allow", "updatedInput": None},
            )

        resolver_task = asyncio.create_task(_resolver())
        results = await asyncio.gather(
            client_a.call_tool(
                "ccr_permission_prompt",
                {"tool_name": "AAA", "input": {}},
            ),
            client_b.call_tool(
                "ccr_permission_prompt",
                {"tool_name": "BBB", "input": {}},
            ),
        )
        await resolver_task

    assert len(captured) == 2
    assert {e.tool_name for e in captured} == {"AAA", "BBB"}
    payload_a = _tool_result_payload(results[0])
    payload_b = _tool_result_payload(results[1])
    assert payload_a == {"behavior": "allow", "updatedInput": None}
    assert payload_b == {"behavior": "deny", "message": "no"}


async def test_tool_result_is_single_text_block_with_compact_json_and_no_structured_content(
    tmp_path: Path,
) -> None:
    """Regression: Claude Code's --permission-prompt-tool rejects results that

    are not a single text content block with a compact JSON-encoded decision.
    The SDK's dict-return path emits pretty-printed JSON AND populates
    structuredContent, both of which trip Claude Code's parser. Verify the
    wire shape: exactly one TextContent block, type="text", text is a compact
    JSON string round-tripping to the decision dict, structuredContent unset.
    """
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=5.0, data_dir=tmp_path)
    server.set_current_session(_SESSION_ID)

    captured: list[McpPermissionRequest] = []
    collector = asyncio.create_task(_collect_first_envelope(bus, captured))
    await asyncio.sleep(0)

    async with create_connected_server_and_client_session(server.server) as client:

        async def _resolver() -> None:
            await collector
            await server.resolve(
                captured[0].request_id,
                {"behavior": "allow", "updatedInput": {"cmd": "ls"}},
            )

        resolver_task = asyncio.create_task(_resolver())
        result = await client.call_tool(
            "ccr_permission_prompt",
            {"tool_name": "Bash", "input": {"cmd": "ls"}},
        )
        await resolver_task

    assert getattr(result, "structuredContent", None) is None, (
        "Claude Code rejects results with structuredContent set"
    )
    assert len(result.content) == 1, "expected exactly one content block"
    block = result.content[0]
    assert block.type == "text"
    assert isinstance(block.text, str)
    assert "\n" not in block.text, "text must be compact JSON, not pretty-printed"
    assert json.loads(block.text) == {
        "behavior": "allow",
        "updatedInput": {"cmd": "ls"},
    }


async def test_argv_property_includes_permission_prompt_tool_and_mcp_config(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, data_dir=tmp_path)
    argv = server.claude_argv
    assert argv[0] == "--permission-prompt-tool"
    assert argv[1] == "mcp__ccr__ccr_permission_prompt"
    assert argv[2] == "--mcp-config"
    assert argv[3].endswith(".ccr-mcp-config.json")
    assert argv[3].startswith(str(tmp_path))


async def test_start_writes_config_file_with_mcp_servers_ccr_key(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, data_dir=tmp_path)
    try:
        await server.start()
        config_path = tmp_path / ".ccr-mcp-config.json"
        assert config_path.exists()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "mcpServers" in config
        assert "ccr" in config["mcpServers"]
        entry = config["mcpServers"]["ccr"]
        assert entry["command"] == "python"
        assert entry["args"][:2] == ["-m", "ccr.claude.mcp_relay"]
    finally:
        await server.stop()


async def test_start_is_idempotent_and_stop_removes_config(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    server = McpPermissionServer(bus=bus, data_dir=tmp_path)
    config_path = tmp_path / ".ccr-mcp-config.json"
    await server.start()
    await server.start()
    assert config_path.exists()
    await server.stop()
    assert not config_path.exists()
    # Idempotent stop on an already-stopped server.
    await server.stop()


def test_mcp_permission_request_is_not_in_claude_event_union() -> None:
    """The synthetic envelope must not be a member of the parsed JSONL union."""
    from ccr.claude import events as evmod

    # ClaudeEvent is the type alias of the JSONL union — assert
    # McpPermissionRequest is not one of its members.
    members = getattr(evmod.ClaudeEvent, "__args__", ())
    assert McpPermissionRequest not in members


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [
        (120.0, "Timed out — no paired user responded within 120s"),
        (0.5, "Timed out — no paired user responded within 0.5s"),
    ],
)
def test_format_timeout_message(timeout: float, expected: str) -> None:
    from ccr.claude.mcp import _format_timeout_message  # type: ignore[attr-defined]

    assert _format_timeout_message(timeout) == expected


# --------------------------------------------------------------------------- #
# Unix-socket transport integration test (CCR-028).
#
# Exercises the real bridge: the listener accepts an inbound asyncio Unix
# connection, frames raw bytes into JSON-RPC messages, and runs an MCP
# ``Server.run`` against the resulting object streams. The client side wraps
# the asyncio reader/writer into the same memory streams ``ClientSession``
# uses with the in-memory transport.
# --------------------------------------------------------------------------- #


async def _stdio_pump_socket_to_messages(
    reader: asyncio.StreamReader,
    sender: anyio.streams.memory.MemoryObjectSendStream[SessionMessage | Exception],
) -> None:
    async with sender:
        buffer = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        msg = mcp_types.JSONRPCMessage.model_validate_json(line)
                    except Exception as exc:  # noqa: BLE001
                        await sender.send(exc)
                        continue
                    await sender.send(SessionMessage(msg))
        except (asyncio.CancelledError, ConnectionError):
            return


async def _stdio_pump_messages_to_socket(
    receiver: anyio.streams.memory.MemoryObjectReceiveStream[SessionMessage],
    writer: asyncio.StreamWriter,
) -> None:
    async with receiver:
        try:
            async for session_message in receiver:
                payload = session_message.message.model_dump_json(
                    by_alias=True,
                    exclude_none=True,
                )
                writer.write(payload.encode("utf-8") + b"\n")
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError):
            return


@contextlib.asynccontextmanager
async def _client_over_unix_socket(socket_path: str) -> AsyncIterator[ClientSession]:
    """Connect via asyncio Unix socket and yield an initialized ClientSession."""
    reader, writer = await asyncio.open_unix_connection(path=socket_path)

    server_to_client_send, server_to_client_recv = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](0)
    client_to_server_send, client_to_server_recv = anyio.create_memory_object_stream[
        SessionMessage
    ](0)

    pump_in = asyncio.create_task(_stdio_pump_socket_to_messages(reader, server_to_client_send))
    pump_out = asyncio.create_task(_stdio_pump_messages_to_socket(client_to_server_recv, writer))

    try:
        async with ClientSession(
            read_stream=server_to_client_recv,
            write_stream=client_to_server_send,
        ) as session:
            await session.initialize()
            yield session
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()
        for task in (pump_in, pump_out):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await server_to_client_recv.aclose()
        with contextlib.suppress(Exception):
            await client_to_server_send.aclose()


async def test_bridge_unix_socket_roundtrip(tmp_path: Path) -> None:
    """End-to-end: relay → bridge → Server.run → tool call → resolve → reply."""
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=5.0, data_dir=tmp_path)
    server.set_current_session(_SESSION_ID)

    captured: list[McpPermissionRequest] = []
    collector = asyncio.create_task(_collect_first_envelope(bus, captured))
    await asyncio.sleep(0)

    try:
        await server.start()

        # F3: socket file mode is 0o700 after start().
        sock_path = server._socket_path()  # noqa: SLF001 — test asserts on the implementation detail
        mode = Path(sock_path).stat().st_mode & 0o777  # noqa: ASYNC240 — single sync stat is fine in tests
        assert mode == 0o700, f"expected 0o700, got 0o{mode:o}"

        async def _resolver() -> None:
            await collector
            assert len(captured) == 1
            ok = await server.resolve(
                captured[0].request_id,
                {"behavior": "allow", "updatedInput": {"cmd": "echo hi"}},
            )
            assert ok is True

        resolver_task = asyncio.create_task(_resolver())

        async with _client_over_unix_socket(sock_path) as client:
            result = await client.call_tool(
                "ccr_permission_prompt",
                {"tool_name": "Bash", "input": {"cmd": "echo"}},
            )

        await resolver_task

        assert len(captured) == 1
        envelope = captured[0]
        assert envelope.tool_name == "Bash"
        assert envelope.tool_input == {"cmd": "echo"}
        assert envelope.session_id == _SESSION_ID

        payload = _tool_result_payload(result)
        assert payload == {"behavior": "allow", "updatedInput": {"cmd": "echo hi"}}
    finally:
        await server.stop()
        if not collector.done():
            collector.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await collector


async def test_socket_listener_chmods_socket_to_owner_only(tmp_path: Path) -> None:
    """F3: ``start()`` tightens the socket file to mode ``0o700``."""
    bus = EventBus()
    server = McpPermissionServer(bus=bus, timeout_seconds=5.0, data_dir=tmp_path)
    try:
        await server.start()
        sock_path = server._socket_path()  # noqa: SLF001 — test asserts on impl detail
        mode = Path(sock_path).stat().st_mode & 0o777  # noqa: ASYNC240 — single sync stat is fine in tests
        assert mode == 0o700
    finally:
        await server.stop()
