"""``auq:*`` callback + ``/answer`` + plain-text-when-single handlers (CCR-026).

The router covers three entry points and one custom filter:

* :func:`cb_ask_user_question` — callback handler claimed by
  ``F.data.startswith("auq:")``. Mirrors :func:`cb_permission`'s shape:
  parse the callback triple, look up the option text by index from
  :meth:`SessionManager.question_options`, deliver via
  :meth:`SessionManager.send_tool_result`, and edit the originating
  message with a ``→ {answer} (by @{username})`` suffix. Stale / forged
  / index-out-of-range payloads map to the canned ``"Stale prompt"``
  alert.
* :func:`cmd_answer` — ``/answer <id8> <text>`` handler. Multi-question
  safe: the user picks a specific outstanding question by 8-char id
  prefix. Resolves the prefix via
  :meth:`SessionManager.question_id_by_prefix`.
* :func:`handle_free_text_answer` — plain-text handler claimed only when
  exactly one free-text question is outstanding (the
  :func:`_single_free_text_outstanding` filter). Zero or ≥2 outstanding
  → filter returns ``False`` and the message falls through to the
  existing :func:`session.handle_text` (where it becomes a normal
  user-turn).

The router is registered between ``pairing_router`` and ``session_router``
in :func:`ccr.bot.app.build_dispatcher` so the conditional plain-text
claim resolves before ``session.handle_text``'s catch-all does.
"""

from __future__ import annotations

import contextlib
import html
import re
import uuid
from typing import TYPE_CHECKING

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command

from ccr.claude.manager import NoActiveSessionError

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message

    from ccr.claude.manager import SessionManager


# Per CCR-026 plan §"Patterns and prior art": do not lift this regex to a
# shared module — two callers (session.py + this file) do not justify a new
# abstraction. The ``session.py`` ``_HEX8_RE`` correctly guards hex-UUID
# session IDs; this handler instead guards Claude SDK ``tool_use_id``
# prefixes, whose real character set is alphanumeric plus ``_`` and ``-``
# (e.g. ``toolu_01KiaJ44hVLfkrxcbgp9uw1h`` — the leading 8 chars are
# ``toolu_01``, which contains an underscore). A 1:1 transcription of
# ``_HEX8_RE`` would reject every real id prefix.
_ID8_RE = re.compile(r"^[0-9a-zA-Z_-]{8}$")

_AUQ_PREFIX = "auq:"
_AUQ_PART_COUNT = 4
_ANSWER_PART_COUNT = 2
_STALE_ALERT = "Stale prompt"
_FORWARDED_REPLY = "Forwarded."
_USAGE_HINT = "Usage: /answer &lt;8-char-id&gt; &lt;text&gt;"


log = structlog.get_logger(__name__)
router = Router(name="ask_user_question")


def _parse_auq_callback(data: str) -> tuple[uuid.UUID, str, int] | None:
    """Parse ``auq:{session_id}:{question_id}:{idx}`` into typed parts.

    Returns ``None`` on malformed payload (missing prefix, wrong segment
    count, non-UUID session id, or non-integer index). ``question_id`` is
    the 8-char prefix of the full ``tool_use_id`` and is returned verbatim.
    """
    if not data.startswith(_AUQ_PREFIX):
        return None
    parts = data.split(":", 3)
    if len(parts) != _AUQ_PART_COUNT:
        return None
    _prefix, sid_str, question_id, idx_str = parts
    if not question_id:
        return None
    try:
        session_id = uuid.UUID(sid_str)
        idx = int(idx_str)
    except ValueError:
        return None
    return session_id, question_id, idx


