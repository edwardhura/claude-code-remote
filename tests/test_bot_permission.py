"""Tests for ``ccr.bot.handlers.permission`` and ``ccr.bot.keyboards``.

CCR-025 restored the real ``cb_permission`` body. Coverage:

* ``_parse_callback`` parses / rejects the ``perm:*`` callback data shape;
* ``permission_kb`` produces the expected button rows;
* ``cb_permission`` honours pair / parse / whitelist / resolve gates and
  edits the original message with ``→ {choice} (by @username)``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from ccr.bot.handlers.permission import _parse_callback, cb_permission
from ccr.bot.keyboards import permission_kb

_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


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
    kb = permission_kb(_SESSION_ID, "r1", ["approve", "deny"])
    assert isinstance(kb, InlineKeyboardMarkup)
    assert len(kb.inline_keyboard) == 2
    expected = [
        ("Approve", f"perm:{_SESSION_ID}:r1:approve"),
        ("Deny", f"perm:{_SESSION_ID}:r1:deny"),
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
# cb_permission — handler body.
# --------------------------------------------------------------------------- #


def _make_cb(
    *,
    data: str = "",
    username: str | None = "alice",
    user_id: int = 42,
    message_text: str = "🛑 Permission requested",
) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(username=username, id=user_id)
    msg = MagicMock()
    msg.html_text = message_text
    msg.text = message_text
    msg.edit_text = AsyncMock()
    cb.message = msg
    return cb


def _make_manager(*, resolve_returns: bool = True) -> MagicMock:
    manager = MagicMock()
    manager.resolve_permission = AsyncMock(return_value=resolve_returns)
    manager.is_permission_pending = MagicMock(return_value=resolve_returns)
    return manager


@pytest.mark.asyncio
async def test_callback_paired_user_calls_resolve_permission_with_allow_payload() -> None:
    cb = _make_cb(data=f"perm:{_SESSION_ID}:r123:approve")
    manager = _make_manager()
    await cb_permission(cb, session_manager=manager, is_paired_user=True)
    manager.resolve_permission.assert_awaited_once_with(
        "r123",
        {"behavior": "allow", "updatedInput": None},
    )
    cb.message.edit_text.assert_awaited_once()
    cb.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_callback_paired_user_calls_resolve_permission_with_deny_payload() -> None:
    cb = _make_cb(data=f"perm:{_SESSION_ID}:r123:deny")
    manager = _make_manager()
    await cb_permission(cb, session_manager=manager, is_paired_user=True)
    manager.resolve_permission.assert_awaited_once_with(
        "r123",
        {"behavior": "deny", "message": "Denied by user"},
    )


@pytest.mark.asyncio
async def test_callback_edits_message_with_username_and_removes_keyboard() -> None:
    cb = _make_cb(
        data=f"perm:{_SESSION_ID}:r123:approve",
        username="bob",
        message_text="🛑 ...",
    )
    manager = _make_manager()
    await cb_permission(cb, session_manager=manager, is_paired_user=True)
    args, kwargs = cb.message.edit_text.call_args
    new_text = args[0]
    assert new_text.endswith("→ approve (by @bob)")
    assert kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_callback_edits_message_falls_back_to_user_id_when_no_username() -> None:
    cb = _make_cb(
        data=f"perm:{_SESSION_ID}:r123:approve",
        username=None,
        user_id=999,
    )
    manager = _make_manager()
    await cb_permission(cb, session_manager=manager, is_paired_user=True)
    args, _kwargs = cb.message.edit_text.call_args
    assert args[0].endswith("→ approve (by @999)")


@pytest.mark.asyncio
async def test_callback_unpaired_user_rejected_no_manager_call() -> None:
    cb = _make_cb(data=f"perm:{_SESSION_ID}:r123:approve")
    manager = _make_manager()
    await cb_permission(cb, session_manager=manager, is_paired_user=False)
    cb.answer.assert_awaited_once_with("Not paired.", show_alert=True)
    manager.resolve_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_resolve_returns_false_alerts_no_edit() -> None:
    cb = _make_cb(data=f"perm:{_SESSION_ID}:r123:approve")
    manager = _make_manager(resolve_returns=False)
    await cb_permission(cb, session_manager=manager, is_paired_user=True)
    manager.resolve_permission.assert_awaited_once()
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    cb.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_callback_invalid_choice_rejected() -> None:
    cb = _make_cb(data=f"perm:{_SESSION_ID}:r123:forge")
    manager = _make_manager()
    await cb_permission(cb, session_manager=manager, is_paired_user=True)
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    manager.resolve_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_malformed_callback_data_rejected() -> None:
    cb = _make_cb(data="perm:notauuid:r1:approve")
    manager = _make_manager()
    await cb_permission(cb, session_manager=manager, is_paired_user=True)
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    manager.resolve_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_already_answered_message_edit_swallows_bad_request() -> None:
    cb = _make_cb(data=f"perm:{_SESSION_ID}:r123:approve")
    manager = _make_manager()

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise TelegramBadRequest(method=None, message="message is not modified")  # type: ignore[arg-type]

    cb.message.edit_text = AsyncMock(side_effect=_raise)
    await cb_permission(cb, session_manager=manager, is_paired_user=True)
    # edit_text raised but cb.answer() must still complete.
    cb.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_callback_html_escapes_username_and_choice() -> None:
    """The suffix renders ``html.escape`` on user-controlled fields."""
    cb = _make_cb(
        data=f"perm:{_SESSION_ID}:r123:approve",
        username="evil<script>",
    )
    manager = _make_manager()
    await cb_permission(cb, session_manager=manager, is_paired_user=True)
    args, _kwargs = cb.message.edit_text.call_args
    assert "<script>" not in args[0]
    assert "&lt;script&gt;" in args[0]
