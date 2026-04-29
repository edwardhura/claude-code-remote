"""Tests for ``ccr.bot.handlers.permission`` and ``ccr.bot.keyboards``.

Calls the handler directly (matching the pattern in ``test_bot_session.py``)
with a stub :class:`SessionManager` and a hand-built
:class:`aiogram.types.CallbackQuery` so we exercise the dispatch logic
without spinning up a real Telegram bot.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User

from ccr.bot.handlers.permission import _parse_callback, cb_permission
from ccr.bot.keyboards import permission_kb
from ccr.claude.manager import (
    NoActiveSessionError,
    SessionError,
    StaleSessionError,
)

_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


# --------------------------------------------------------------------------- #
# Helpers / stubs.
# --------------------------------------------------------------------------- #


class FakeManager:
    """Stub :class:`SessionManager` exposing only the surface the handler uses."""

    def __init__(
        self,
        *,
        valid: bool = True,
        send_side_effect: Exception | None = None,
    ) -> None:
        self._valid = valid
        self.send_permission = AsyncMock()
        if send_side_effect is not None:
            self.send_permission.side_effect = send_side_effect

    def is_permission_choice_valid(self, request_id: str, choice: str) -> bool:
        del request_id, choice
        return self._valid


def _make_message(
    *,
    chat_id: int = 1000,
    text: str = "\U0001f6d1 Permission requested\nTool: <code>bash</code>\nInput: {'cmd': 'ls'}",
    edit_side_effect: Exception | None = None,
) -> Message:
    msg = Message(
        message_id=42,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        text=text,
    )
    edit = AsyncMock()
    if edit_side_effect is not None:
        edit.side_effect = edit_side_effect
    object.__setattr__(msg, "edit_text", edit)
    # ``html_text`` is a read-only Pydantic accessor; stub it to a known
    # string so the handler concatenates with our expected prefix.
    object.__setattr__(msg, "_html_text_override", text)
    return msg


def _make_callback(
    *,
    data: str,
    user_id: int = 99,
    username: str | None = "alice",
    message: Message | None = None,
) -> CallbackQuery:
    cb = CallbackQuery(
        id="cb-1",
        from_user=User(id=user_id, is_bot=False, first_name="A", username=username),
        chat_instance="chat-instance-x",
        data=data,
        message=message,
    )
    object.__setattr__(cb, "answer", AsyncMock())
    return cb


@pytest.fixture(autouse=True)
def _patch_html_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override Message.html_text to use the fixture's stored text.

    aiogram's ``html_text`` property re-renders entities that are attached
    to a message; since our test ``Message`` instances have no entities
    we just want to read the raw text back.
    """

    def _html_text(self: Any) -> str | None:
        override = getattr(self, "_html_text_override", None)
        if override is not None:
            return override  # type: ignore[no-any-return]
        return self.text

    monkeypatch.setattr(Message, "html_text", property(_html_text))


# --------------------------------------------------------------------------- #
# _parse_callback — unit tests.
# --------------------------------------------------------------------------- #


def test_parse_callback_accepts_valid_payload() -> None:
    parsed = _parse_callback(f"perm:{_SESSION_ID}:r1:approve")
    assert parsed is not None
    sid, req, choice = parsed
    assert sid == _SESSION_ID
    assert req == "r1"
    assert choice == "approve"


def test_parse_callback_rejects_missing_prefix() -> None:
    assert _parse_callback(f"foo:{_SESSION_ID}:r1:approve") is None


def test_parse_callback_rejects_bad_uuid() -> None:
    assert _parse_callback("perm:notauuid:r1:approve") is None


def test_parse_callback_rejects_too_few_segments() -> None:
    assert _parse_callback("perm:abc:def") is None


# --------------------------------------------------------------------------- #
# permission_kb — keyboard shape.
# --------------------------------------------------------------------------- #


def test_keyboard_buttons_match_options_and_callback_data() -> None:
    kb = permission_kb(_SESSION_ID, "r1", ["approve", "skip", "abort"])
    assert isinstance(kb, InlineKeyboardMarkup)
    assert len(kb.inline_keyboard) == 3
    expected = [
        ("Approve", f"perm:{_SESSION_ID}:r1:approve"),
        ("Skip", f"perm:{_SESSION_ID}:r1:skip"),
        ("Abort", f"perm:{_SESSION_ID}:r1:abort"),
    ]
    for row, (label, data) in zip(kb.inline_keyboard, expected, strict=True):
        assert len(row) == 1
        assert row[0].text == label
        assert row[0].callback_data == data


def test_keyboard_unknown_option_uses_capitalized_label() -> None:
    kb = permission_kb(_SESSION_ID, "r1", ["foo"])
    assert kb.inline_keyboard[0][0].text == "Foo"
    assert kb.inline_keyboard[0][0].callback_data == f"perm:{_SESSION_ID}:r1:foo"


# --------------------------------------------------------------------------- #
# cb_permission — handler tests.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_callback_paired_user_calls_send_permission_with_right_args() -> None:
    manager = FakeManager(valid=True)
    msg = _make_message()
    cb = _make_callback(data=f"perm:{_SESSION_ID}:r1:approve", message=msg)

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    manager.send_permission.assert_awaited_once_with(_SESSION_ID, "r1", "approve")


