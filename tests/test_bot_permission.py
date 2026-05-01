"""Tests for ``ccr.bot.handlers.permission`` and ``ccr.bot.keyboards``.

CCR-024 stripped the body of ``cb_permission``; only the parse helper and
keyboard widget tests remain. The handler-body tests will be restored when
CCR-025 wires the MCP-driven permission channel through this router.
"""

from __future__ import annotations

import uuid

from aiogram.types import InlineKeyboardMarkup

from ccr.bot.handlers.permission import _parse_callback
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