@router.callback_query(F.data.startswith(_AUQ_PREFIX))
async def cb_ask_user_question(  # noqa: PLR0911 — gate-check ladder mirrors cb_permission
    cb: CallbackQuery,
    session_manager: SessionManager,
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name
) -> None:
    """Resolve an AskUserQuestion button tap into a manager-side tool_result."""
    if not is_paired_user:
        await cb.answer("Not paired.", show_alert=True)
        return
    if cb.data is None or cb.from_user is None:
        return

    parsed = _parse_auq_callback(cb.data)
    if parsed is None:
        await cb.answer(_STALE_ALERT, show_alert=True)
        return
    _session_id, question_id, idx = parsed

    full_id = session_manager.question_id_by_prefix(question_id)
    if full_id is None:
        await cb.answer(_STALE_ALERT, show_alert=True)
        return

    options = session_manager.question_options(full_id)
    if options is None or not 0 <= idx < len(options):
        await cb.answer(_STALE_ALERT, show_alert=True)
        return

    answer_text = options[idx]
    try:
        delivered = await session_manager.send_tool_result(full_id, answer_text)
    except NoActiveSessionError:
        await cb.answer(_STALE_ALERT, show_alert=True)
        return
    if not delivered:
        await cb.answer(_STALE_ALERT, show_alert=True)
        return

    username = cb.from_user.username or str(cb.from_user.id)
    suffix = f"\n→ {html.escape(answer_text)} (by @{html.escape(username)})"
    if cb.message is not None and hasattr(cb.message, "edit_text"):
        original = getattr(cb.message, "html_text", None) or getattr(cb.message, "text", None) or ""
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(original + suffix, reply_markup=None)
    await cb.answer()


@router.message(Command("answer"))
async def cmd_answer(
    msg: Message,
    session_manager: SessionManager,
) -> None:
    """``/answer <id8> <text>`` — direct typed reply to a specific question.

    Always works regardless of how many free-text questions are
    outstanding — the explicit 8-char id prefix is the disambiguator.
    """
    raw = (msg.text or "").removeprefix("/answer").strip()
    parts = raw.split(maxsplit=1)
    if len(parts) != _ANSWER_PART_COUNT or not _ID8_RE.match(parts[0]):
        try:
            await msg.answer(_USAGE_HINT)
        except TelegramBadRequest:
            log.warning("usage_hint_send_failed", exc_info=True)
        return
    id_prefix, body = parts

    full_id = session_manager.question_id_by_prefix(id_prefix)
    if full_id is None:
        await msg.answer(_STALE_ALERT)
        return
    try:
        delivered = await session_manager.send_tool_result(full_id, body)
    except NoActiveSessionError:
        await msg.answer(_STALE_ALERT)
        return
    if not delivered:
        await msg.answer(_STALE_ALERT)
        return
    await msg.answer(_FORWARDED_REPLY)


async def _single_free_text_outstanding(
    message: Message,
    session_manager: SessionManager,
) -> bool:
    """Filter: claim plain text only when exactly one free-text question is outstanding.

    aiogram passes workflow data as kwargs to filter callables, so
    ``session_manager`` is available here. Excludes slash-prefixed
    messages so ``/cost``, ``/usage``, etc. always reach the
    passthrough router. Also excludes empty / non-text messages.
    """
    if message.text is None or message.text.startswith("/"):
        return False
    return len(session_manager.outstanding_free_text_questions()) == 1


@router.message(F.text & ~F.text.startswith("/"), _single_free_text_outstanding)
async def handle_free_text_answer(
    msg: Message,
    session_manager: SessionManager,
) -> None:
    """Plain text → answer the single outstanding free-text question."""
    pending = session_manager.outstanding_free_text_questions()
    # Filter guarantees len == 1, but re-check defensively (race with a
    # concurrent button tap or another paired user's /answer).
    if len(pending) != 1:
        return
    text = msg.text or ""
    try:
        delivered = await session_manager.send_tool_result(pending[0], text)
    except NoActiveSessionError:
        await msg.answer(_STALE_ALERT)
        return
    if not delivered:
        await msg.answer(_STALE_ALERT)
        return
    await msg.answer(_FORWARDED_REPLY)


__all__ = [
    "cb_ask_user_question",
    "cmd_answer",
    "handle_free_text_answer",
    "router",
]
