"""Tests for ``ccr.bot.handlers.config`` and the ``cfg:*`` callbacks (CCR-034).

Strategy mirrors :mod:`tests.test_bot_pairing` and
:mod:`tests.test_bot_permission`: real in-memory SQLite for the DB-touching
paths (so the ``timezone`` column round-trips through SQLAlchemy / the
migration metadata) and aiogram :class:`MagicMock`-backed callback
queries for the inline-keyboard flows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from aiogram.filters import CommandObject
from aiogram.types import Chat, InlineKeyboardMarkup, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ccr.bot.handlers.config import (
    _CURATED_ZONES,
    cb_close,
    cb_open_tz_picker,
    cb_pick_tz,
    cmd_config,
)
from ccr.bot.middlewares import AllowlistMiddleware
from ccr.db.engine import AsyncSessionMaker
from ccr.db.models import Base, PairedUser

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return AsyncSessionMaker(engine)


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _make_message(
    *,
    text: str,
    user_id: int = 42,
    username: str | None = "alice",
    chat_id: int = 1000,
) -> Message:
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="A", username=username),
        text=text,
    )
    object.__setattr__(msg, "answer", AsyncMock())
    return msg


def _make_cb(
    *,
    data: str,
    user_id: int = 42,
    username: str | None = "alice",
    message_text: str = "<b>Config</b>",
) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(username=username, id=user_id)
    msg = MagicMock()
    msg.text = message_text
    msg.html_text = message_text
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()
    cb.message = msg
    return cb


def _command(args: str | None = None) -> CommandObject:
    return CommandObject(prefix="/", command="config", args=args)


async def _seed_paired(
    factory: async_sessionmaker[AsyncSession],
    *,
    tg_user_id: int,
    is_owner: bool = True,
) -> None:
    async with factory() as session:
        session.add(
            PairedUser(
                tg_user_id=tg_user_id,
                tg_username="alice",
                is_owner=is_owner,
                approved_at=datetime.now(UTC),
            ),
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# 1) /config opens a menu with `Timezone` and `Close` buttons.
# --------------------------------------------------------------------------- #


async def test_config_opens_menu_with_timezone_and_close_buttons(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    msg = _make_message(text="/config", user_id=111)
    await _seed_paired(session_factory, tg_user_id=111)

    await cmd_config(
        msg,
        db_factory=session_factory,
        command=_command(),
        is_paired_user=True,
    )

    msg.answer.assert_awaited_once()
    call = msg.answer.await_args
    assert call is not None
    args, kwargs = call.args, call.kwargs
    body = args[0] if args else kwargs["text"]
    kb = kwargs["reply_markup"]
    assert "Config" in body
    assert isinstance(kb, InlineKeyboardMarkup)

    # Flatten the keyboard for a single contains-check.
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "Timezone" in labels
    assert any(label in {"Close", "Back"} for label in labels)
    assert "cfg:tz" in callbacks
    assert "cfg:close" in callbacks


# --------------------------------------------------------------------------- #
# 2) Tapping Timezone opens a picker.
# --------------------------------------------------------------------------- #


async def test_tapping_timezone_opens_picker_with_curated_zones() -> None:
    cb = _make_cb(data="cfg:tz")
    await cb_open_tz_picker(cb, is_paired_user=True)

    cb.message.edit_text.assert_awaited_once()
    args, kwargs = cb.message.edit_text.call_args
    body = args[0]
    kb = kwargs["reply_markup"]
    assert "Timezone" in body
    assert isinstance(kb, InlineKeyboardMarkup)

    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    # Every curated zone label appears.
    for zone in _CURATED_ZONES:
        assert zone in labels
    # Each pick callback is `cfg:tz:<idx>`; close is on its own row.
    assert "cfg:close" in callbacks
    assert all(cb_data == "cfg:close" or cb_data.startswith("cfg:tz:") for cb_data in callbacks)

    cb.answer.assert_awaited_once_with()


# --------------------------------------------------------------------------- #
# 3) Tapping a valid zone persists it for the calling tg_user_id.
# --------------------------------------------------------------------------- #


async def test_pick_tz_button_persists_to_paired_users_for_caller(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_paired(session_factory, tg_user_id=222)
    # Pick "Europe/Berlin" — find its index in _CURATED_ZONES.
    target = "Europe/Berlin"
    idx = _CURATED_ZONES.index(target)
    cb = _make_cb(data=f"cfg:tz:{idx}", user_id=222)

    await cb_pick_tz(
        cb,
        db_factory=session_factory,
        is_paired_user=True,
    )

    async with session_factory() as session:
        row = await session.scalar(
            select(PairedUser).where(PairedUser.tg_user_id == 222),
        )
        assert row is not None
        assert row.timezone == target

    cb.message.edit_text.assert_awaited_once()
    args, _kwargs = cb.message.edit_text.call_args
    assert target in args[0]
    cb.answer.assert_awaited_once_with()


async def test_pick_tz_uses_caller_id_not_callback_payload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The DB lookup MUST be keyed on ``cb.from_user.id``, not anything in the payload.

    Two paired users; the callback_data for any zone is the same shape
    (``cfg:tz:<idx>``) — only the calling tg_user_id distinguishes which
    row gets updated.
    """
    await _seed_paired(session_factory, tg_user_id=111)
    await _seed_paired(session_factory, tg_user_id=222, is_owner=False)
    idx = _CURATED_ZONES.index("UTC")
    cb = _make_cb(data=f"cfg:tz:{idx}", user_id=222)

    await cb_pick_tz(
        cb,
        db_factory=session_factory,
        is_paired_user=True,
    )

    async with session_factory() as session:
        rows = (await session.scalars(select(PairedUser))).all()
        by_id = {r.tg_user_id: r for r in rows}
        # Caller's row updated; the other paired user's row left at NULL.
        assert by_id[222].timezone == "UTC"
        assert by_id[111].timezone is None


