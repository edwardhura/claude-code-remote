"""``perm:*`` callback-query handler — permission inline-button responses.

CCR-024 stripped the body of this handler; CCR-025 restores it now that
permission gating runs through the MCP ``--permission-prompt-tool`` channel.
The handler:

* validates the callback id triple (``session_id``, ``request_id``, ``choice``);
* whitelists ``choice`` to ``("approve", "deny")`` — anything else maps to
  "Stale prompt" without ever calling :meth:`SessionManager.resolve_permission`
  (so a forged ``callback_data`` cannot smuggle an arbitrary string into the
  decision dict that goes to claude);
* builds the decision dict on the *server* side from the whitelisted choice;
* maps ``resolve_permission(...) == False`` to "Stale prompt" with no
  ``edit_text`` (the message has already been edited by an earlier tap, or
  the request timed out);
* edits the original message to record who decided (best-effort —
  :class:`TelegramBadRequest` is suppressed so a concurrent tap does not
  prevent ``cb.answer()``).
"""

from __future__ import annotations

import contextlib
import html
import uuid
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery

    from ccr.claude.manager import SessionManager


_CALLBACK_PART_COUNT = 4
_ALLOWED_CHOICES: frozenset[str] = frozenset({"approve", "deny"})


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
async def cb_permission(
    cb: CallbackQuery,
    session_manager: SessionManager,
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name
) -> None:
    """Resolve a permission tap into a manager-side decision."""
    if not is_paired_user:
        await cb.answer("Not paired.", show_alert=True)
        return
    if cb.data is None or cb.from_user is None:
        return

    parsed = _parse_callback(cb.data)
    if parsed is None:
        await cb.answer("Stale prompt", show_alert=True)
        return
    _session_id, request_id, choice = parsed

    if choice not in _ALLOWED_CHOICES:
        await cb.answer("Stale prompt", show_alert=True)
        return

    decision: dict[str, object] = (
        {"behavior": "allow", "updatedInput": None}
        if choice == "approve"
        else {"behavior": "deny", "message": "Denied by user"}
    )
    resolved = await session_manager.resolve_permission(request_id, decision)
    if not resolved:
        await cb.answer("Stale prompt", show_alert=True)
        return

    username = cb.from_user.username or str(cb.from_user.id)
    suffix = f"\n→ {html.escape(choice)} (by @{html.escape(username)})"
    if cb.message is not None and hasattr(cb.message, "edit_text"):
        original = getattr(cb.message, "html_text", None) or getattr(cb.message, "text", None) or ""
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(original + suffix, reply_markup=None)
    await cb.answer()


__all__ = ["cb_permission", "router"]
