"""Inline-keyboard widget builders for Telegram messages.

Currently only :func:`permission_kb` is needed — the permission inline
buttons that turn a permission prompt into a tappable Telegram message.
The function is kept dormant after CCR-024 stripped the dead JSONL-based
permission channel; CCR-025 will reuse it for the MCP-driven envelope.
CCR-014 (``/view`` / ``/last`` / ``/preview``) will land link buttons
here as well, which is why this lives in its own module rather than
getting merged into ``formatting.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:
    import uuid

# Friendly button labels for the canonical Claude permission options.
# Anything not in this map falls back to ``opt.capitalize()`` so unknown
# options still render with a sensible label. The original ``opt`` string
# is preserved verbatim in ``callback_data`` so the resolver receives
# the raw option name.
_LABELS: dict[str, str] = {
    "approve": "Approve",
    "skip": "Skip",
    "abort": "Abort",
}


def permission_kb(
    session_id: uuid.UUID,
    request_id: str,
    options: list[str],
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for a permission prompt.

    One row per option. ``callback_data`` for each button is::

        perm:{session_id}:{request_id}:{choice}

    Telegram caps ``callback_data`` at 64 bytes; a UUID (36) plus the
    ``perm:`` prefix and two ``:`` and a typical option fits comfortably.
    Long ``request_id`` values could in theory blow the cap; the handler
    validates parsing and falls through to a "stale" alert rather than
    crashing if a malformed payload arrives.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_LABELS.get(opt, opt.capitalize()),
                callback_data=f"perm:{session_id}:{request_id}:{opt}",
            ),
        ]
        for opt in options
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


__all__ = ["permission_kb"]
