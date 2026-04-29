"""``perm:*`` callback-query handler — permission inline-button responses.

A single handler matches every callback whose ``data`` starts with ``perm:``
and parses the remaining ``{session_id}:{request_id}:{choice}`` segments.
The session id is validated as a UUID and the choice is validated against
the authoritative option set held by the
:class:`~ccr.claude.manager.SessionManager` — never against the
(forgeable) ``callback_data`` itself.

On a successful tap the original message is edited in place: the keyboard
is removed and ``"→ {choice} (by @{username})"`` is appended so every
paired user sees who answered. Concurrent taps from a second user race
into the validation; the second one fails with "Stale prompt" because
``send_permission`` cleared the request_id from
``_pending_options``.
"""

from __future__ import annotations

import contextlib
import html
import uuid
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from ccr.claude.manager import (
    NoActiveSessionError,
    SessionError,
    StaleSessionError,
)

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery

    from ccr.claude.manager import SessionManager


_CALLBACK_PART_COUNT = 4


router = Router(name="permission")


def _parse_callback(data: str) -> tuple[uuid.UUID, str, str] | None:
    """Parse ``perm:{session_id}:{request_id}:{choice}`` into typed parts.

    Returns ``None`` if the prefix is missing, the segment count is wrong,
    or the session id is not a valid UUID. ``request_id`` and ``choice``
    are returned verbatim — the manager validates them next.
    """
    if not data.startswith("perm:"):
        return None
    parts = data.split(":", 3)
    if len(parts) != _CALLBACK_PART_COUNT:
        return None
    _prefix, sid_str, request_id, choice = parts
    if not request_id or not choice:
        return None
    try:
        session_id = uuid.UUID(sid_str)
    except ValueError:
        return None
    return session_id, request_id, choice


@router.callback_query(F.data.startswith("perm:"))
async def cb_permission(  # noqa: PLR0911 — each early return maps to a distinct documented edge case
    cb: CallbackQuery,
    session_manager: SessionManager,
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name
) -> None:
    """Handle a paired user tapping a permission inline button."""
    if not is_paired_user:
        # Defensive — the AllowlistMiddleware already short-circuits
        # unpaired callback queries, but keep an explicit fail-closed.
        await cb.answer("Not paired.", show_alert=True)
        return
    if cb.data is None or cb.from_user is None:
        # aiogram should never deliver these but be defensive.
        return

    parsed = _parse_callback(cb.data)
    if parsed is None:
        await cb.answer("Stale prompt", show_alert=True)
        return
    session_id, request_id, choice = parsed

    if not session_manager.is_permission_choice_valid(request_id, choice):
        await cb.answer("Stale prompt", show_alert=True)
        return

    try:
        await session_manager.send_permission(session_id, request_id, choice)
    except StaleSessionError:
        await cb.answer("Stale prompt", show_alert=True)
        return
    except NoActiveSessionError:
        await cb.answer("No active session", show_alert=True)
        return
    except SessionError:
        await cb.answer("Failed to record choice", show_alert=True)
        return

    username = cb.from_user.username or str(cb.from_user.id)
    suffix = f"\n→ {html.escape(choice)} (by @{html.escape(username)})"

    message = cb.message
    if isinstance(message, Message):
        # The original text comes from cb.message.html_text — already
        # rendered HTML, do NOT re-escape it. We append the suffix
        # (already escaped) and drop the keyboard. ``html_text`` is an
        # accessor that re-renders the message with entity tags; it
        # returns ``None`` when the message has no text (a photo, etc.).
        original_text = message.html_text or message.text or ""
        with contextlib.suppress(TelegramBadRequest):
            await message.edit_text(original_text + suffix, reply_markup=None)

    await cb.answer()


__all__ = ["cb_permission", "router"]