async def test_pick_tz_invalid_index_rejected_no_db_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_paired(session_factory, tg_user_id=111)
    cb = _make_cb(data="cfg:tz:9999", user_id=111)

    await cb_pick_tz(
        cb,
        db_factory=session_factory,
        is_paired_user=True,
    )

    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    async with session_factory() as session:
        row = await session.scalar(
            select(PairedUser).where(PairedUser.tg_user_id == 111),
        )
        assert row is not None
        assert row.timezone is None


async def test_pick_tz_non_integer_payload_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_paired(session_factory, tg_user_id=111)
    cb = _make_cb(data="cfg:tz:abc", user_id=111)

    await cb_pick_tz(
        cb,
        db_factory=session_factory,
        is_paired_user=True,
    )

    cb.answer.assert_awaited_once_with("Stale prompt", show_alert=True)
    async with session_factory() as session:
        row = await session.scalar(
            select(PairedUser).where(PairedUser.tg_user_id == 111),
        )
        assert row is not None
        assert row.timezone is None


# --------------------------------------------------------------------------- #
# 4) /config tz <invalid name> rejected; column not updated.
# --------------------------------------------------------------------------- #


async def test_config_tz_invalid_iana_name_rejected_no_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_paired(session_factory, tg_user_id=111)
    msg = _make_message(text="/config tz Not/A/Zone", user_id=111)

    await cmd_config(
        msg,
        db_factory=session_factory,
        command=_command(args="tz Not/A/Zone"),
        is_paired_user=True,
    )

    msg.answer.assert_awaited_once()
    args, _kwargs = msg.answer.call_args
    body = args[0]
    assert "Unknown timezone" in body or "unknown" in body.lower()
    # Original payload appears in the error (HTML-escaped).
    assert "Not/A/Zone" in body

    async with session_factory() as session:
        row = await session.scalar(
            select(PairedUser).where(PairedUser.tg_user_id == 111),
        )
        assert row is not None
        assert row.timezone is None


async def test_config_tz_valid_iana_name_persists_via_free_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_paired(session_factory, tg_user_id=111)
    msg = _make_message(text="/config tz Africa/Lagos", user_id=111)

    await cmd_config(
        msg,
        db_factory=session_factory,
        command=_command(args="tz Africa/Lagos"),
        is_paired_user=True,
    )

    msg.answer.assert_awaited_once()
    async with session_factory() as session:
        row = await session.scalar(
            select(PairedUser).where(PairedUser.tg_user_id == 111),
        )
        assert row is not None
        assert row.timezone == "Africa/Lagos"