@pytest.mark.asyncio
async def test_callback_edits_message_with_username_and_removes_keyboard() -> None:
    manager = FakeManager(valid=True)
    original = "\U0001f6d1 Permission requested\nTool: <code>bash</code>\nInput: {}"
    msg = _make_message(text=original)
    cb = _make_callback(
        data=f"perm:{_SESSION_ID}:r1:approve",
        username="alice",
        message=msg,
    )

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    msg.edit_text.assert_awaited_once()
    args, kwargs = msg.edit_text.await_args.args, msg.edit_text.await_args.kwargs
    new_text = args[0] if args else kwargs.get("text")
    assert "→ approve (by @alice)" in new_text
    assert kwargs.get("reply_markup") is None
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_callback_edits_message_falls_back_to_user_id_when_no_username() -> None:
    manager = FakeManager(valid=True)
    msg = _make_message()
    cb = _make_callback(
        data=f"perm:{_SESSION_ID}:r1:approve",
        username=None,
        user_id=123456,
        message=msg,
    )

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    new_text = msg.edit_text.await_args.args[0]
    assert "→ approve (by @123456)" in new_text


@pytest.mark.asyncio
async def test_callback_unpaired_user_rejected_no_manager_call() -> None:
    manager = FakeManager(valid=True)
    msg = _make_message()
    cb = _make_callback(data=f"perm:{_SESSION_ID}:r1:approve", message=msg)

    await cb_permission(cb, session_manager=manager, is_paired_user=False)

    manager.send_permission.assert_not_called()
    cb.answer.assert_awaited_once_with("Not paired.", show_alert=True)


@pytest.mark.asyncio
async def test_callback_stale_session_id_alerts_no_manager_call() -> None:
    """``send_permission`` raising ``StaleSessionError`` produces a stale-prompt alert."""
    manager = FakeManager(valid=True, send_side_effect=StaleSessionError("stale"))
    msg = _make_message()
    cb = _make_callback(data=f"perm:{_SESSION_ID}:r1:approve", message=msg)

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    msg.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_no_active_session_alerts() -> None:
    """``send_permission`` raising ``NoActiveSessionError`` produces the matching alert."""
    manager = FakeManager(valid=True, send_side_effect=NoActiveSessionError("no session"))
    msg = _make_message()
    cb = _make_callback(data=f"perm:{_SESSION_ID}:r1:approve", message=msg)

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    cb.answer.assert_awaited_once_with("No active session", show_alert=True)
    msg.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_session_error_alerts_failed_to_record() -> None:
    """A bare ``SessionError`` (e.g. subprocess write failure) surfaces a generic alert."""
    manager = FakeManager(valid=True, send_side_effect=SessionError("write failed"))
    msg = _make_message()
    cb = _make_callback(data=f"perm:{_SESSION_ID}:r1:approve", message=msg)

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    cb.answer.assert_awaited_once_with("Failed to record choice", show_alert=True)
    msg.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_invalid_choice_rejected() -> None:
    """A forged ``choice`` is rejected before ``send_permission`` is called."""
    manager = FakeManager(valid=False)
    msg = _make_message()
    cb = _make_callback(data=f"perm:{_SESSION_ID}:r1:abort", message=msg)

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    manager.send_permission.assert_not_awaited()
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    msg.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_malformed_callback_data_rejected() -> None:
    """A bad UUID in the payload gets the same "stale prompt" treatment."""
    manager = FakeManager(valid=True)
    msg = _make_message()
    cb = _make_callback(data="perm:notauuid:r1:approve", message=msg)

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    manager.send_permission.assert_not_awaited()
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)


@pytest.mark.asyncio
async def test_callback_already_answered_message_edit_swallows_bad_request() -> None:
    """Concurrent taps: second ``edit_text`` raises ``TelegramBadRequest``; handler still completes."""
    manager = FakeManager(valid=True)
    msg = _make_message(
        edit_side_effect=TelegramBadRequest(
            method=MagicMock(),
            message="Bad Request: message is not modified",
        ),
    )
    cb = _make_callback(data=f"perm:{_SESSION_ID}:r1:approve", message=msg)

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    manager.send_permission.assert_awaited_once_with(_SESSION_ID, "r1", "approve")
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_callback_html_escapes_choice_and_username() -> None:
    """``choice`` and ``username`` go through ``html.escape``; original text NOT re-escaped."""
    manager = FakeManager(valid=True)
    original = "\U0001f6d1 Permission requested\nTool: <code>bash</code>\nInput: {}"
    msg = _make_message(text=original)
    cb = _make_callback(
        data=f"perm:{_SESSION_ID}:r1:approve",
        username="al<ice>",
        message=msg,
    )

    await cb_permission(cb, session_manager=manager, is_paired_user=True)

    new_text = msg.edit_text.await_args.args[0]
    # Original ``<code>`` tag preserved, NOT re-escaped.
    assert "<code>bash</code>" in new_text
    # Suffix HTML-escapes the username.
    assert "@al&lt;ice&gt;" in new_text
