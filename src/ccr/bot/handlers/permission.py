"""``perm:*`` callback-query handler — permission inline-button responses.

CCR-024 stripped the body of this handler. Permission gating is moving to
the MCP ``--permission-prompt-tool`` channel under CCR-025; until that
ticket lands, any ``perm:*`` callback that reaches this handler is from a
stale message produced by the dead CCR-009 path and must fail loudly. The
``router`` registration and :func:`_parse_callback` helper survive so
CCR-025 can replace the body without re-deriving the wire-protocol parse.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from aiogram import F, Router

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
async def cb_permission(
    cb: CallbackQuery,
    session_manager: SessionManager,
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name
) -> None:
    """Stub: MCP integration pending — see CCR-025."""
    del cb, session_manager, is_paired_user
    message = "MCP integration pending — see CCR-025"
    raise NotImplementedError(message)


__all__ = ["cb_permission", "router"]
