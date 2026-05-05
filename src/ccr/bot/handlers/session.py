"""Session lifecycle handlers — `/new`, `/stop`, `/clear`, `/who`, plain text.

The :class:`SessionManager` and the DB factory are passed in via aiogram
workflow data (``dp["session_manager"] = ...`` etc.) so handlers stay free
of module-level singletons. Reply strings are deliberately stable — tests
match them verbatim.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from sqlalchemy import select

from ccr.auth import pairing
from ccr.bot.formatting import chunk_text
from ccr.bot.notify import broadcast_paired
from ccr.claude.manager import (
    NoActiveSessionError,
    NoPriorSessionError,
    SessionAlreadyRunningError,
    SessionError,
    SessionNotFoundError,
)
from ccr.claude.state import SessionStatus
from ccr.db.models import PairedUser, Session
from ccr.utils import format_user_datetime

if TYPE_CHECKING:
    from aiogram.types import Message
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ccr.claude.manager import SessionManager


router = Router(name="session")

_SECONDS_PER_MINUTE = 60
_SESSIONS_LIMIT = 20
_FIRST_PROMPT_DISPLAY_TRUNCATE = 60
_EMPTY_SESSIONS_REPLY = "(no sessions)"
_HEX8_RE = re.compile(r"^[0-9a-f]{8}$")
_INVALID_CONTINUE_ARG_REPLY = (
    "Invalid session id. Expected 8 hex characters (e.g. /continue 76581b99)."
)
_DIVIDER_MESSAGE = "— — — new session — — —"


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


@router.message(Command("continue"))
async def cmd_continue(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],  # noqa: ARG001 — kept for parity with siblings
) -> None:
    """Resume a finished Claude session — most recent, or one matched by 8-hex prefix."""
    if msg.from_user is None:
        return

    raw = msg.text or ""
    args = raw.removeprefix("/continue").strip()
    session_id_prefix: str | None
    if args == "":
        session_id_prefix = None
    elif _HEX8_RE.match(args):
        session_id_prefix = args
    else:
        await msg.answer(_INVALID_CONTINUE_ARG_REPLY)
        return

    try:
        session_id = await session_manager.continue_session(
            started_by_tg_user_id=msg.from_user.id,
            session_id_prefix=session_id_prefix,
        )
    except SessionAlreadyRunningError as exc:
        # Fixed canned string from the exception — passed verbatim, no escape.
        await msg.answer(str(exc))
        return
    except NoPriorSessionError as exc:
        await msg.answer(str(exc))
        return
    except SessionNotFoundError as exc:
        # Message embeds only the validated 8-hex prefix — no HTML escape needed.
        await msg.answer(str(exc))
        return
    except SessionError as exc:
        await msg.answer(html.escape(str(exc)))
        return
    inf = await session_manager.info()
    pid = inf.get("pid")
    await msg.answer(f"Session {_short_id(session_id)} resumed (pid {pid}).")


@router.message(Command("clear"))
async def cmd_clear(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Stop any running session, post a divider, then start a fresh empty one.

    The divider broadcast is gated on ``prior_status != IDLE`` (read before
    :meth:`SessionManager.stop`) so a ``/clear`` from a clean state does not
    drop a misleading "new session" boundary into chat with nothing above it.
    The broadcast is sent before :meth:`SessionManager.new_session` so the new
    session's first events land *after* the divider in chat order.
    """
    if msg.from_user is None:
        return

    prior_status = await session_manager.status()
    await session_manager.stop()

    if prior_status != SessionStatus.IDLE and msg.bot is not None:
        async with db_factory() as db:
            await broadcast_paired(msg.bot, db, _DIVIDER_MESSAGE)

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


@router.message(Command("sessions"))
async def cmd_sessions(
    msg: Message,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """List the most recent sessions (up to 20), newest first.

    Each line is ``<id8> · <status> · <started_at> · <first_prompt> · by <user>``,
    where the timestamp is rendered via :func:`format_user_datetime` in
    ``"short"`` mode using the calling user's timezone preference (or UTC when
    the caller has none / is unpaired). Replies with the stable empty-state
    string ``"(no sessions)"`` when the DB has no rows. The body is chunked
    through :func:`chunk_text` so very long listings stay below Telegram's
    4096-char limit.
    """
    user: PairedUser | None = None
    async with db_factory() as db:
        if msg.from_user is not None:
            user = (
                await db.scalars(
                    select(PairedUser).where(PairedUser.tg_user_id == msg.from_user.id),
                )
            ).first()
        rows = (
            await db.scalars(
                select(Session).order_by(Session.started_at.desc()).limit(_SESSIONS_LIMIT),
            )
        ).all()

    if not rows:
        await msg.answer(_EMPTY_SESSIONS_REPLY)
        return

    lines = [_format_session_row(row, user) for row in rows]
    body = "\n".join(lines)
    for chunk in chunk_text(body):
        await msg.answer(chunk)


def _format_session_row(row: Session, user: PairedUser | None) -> str:
    id8 = str(row.id)[:8]
    status = html.escape(row.status)
    started = format_user_datetime(row.started_at, user, "short")
    prompt_display = (
        "—"
        if row.first_prompt is None
        else html.escape(row.first_prompt[:_FIRST_PROMPT_DISPLAY_TRUNCATE])
    )
    by = "—" if row.started_by_tg_user_id is None else html.escape(str(row.started_by_tg_user_id))
    return f"<code>{id8}</code> · {status} · {started} · {prompt_display} · by {by}"


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],  # noqa: ARG001 — kept for parity with siblings
) -> None:
    """Forward free-text input to the manager: starts a session if idle.

    Slash-prefixed messages are intentionally excluded so unhandled
    commands fall through to the passthrough router (see CCR-010); a
    user typing ``/cost`` should hit ``send_slash``, not ``send``.
    """
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


__all__ = ["cmd_continue", "cmd_sessions", "router"]
