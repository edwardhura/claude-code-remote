"""Session lifecycle handlers — `/new`, `/stop`, `/clear`, `/who`, plain text.

The :class:`SessionManager` and the DB factory are passed in via aiogram
workflow data (``dp["session_manager"] = ...`` etc.) so handlers stay free
of module-level singletons. Reply strings are deliberately stable — tests
match them verbatim.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command

from ccr.auth import pairing
from ccr.claude.manager import (
    NoActiveSessionError,
    SessionError,
)
from ccr.claude.state import SessionStatus

if TYPE_CHECKING:
    from aiogram.types import Message
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ccr.claude.manager import SessionManager


router = Router(name="session")

_SECONDS_PER_MINUTE = 60


def _short_id(session_id: object) -> str:
    return str(session_id)[:8]


def _format_uptime(started_at: datetime) -> str:
    """Render uptime as ``"{N}s"`` for <60s and ``"{m}m {s}s"`` otherwise."""
    delta = datetime.now(UTC) - started_at
    total_s = max(int(delta.total_seconds()), 0)
    if total_s >= _SECONDS_PER_MINUTE:
        return f"{total_s // _SECONDS_PER_MINUTE}m {total_s % _SECONDS_PER_MINUTE}s"
    return f"{total_s}s"


@router.message(Command("new"))
async def cmd_new(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],  # noqa: ARG001 — kept for parity with siblings
) -> None:
    """Start a fresh Claude session with no initial prompt."""
    if msg.from_user is None:
        return
    try:
        session_id = await session_manager.new_session(
            prompt=None,
            started_by_tg_user_id=msg.from_user.id,
        )
    except SessionError as exc:
        await msg.answer(html.escape(str(exc)))
        return
    inf = await session_manager.info()
    pid = inf.get("pid")
    await msg.answer(f"Session {_short_id(session_id)} started (pid {pid}).")


@router.message(Command("stop"))
async def cmd_stop(
    msg: Message,
    session_manager: SessionManager,
) -> None:
    """Stop the active session, or report idle when there's nothing to stop.

    :meth:`SessionManager.stop` is itself idempotent (no-op on idle), so we
    look up the status before calling so we can produce the documented
    "No active session." reply on the idle path.
    """
    status = await session_manager.status()
    if status == SessionStatus.IDLE:
        await msg.answer("No active session.")
        return
    await session_manager.stop()
    await msg.answer("Session stopped.")


@router.message(Command("clear"))
async def cmd_clear(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],  # noqa: ARG001 — kept for parity with siblings
) -> None:
    """Stop any running session and immediately start a fresh empty one."""
    if msg.from_user is None:
        return
    await session_manager.stop()
    try:
        session_id = await session_manager.new_session(
            prompt=None,
            started_by_tg_user_id=msg.from_user.id,
        )
    except SessionError as exc:
        await msg.answer(html.escape(str(exc)))
        return
    inf = await session_manager.info()
    pid = inf.get("pid")
    await msg.answer(f"Session {_short_id(session_id)} started (pid {pid}).")


@router.message(Command("pid"))
async def cmd_pid(
    msg: Message,
    session_manager: SessionManager,
) -> None:
    """Reply with the current session id, subprocess pid, and uptime."""
    inf = await session_manager.info()
    if inf.get("status") == SessionStatus.IDLE or inf.get("session_id") is None:
        await msg.answer("No active session.")
        return
    id8 = str(inf.get("session_id"))[:8]
    pid = inf.get("pid")
    started_at = inf.get("started_at")
    uptime = _format_uptime(started_at) if isinstance(started_at, datetime) else "?"
    await msg.answer(f"Session {id8} · pid {pid} · running {uptime}")


@router.message(Command("who"))
async def cmd_who(
    msg: Message,
    db_factory: async_sessionmaker[AsyncSession],
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name, not positionally
) -> None:
    """Show pairing status. Owners see the full table; friends see the count + owner handle."""
    if not is_paired_user or msg.from_user is None:
        # Defensive — middleware already short-circuits unpaired senders.
        return

    async with db_factory() as db:
        owner = await pairing.get_owner(db)
        is_owner = owner is not None and owner.tg_user_id == msg.from_user.id

        if is_owner:
            users = await pairing.list_paired(db)
            lines = ["<b>Paired users</b>"]
            for u in users:
                handle = f"@{html.escape(u.tg_username)}" if u.tg_username else "—"
                role = "owner" if u.is_owner else "friend"
                state = "revoked" if u.revoked_at is not None else "active"
                lines.append(
                    f"• <code>{u.tg_user_id}</code> {handle} ({role}, {state})",
                )
            await msg.answer("\n".join(lines))
            return

        users = await pairing.list_paired(db)
        active = [u for u in users if u.revoked_at is None]
        owner_handle = (
            f"@{html.escape(owner.tg_username)}" if owner is not None and owner.tg_username else "—"
        )
        await msg.answer(
            f"Paired users: {len(active)}.\nOwner: {owner_handle}",
        )


@router.message()
async def handle_text(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],  # noqa: ARG001 — kept for parity with siblings
) -> None:
    """Forward free-text input to the manager: starts a session if idle."""
    if msg.from_user is None or msg.text is None:
        return

    status = await session_manager.status()
    try:
        if status == SessionStatus.IDLE:
            await session_manager.new_session(
                prompt=msg.text,
                started_by_tg_user_id=msg.from_user.id,
            )
        else:
            await session_manager.send(msg.text)
    except NoActiveSessionError:
        await msg.answer("No active session.")
        return
    except SessionError as exc:
        await msg.answer(html.escape(str(exc)))
        return
    await msg.answer("Forwarded.")


__all__ = ["router"]
