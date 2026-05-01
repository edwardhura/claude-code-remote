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
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from ccr.claude.events import McpPermissionRequest
from ccr.claude.mcp import McpPermissionServer
from ccr.events import EventBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp import ClientSession


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
