"""Tests for ``ccr.bot.handlers.passthrough`` — slash-command whitelist."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject
from aiogram.types import Chat, Message, User

from ccr.bot.handlers.passthrough import cmd_passthrough
from ccr.claude.manager import NoActiveSessionError

# --------------------------------------------------------------------------- #
# Helpers / stubs.
# --------------------------------------------------------------------------- #


def _make_message(*, text: str, user_id: int = 42, chat_id: int = 1000) -> Message:
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="A", username="alice"),
        text=text,
    )
    object.__setattr__(msg, "answer", AsyncMock())
    return msg


def _command(name: str, args: str = "") -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args or None)


class FakeManager:
    """Stub :class:`SessionManager` exposing only ``send_slash``."""

    def __init__(self, *, send_side_effect: Exception | None = None) -> None:
        self.send_slash = AsyncMock()
        if send_side_effect is not None:
            self.send_slash.side_effect = send_side_effect


# --------------------------------------------------------------------------- #
# Whitelist branch.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cost_calls_send_slash() -> None:
    """``/cost`` is whitelisted → forwarded via ``send_slash`` with empty args."""
    manager = FakeManager()
    msg = _make_message(text="/cost")

    await cmd_passthrough(msg, command=_command("cost"), session_manager=manager)

    manager.send_slash.assert_awaited_once_with("cost", "")
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_cost_no_session() -> None:
    """``/cost`` while idle → ``NoActiveSessionError`` is caught and replied politely."""
    manager = FakeManager(send_side_effect=NoActiveSessionError("No active session."))
    msg = _make_message(text="/cost")

    await cmd_passthrough(msg, command=_command("cost"), session_manager=manager)

    manager.send_slash.assert_awaited_once_with("cost", "")
    msg.answer.assert_awaited_once_with("No active session.")


@pytest.mark.asyncio
async def test_model_with_args_passes_args_through() -> None:
    """Arguments after the command are forwarded as-is to ``send_slash``."""
    manager = FakeManager()
    msg = _make_message(text="/model claude-3-opus")

    await cmd_passthrough(
        msg,
        command=_command("model", "claude-3-opus"),
        session_manager=manager,
    )

    manager.send_slash.assert_awaited_once_with("model", "claude-3-opus")


# --------------------------------------------------------------------------- #
# Blocked-interactive branch.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agents_is_blocked() -> None:
    """``/agents`` produces the canned interactive-command message; no manager call."""
    manager = FakeManager()
    msg = _make_message(text="/agents")

    await cmd_passthrough(msg, command=_command("agents"), session_manager=manager)

    manager.send_slash.assert_not_awaited()
    msg.answer.assert_awaited_once_with(
        "Interactive command — run /agents in your local Claude Code terminal.",
    )


@pytest.mark.asyncio
async def test_mcp_is_blocked() -> None:
    """``/mcp`` is also blocked-interactive."""
    manager = FakeManager()
    msg = _make_message(text="/mcp")

    await cmd_passthrough(msg, command=_command("mcp"), session_manager=manager)

    manager.send_slash.assert_not_awaited()
    msg.answer.assert_awaited_once_with(
        "Interactive command — run /mcp in your local Claude Code terminal.",
    )


# --------------------------------------------------------------------------- #
# Unknown branch.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_command_returns_usage() -> None:
    """An unrecognised slash command produces the canonical usage hint."""
    manager = FakeManager()
    msg = _make_message(text="/totallyunknown")

    await cmd_passthrough(
        msg,
        command=_command("totallyunknown"),
        session_manager=manager,
    )

    manager.send_slash.assert_not_awaited()
    msg.answer.assert_awaited_once_with(
        "Unknown command. Whitelisted: /new /stop /clear /view /last /preview "
        "/cost /model /compact /who.",
    )
