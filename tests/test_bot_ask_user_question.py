"""Tests for ``ccr.bot.handlers.ask_user_question`` (CCR-026).

Coverage:

* Formatter: with-options (sentinel), without-options (text), HTML
  escaping, empty-input fallback.
* Callback handler: button tap → tool_result + edit; unpaired user
  rejection; malformed callback; index out of range; unknown
  tool_use_id; resolve-returns-False; concurrent tap.
* ``/answer`` command: routed to the right id; malformed args; unknown
  prefix; ambiguous prefix.
* Plain-text path: routes to single outstanding question; falls through
  filter when zero / two outstanding / slash-prefixed.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from ccr.bot.formatting import _PendingKeyboard, event_to_messages
from ccr.bot.handlers.ask_user_question import (
    _parse_auq_callback,
    _single_free_text_outstanding,
    cb_ask_user_question,
    cmd_answer,
    handle_free_text_answer,
)
from ccr.bot.keyboards import ask_user_question_kb
from ccr.claude.events import AssistantTurn, ToolUseBlock
from ccr.claude.manager import NoActiveSessionError

_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _assistant_with(blocks: list[Any]) -> AssistantTurn:
    return AssistantTurn.model_validate(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [b.model_dump() if hasattr(b, "model_dump") else b for b in blocks],
            },
        }
    )


# --------------------------------------------------------------------------- #
# Formatter tests — duplicated from test_formatting.py with the
# CCR-026-specific coverage in this file's scope. Cheap to keep.
# --------------------------------------------------------------------------- #


def test_format_ask_user_question_with_options_returns_keyboard_sentinel() -> None:
    full_id = "toolu_abcabcabcdcdcdcd"
    block = ToolUseBlock(
        type="tool_use",
        id=full_id,
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "question": "Pick one",
                    "options": [{"label": "A"}, {"label": "B"}],
                },
            ],
        },
    )
    out = event_to_messages(_assistant_with([block]))
    assert len(out) == 1
    text, kb = out[0]
    assert "Pick one" in text
    assert isinstance(kb, _PendingKeyboard)
    assert kb.kind == "auq"
    assert kb.request_id == full_id[:8]
    assert kb.options == ["A", "B"]


def test_format_ask_user_question_without_options_returns_free_text_prompt() -> None:
    block = ToolUseBlock(
        type="tool_use",
        id="toolu_aaaabbbb11112222",
        name="AskUserQuestion",
        input={"questions": [{"question": "What name?"}]},
    )
    out = event_to_messages(_assistant_with([block]))
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert "What name?" in text
    assert "/answer" in text
    assert "toolu_aa" in text


def test_format_ask_user_question_html_escapes_question_and_options() -> None:
    block = ToolUseBlock(
        type="tool_use",
        id="toolu_evilevil0000xxxx",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "question": "<script>x</script>",
                    "options": [{"label": "A & B"}],
                },
            ],
        },
    )
    out = event_to_messages(_assistant_with([block]))
    assert len(out) == 1
    text, kb = out[0]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert isinstance(kb, _PendingKeyboard)
    assert kb.options == ["A & B"]


def test_format_ask_user_question_empty_input_falls_back() -> None:
    block = ToolUseBlock(
        type="tool_use",
        id="toolu_emptyempty00000",
        name="AskUserQuestion",
        input={},
    )
    out = event_to_messages(_assistant_with([block]))
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert "(no question text)" in text


# --------------------------------------------------------------------------- #
# Helpers for handler tests.
# --------------------------------------------------------------------------- #


def _make_cb(
    *,
    data: str = "",
    username: str | None = "alice",
    user_id: int = 42,
    message_text: str = "❓ pick one",
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


def _make_manager(
    *,
    options: list[str] | None = None,
    full_id: str | None = "toolu_aaaa1111bbbb2222",
    send_returns: bool = True,
    send_raises: BaseException | None = None,
) -> MagicMock:
    manager = MagicMock()
    manager.question_options = MagicMock(return_value=options)
    manager.question_id_by_prefix = MagicMock(return_value=full_id)

    async def _send(_tool_use_id: str, _content: str, **_kwargs: Any) -> bool:
        if send_raises is not None:
            raise send_raises
        return send_returns

    manager.send_tool_result = AsyncMock(side_effect=_send)
    manager.outstanding_free_text_questions = MagicMock(return_value=[])
    return manager


def _make_msg(text: str | None) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.answer = AsyncMock()
    return msg


# --------------------------------------------------------------------------- #
# _parse_auq_callback.
# --------------------------------------------------------------------------- #


def test_parse_auq_callback_accepts_valid_payload() -> None:
    parsed = _parse_auq_callback(f"auq:{_SESSION_ID}:abcdef01:0")
    assert parsed is not None
    sid, qid, idx = parsed
    assert sid == _SESSION_ID
    assert qid == "abcdef01"
    assert idx == 0


def test_parse_auq_callback_rejects_missing_prefix() -> None:
    assert _parse_auq_callback(f"foo:{_SESSION_ID}:abcdef01:0") is None


def test_parse_auq_callback_rejects_bad_uuid() -> None:
    assert _parse_auq_callback("auq:notauuid:abcdef01:0") is None


def test_parse_auq_callback_rejects_bad_index() -> None:
    assert _parse_auq_callback(f"auq:{_SESSION_ID}:abcdef01:notanint") is None


# --------------------------------------------------------------------------- #
# cb_ask_user_question — handler body.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cb_ask_user_question_button_tap_sends_tool_result_with_chosen_text() -> None:
    """Tap idx 0 → send_tool_result(full_id, options[0]); message edited."""
    cb = _make_cb(data=f"auq:{_SESSION_ID}:abcdef01:0")
    manager = _make_manager(options=["A", "B"], full_id="toolu_full01")
    await cb_ask_user_question(cb, session_manager=manager, is_paired_user=True)
    manager.question_id_by_prefix.assert_called_once_with("abcdef01")
    manager.send_tool_result.assert_awaited_once_with("toolu_full01", "A")
    cb.message.edit_text.assert_awaited_once()
    args, kwargs = cb.message.edit_text.call_args
    assert "→ A (by @alice)" in args[0]
    assert kwargs["reply_markup"] is None
    cb.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cb_ask_user_question_unpaired_user_rejected() -> None:
    cb = _make_cb(data=f"auq:{_SESSION_ID}:abcdef01:0")
    manager = _make_manager(options=["A"])
    await cb_ask_user_question(cb, session_manager=manager, is_paired_user=False)
    cb.answer.assert_awaited_once_with("Not paired.", show_alert=True)
    manager.send_tool_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cb_ask_user_question_malformed_callback_rejected() -> None:
    cb = _make_cb(data="auq:notauuid:abcdef01:0")
    manager = _make_manager(options=["A"])
    await cb_ask_user_question(cb, session_manager=manager, is_paired_user=True)
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    manager.send_tool_result.assert_not_awaited()
    manager.question_options.assert_not_called()


@pytest.mark.asyncio
async def test_cb_ask_user_question_index_out_of_range_rejected() -> None:
    cb = _make_cb(data=f"auq:{_SESSION_ID}:abcdef01:5")
    manager = _make_manager(options=["A", "B"], full_id="toolu_full01")
    await cb_ask_user_question(cb, session_manager=manager, is_paired_user=True)
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    manager.send_tool_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cb_ask_user_question_unknown_tool_use_id_rejected() -> None:
    """When question_id_by_prefix returns None — Stale prompt, no send."""
    cb = _make_cb(data=f"auq:{_SESSION_ID}:abcdef01:0")
    manager = _make_manager(options=None, full_id=None)
    await cb_ask_user_question(cb, session_manager=manager, is_paired_user=True)
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    manager.send_tool_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cb_ask_user_question_send_tool_result_returns_false_alerts_no_edit() -> None:
    cb = _make_cb(data=f"auq:{_SESSION_ID}:abcdef01:0")
    manager = _make_manager(options=["A"], full_id="toolu_full01", send_returns=False)
    await cb_ask_user_question(cb, session_manager=manager, is_paired_user=True)
    manager.send_tool_result.assert_awaited_once()
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    cb.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_cb_ask_user_question_concurrent_taps_only_first_succeeds() -> None:
    """Two taps: first delivers, second sees question_options=None → Stale."""
    # First tap — manager has the question.
    cb1 = _make_cb(data=f"auq:{_SESSION_ID}:abcdef01:0")
    manager_first = _make_manager(options=["A"], full_id="toolu_full01")
    await cb_ask_user_question(cb1, session_manager=manager_first, is_paired_user=True)
    manager_first.send_tool_result.assert_awaited_once_with("toolu_full01", "A")
    cb1.answer.assert_awaited_once_with()

    # Second tap on the same callback — manager now reports unknown id.
    cb2 = _make_cb(data=f"auq:{_SESSION_ID}:abcdef01:0")
    manager_second = _make_manager(options=None, full_id=None)
    await cb_ask_user_question(cb2, session_manager=manager_second, is_paired_user=True)
    cb2.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    manager_second.send_tool_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cb_ask_user_question_no_active_session_alerts_stale() -> None:
    """A NoActiveSessionError from send_tool_result maps to Stale prompt."""
    cb = _make_cb(data=f"auq:{_SESSION_ID}:abcdef01:0")
    manager = _make_manager(
        options=["A"],
        full_id="toolu_full01",
        send_raises=NoActiveSessionError("idle"),
    )
    await cb_ask_user_question(cb, session_manager=manager, is_paired_user=True)
    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)


@pytest.mark.asyncio
async def test_cb_ask_user_question_html_escapes_username_and_answer() -> None:
    cb = _make_cb(
        data=f"auq:{_SESSION_ID}:abcdef01:0",
        username="evil<script>",
    )
    manager = _make_manager(options=["A & B"], full_id="toolu_full01")
    await cb_ask_user_question(cb, session_manager=manager, is_paired_user=True)
    args, _kwargs = cb.message.edit_text.call_args
    new_text = args[0]
    assert "<script>" not in new_text
    assert "&lt;script&gt;" in new_text
    assert "&amp;" in new_text  # the option text is escaped


@pytest.mark.asyncio
async def test_cb_ask_user_question_edit_swallows_telegram_bad_request() -> None:
    cb = _make_cb(data=f"auq:{_SESSION_ID}:abcdef01:0")
    manager = _make_manager(options=["A"], full_id="toolu_full01")

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise TelegramBadRequest(method=None, message="not modified")  # type: ignore[arg-type]

    cb.message.edit_text = AsyncMock(side_effect=_raise)
    await cb_ask_user_question(cb, session_manager=manager, is_paired_user=True)
    cb.answer.assert_awaited_once_with()


# --------------------------------------------------------------------------- #
# cmd_answer — `/answer <id8> <text>`.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cmd_answer_routes_to_correct_tool_use_id() -> None:
    msg = _make_msg("/answer aaaaaaaa hello world")
    manager = _make_manager(full_id="toolu_aaaaaaaaXXX")
    await cmd_answer(msg, session_manager=manager)
    manager.question_id_by_prefix.assert_called_once_with("aaaaaaaa")
    manager.send_tool_result.assert_awaited_once_with("toolu_aaaaaaaaXXX", "hello world")
    msg.answer.assert_awaited_once_with("Forwarded.")


@pytest.mark.asyncio
async def test_cmd_answer_with_real_format_tool_use_id() -> None:
    """Regression for F1: /answer must accept the real SDK tool_use_id prefix.

    Real Claude tool_use_ids look like ``toolu_01KiaJ44hVLfkrxcbgp9uw1h``.
    The first 8 chars (``toolu_01``) contain an underscore, which the
    original ``_HEX8_RE`` (``^[0-9a-f]{8}$``) rejected — the user's typed
    answer never reached ``send_tool_result`` and the usage hint was
    echoed back instead.
    """
    full_id = "toolu_01KiaJ44hVLfkrxcbgp9uw1h"
    id8 = full_id[:8]  # "toolu_01" — contains an underscore.
    msg = _make_msg(f"/answer {id8} hello world")
    manager = _make_manager(full_id=full_id)
    await cmd_answer(msg, session_manager=manager)
    manager.question_id_by_prefix.assert_called_once_with(id8)
    manager.send_tool_result.assert_awaited_once_with(full_id, "hello world")
    msg.answer.assert_awaited_once_with("Forwarded.")


@pytest.mark.asyncio
async def test_cmd_answer_malformed_args_no_body_rejected() -> None:
    msg = _make_msg("/answer aaaaaaaa")
    manager = _make_manager()
    await cmd_answer(msg, session_manager=manager)
    msg.answer.assert_awaited_once_with("Usage: /answer <8-char-id> <text>")
    manager.send_tool_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_answer_malformed_args_too_short_rejected() -> None:
    """A prefix shorter than 8 chars is still rejected by ``_ID8_RE``."""
    msg = _make_msg("/answer XYZ something")
    manager = _make_manager()
    await cmd_answer(msg, session_manager=manager)
    msg.answer.assert_awaited_once_with("Usage: /answer <8-char-id> <text>")
    manager.send_tool_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_answer_malformed_args_invalid_chars_rejected() -> None:
    """Spaces / other chars outside ``[0-9a-zA-Z_-]`` are rejected."""
    # Within the 8-char window but contains characters outside the SDK set.
    # ``parts[0]`` here is ``"a a a a"`` (5 chars after maxsplit=1 collapses) —
    # actually maxsplit=1 keeps the first whitespace-bounded token, so use
    # an 8-char string with a disallowed character.
    msg = _make_msg("/answer abc!def@ something")
    manager = _make_manager()
    await cmd_answer(msg, session_manager=manager)
    msg.answer.assert_awaited_once_with("Usage: /answer <8-char-id> <text>")
    manager.send_tool_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_answer_unknown_prefix_rejected() -> None:
    msg = _make_msg("/answer 99999999 hello")
    manager = _make_manager(full_id=None)
    await cmd_answer(msg, session_manager=manager)
    msg.answer.assert_awaited_once_with("Stale prompt")
    manager.send_tool_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_answer_ambiguous_prefix_rejected() -> None:
    """Prefix collision → question_id_by_prefix returns None → Stale prompt."""
    msg = _make_msg("/answer abcdef00 something")
    manager = _make_manager(full_id=None)
    await cmd_answer(msg, session_manager=manager)
    msg.answer.assert_awaited_once_with("Stale prompt")
    manager.send_tool_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_answer_send_tool_result_false_returns_stale() -> None:
    msg = _make_msg("/answer aaaaaaaa hi")
    manager = _make_manager(full_id="toolu_real", send_returns=False)
    await cmd_answer(msg, session_manager=manager)
    msg.answer.assert_awaited_once_with("Stale prompt")


@pytest.mark.asyncio
async def test_cmd_answer_no_active_session_returns_stale() -> None:
    msg = _make_msg("/answer aaaaaaaa hi")
    manager = _make_manager(
        full_id="toolu_real",
        send_raises=NoActiveSessionError("idle"),
    )
    await cmd_answer(msg, session_manager=manager)
    msg.answer.assert_awaited_once_with("Stale prompt")


# --------------------------------------------------------------------------- #
# handle_free_text_answer + filter.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handle_free_text_answer_routes_to_single_outstanding_question() -> None:
    msg = _make_msg("blue")
    manager = _make_manager()
    manager.outstanding_free_text_questions = MagicMock(return_value=["toolu_only"])
    await handle_free_text_answer(msg, session_manager=manager)
    manager.send_tool_result.assert_awaited_once_with("toolu_only", "blue")
    msg.answer.assert_awaited_once_with("Forwarded.")


@pytest.mark.asyncio
async def test_handle_free_text_answer_filter_falls_through_when_zero_outstanding() -> None:
    """Filter returns False → handler does not run; in real wiring,
    ``session.handle_text`` would pick the message up. Here we assert the
    filter explicitly returns False."""
    msg = _make_msg("blue")
    manager = MagicMock()
    manager.outstanding_free_text_questions = MagicMock(return_value=[])
    fired = await _single_free_text_outstanding(msg, session_manager=manager)
    assert fired is False


@pytest.mark.asyncio
async def test_handle_free_text_answer_filter_falls_through_when_two_outstanding() -> None:
    msg = _make_msg("blue")
    manager = MagicMock()
    manager.outstanding_free_text_questions = MagicMock(return_value=["a", "b"])
    fired = await _single_free_text_outstanding(msg, session_manager=manager)
    assert fired is False


@pytest.mark.asyncio
async def test_handle_free_text_answer_filter_excludes_slash_prefix() -> None:
    """Slash-prefixed messages are excluded so /cost / /usage etc. fall through."""
    msg = _make_msg("/cost")
    manager = MagicMock()
    manager.outstanding_free_text_questions = MagicMock(return_value=["toolu_only"])
    fired = await _single_free_text_outstanding(msg, session_manager=manager)
    assert fired is False


@pytest.mark.asyncio
async def test_handle_free_text_answer_filter_claims_when_one_outstanding() -> None:
    msg = _make_msg("hello")
    manager = MagicMock()
    manager.outstanding_free_text_questions = MagicMock(return_value=["toolu_only"])
    fired = await _single_free_text_outstanding(msg, session_manager=manager)
    assert fired is True


@pytest.mark.asyncio
async def test_handle_free_text_answer_returns_silently_on_race() -> None:
    """If the dict races empty between filter and handler, exit silently."""
    msg = _make_msg("blue")
    manager = _make_manager()
    manager.outstanding_free_text_questions = MagicMock(return_value=[])
    await handle_free_text_answer(msg, session_manager=manager)
    manager.send_tool_result.assert_not_awaited()
    msg.answer.assert_not_called()


@pytest.mark.asyncio
async def test_handle_free_text_answer_send_returns_false_replies_stale() -> None:
    msg = _make_msg("blue")
    manager = _make_manager(send_returns=False, full_id="ignored")
    manager.outstanding_free_text_questions = MagicMock(return_value=["toolu_only"])
    await handle_free_text_answer(msg, session_manager=manager)
    msg.answer.assert_awaited_once_with("Stale prompt")


# --------------------------------------------------------------------------- #
# ask_user_question_kb keyboard shape.
# --------------------------------------------------------------------------- #


def test_ask_user_question_kb_shape_indexed_callbacks_under_64_bytes() -> None:
    kb = ask_user_question_kb(_SESSION_ID, "abcdef01", ["red", "blue", "green"])
    assert isinstance(kb, InlineKeyboardMarkup)
    assert len(kb.inline_keyboard) == 3
    expected = [
        ("red", f"auq:{_SESSION_ID}:abcdef01:0"),
        ("blue", f"auq:{_SESSION_ID}:abcdef01:1"),
        ("green", f"auq:{_SESSION_ID}:abcdef01:2"),
    ]
    for row, (label, data) in zip(kb.inline_keyboard, expected, strict=True):
        assert len(row) == 1
        assert row[0].text == label
        assert row[0].callback_data == data
        assert len(row[0].callback_data.encode("utf-8")) <= 64