async def test_config_tz_invalid_name_html_escaped_in_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per BRIEF: HTML-escape every user-controlled string before embedding."""
    await _seed_paired(session_factory, tg_user_id=111)
    msg = _make_message(text="/config tz <script>", user_id=111)

    await cmd_config(
        msg,
        db_factory=session_factory,
        command=_command(args="tz <script>"),
        is_paired_user=True,
    )

    msg.answer.assert_awaited_once()
    args, _kwargs = msg.answer.call_args
    body = args[0]
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


# --------------------------------------------------------------------------- #
# 5) Close dismisses the menu (no orphaned interactive message).
# --------------------------------------------------------------------------- #


async def test_close_button_dismisses_menu_cleanly() -> None:
    cb = _make_cb(data="cfg:close")
    await cb_close(cb, is_paired_user=True)

    cb.message.edit_text.assert_awaited_once()
    args, kwargs = cb.message.edit_text.call_args
    # The reply markup is dropped — no interactive surface left.
    assert kwargs["reply_markup"] is None
    body = args[0]
    assert "closed" in body.lower()
    cb.answer.assert_awaited_once_with()


# --------------------------------------------------------------------------- #
# 6) Unpaired sender short-circuited by AllowlistMiddleware (regression).
# --------------------------------------------------------------------------- #


async def test_unpaired_sender_short_circuited_by_middleware(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unpaired sender's ``/config`` never reaches ``cmd_config``."""
    middleware = AllowlistMiddleware()
    handler_mock = AsyncMock()

    msg = _make_message(text="/config", user_id=4242, username="stranger")
    data: dict[str, Any] = {"db_factory": session_factory}

    result = await middleware(handler_mock, msg, data)

    assert result is None
    handler_mock.assert_not_called()
    msg.answer.assert_awaited_once_with("Not paired. Send /start to request access.")
    assert data["is_paired_user"] is False


# --------------------------------------------------------------------------- #
# Extras: usage hint + unpaired callback rejection.
# --------------------------------------------------------------------------- #


async def test_config_with_unknown_subcommand_returns_usage_hint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_paired(session_factory, tg_user_id=111)
    msg = _make_message(text="/config wat", user_id=111)

    await cmd_config(
        msg,
        db_factory=session_factory,
        command=_command(args="wat"),
        is_paired_user=True,
    )

    msg.answer.assert_awaited_once()
    args, _kwargs = msg.answer.call_args
    assert "Usage" in args[0]


async def test_open_tz_picker_unpaired_rejected() -> None:
    cb = _make_cb(data="cfg:tz")
    await cb_open_tz_picker(cb, is_paired_user=False)
    cb.answer.assert_awaited_once_with("Not paired.", show_alert=True)
    cb.message.edit_text.assert_not_called()


async def test_pick_tz_unpaired_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cb = _make_cb(data="cfg:tz:0")
    await cb_pick_tz(
        cb,
        db_factory=session_factory,
        is_paired_user=False,
    )
    cb.answer.assert_awaited_once_with("Not paired.", show_alert=True)


async def test_close_unpaired_rejected() -> None:
    cb = _make_cb(data="cfg:close")
    await cb_close(cb, is_paired_user=False)
    cb.answer.assert_awaited_once_with("Not paired.", show_alert=True)
    cb.message.edit_text.assert_not_called()


async def test_pick_tz_when_user_not_in_db_does_not_crash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Defensive: race where middleware passed but the row was concurrently deleted.

    The handler must not raise — the update silently no-ops; the
    callback still answers cleanly.
    """
    idx = _CURATED_ZONES.index("UTC")
    cb = _make_cb(data=f"cfg:tz:{idx}", user_id=999)

    await cb_pick_tz(
        cb,
        db_factory=session_factory,
        is_paired_user=True,
    )

    cb.answer.assert_awaited_once_with()
