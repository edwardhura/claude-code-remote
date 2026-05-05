"""``/config`` command + ``cfg:*`` callback handlers (CCR-034).

The ``/config`` menu is the single entry point for per-user preferences
stored on the :class:`PairedUser` row. Today the only entry is
``Timezone``; the surface is built so that future preferences (display
name fallback, etc.) can slot in without re-shaping the keyboard.

Three flows:

* ``/config`` — opens an inline keyboard with at minimum ``Timezone`` and
  ``Close``. Allowlist-gated by :class:`AllowlistMiddleware` so an
  unpaired sender never reaches this handler.
* ``/config tz <IANA name>`` — free-text fallback. Validates via
  :class:`zoneinfo.ZoneInfo`; on
  :class:`zoneinfo.ZoneInfoNotFoundError` replies with a clear error and
  does *not* touch the DB.
* Callback ``cfg:*`` — three sub-actions:

  - ``cfg:tz`` — render the timezone picker (curated short list).
  - ``cfg:tz:<index>`` — persist the chosen zone for the calling
    ``tg_user_id`` (looked up server-side; the callback payload never
    carries the target id).
  - ``cfg:close`` — edit the menu to a closed acknowledgement so no
    orphaned interactive message remains.

The ``cfg:`` prefix is namespaced away from ``perm:`` and ``auq:`` to
avoid filter collisions on the dispatcher.
"""

from __future__ import annotations

import contextlib
import html
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from ccr.db.models import PairedUser

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_CB_TZ_ROOT = "cfg:tz"
_CB_TZ_PICK = "cfg:tz:"
_CB_CLOSE = "cfg:close"

_TZ_ARG_PARTS = 2  # `/config tz <name>` splits into exactly 2 parts after maxsplit=1.

# Curated short list of common IANA timezones. Buttons reference the
# zone by index (callback_data caps at 64 bytes — short list keeps the
# index < 100). Free-text fallback via ``/config tz <IANA name>`` covers
# anything not on this list.
_CURATED_ZONES: tuple[str, ...] = (
    "UTC",
    "US/Eastern",
    "US/Central",
    "US/Pacific",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Kyiv",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Singapore",
    "Australia/Sydney",
    "America/Sao_Paulo",
)

_MENU_TITLE = "<b>Config</b>\nPick a setting to change."
_TZ_PICKER_TITLE = (
    "<b>Timezone</b>\n"
    "Pick one of the curated zones, or use "
    "<code>/config tz &lt;IANA name&gt;</code> for any other zone."
)
_CLOSED_REPLY = "Menu closed."
_USAGE_HINT = (
    "Usage:\n"
    "<code>/config</code> — open the config menu\n"
    "<code>/config tz &lt;IANA name&gt;</code> — set timezone (e.g. "
    "<code>/config tz Europe/Berlin</code>)"
)
_INVALID_TZ_REPLY = (
    "Unknown timezone: <code>{name}</code>. Use an IANA name "
    "(e.g. <code>Europe/Berlin</code>, <code>America/New_York</code>)."
)


router = Router(name="config")


def _menu_kb() -> InlineKeyboardMarkup:
    """Top-level menu: one row per setting + a row with Close."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Timezone", callback_data=_CB_TZ_ROOT)],
            [InlineKeyboardButton(text="Close", callback_data=_CB_CLOSE)],
        ],
    )


def _tz_picker_kb() -> InlineKeyboardMarkup:
    """Timezone picker: one row per curated zone + a final Close row.

    Each button's ``callback_data`` is ``cfg:tz:<index>`` where ``index``
    points into :data:`_CURATED_ZONES`. Looking up by index keeps payload
    well under Telegram's 64-byte cap and avoids embedding user-controlled
    strings into ``callback_data``.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=zone,
                callback_data=f"{_CB_TZ_PICK}{idx}",
            ),
        ]
        for idx, zone in enumerate(_CURATED_ZONES)
    ]
    rows.append([InlineKeyboardButton(text="Close", callback_data=_CB_CLOSE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _set_timezone(
    db_factory: async_sessionmaker[AsyncSession],
    tg_user_id: int,
    zone_name: str,
) -> bool:
    """Persist ``zone_name`` on the paired_users row for ``tg_user_id``.

    Returns ``True`` on a successful update, ``False`` when the user is
    not on the allowlist (defence-in-depth — middleware already rejects
    that case, but the handler must not crash if a race opens it up).
    """
    async with db_factory() as db:
        row = await db.scalar(
            select(PairedUser).where(PairedUser.tg_user_id == tg_user_id),
        )
        if row is None:
            return False
        row.timezone = zone_name
        await db.commit()
        return True


def _validate_zone(name: str) -> bool:
    """Return ``True`` iff ``name`` resolves to a real IANA timezone."""
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return False
    return True


@router.message(Command("config"))
async def cmd_config(
    msg: Message,
    db_factory: async_sessionmaker[AsyncSession],
    command: CommandObject,
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name
) -> None:
    """Open the menu, or honour the ``/config tz <IANA name>`` free-text fallback."""
    if not is_paired_user or msg.from_user is None:
        # Defensive — middleware short-circuits unpaired senders before
        # we get here, but keep the handler safe in tests that bypass
        # the middleware.
        return

    args = (command.args or "").strip()
    if not args:
        await msg.answer(_MENU_TITLE, reply_markup=_menu_kb())
        return

    parts = args.split(maxsplit=1)
    sub = parts[0].lower()
    if sub == "tz" and len(parts) == _TZ_ARG_PARTS:
        zone_name = parts[1].strip()
        if not _validate_zone(zone_name):
            await msg.answer(
                _INVALID_TZ_REPLY.format(name=html.escape(zone_name)),
            )
            return
        await _set_timezone(db_factory, msg.from_user.id, zone_name)
        await msg.answer(f"Timezone set to <code>{html.escape(zone_name)}</code>.")
        return

    await msg.answer(_USAGE_HINT)


@router.callback_query(F.data == _CB_TZ_ROOT)
async def cb_open_tz_picker(
    cb: CallbackQuery,
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name
) -> None:
    """Render the timezone picker by editing the menu message in place."""
    if not is_paired_user:
        await cb.answer("Not paired.", show_alert=True)
        return
    if cb.message is not None and hasattr(cb.message, "edit_text"):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(_TZ_PICKER_TITLE, reply_markup=_tz_picker_kb())
    await cb.answer()


@router.callback_query(F.data.startswith(_CB_TZ_PICK))
async def cb_pick_tz(
    cb: CallbackQuery,
    db_factory: async_sessionmaker[AsyncSession],
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name
) -> None:
    """Persist the chosen timezone for the calling ``tg_user_id``."""
    if not is_paired_user:
        await cb.answer("Not paired.", show_alert=True)
        return
    if cb.data is None or cb.from_user is None:
        return

    idx_str = cb.data[len(_CB_TZ_PICK) :]
    try:
        idx = int(idx_str)
    except ValueError:
        await cb.answer("Stale prompt", show_alert=True)
        return
    if not 0 <= idx < len(_CURATED_ZONES):
        await cb.answer("Stale prompt", show_alert=True)
        return

    zone_name = _CURATED_ZONES[idx]
    # Validate even curated entries — defence-in-depth against a future
    # typo in _CURATED_ZONES that would otherwise silently persist a bad
    # value the CCR-035 helper would have to fall back on.
    if not _validate_zone(zone_name):
        await cb.answer("Stale prompt", show_alert=True)
        return

    await _set_timezone(db_factory, cb.from_user.id, zone_name)

    if cb.message is not None and hasattr(cb.message, "edit_text"):
        confirmation = f"Timezone set to <code>{html.escape(zone_name)}</code>."
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(confirmation, reply_markup=None)
    await cb.answer()


@router.callback_query(F.data == _CB_CLOSE)
async def cb_close(
    cb: CallbackQuery,
    is_paired_user: bool,  # noqa: FBT001 — aiogram passes workflow data by name
) -> None:
    """Edit the menu message to a closed acknowledgement so no interactive surface remains."""
    if not is_paired_user:
        await cb.answer("Not paired.", show_alert=True)
        return
    if cb.message is not None and hasattr(cb.message, "edit_text"):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(_CLOSED_REPLY, reply_markup=None)
    await cb.answer()


__all__ = [
    "cb_close",
    "cb_open_tz_picker",
    "cb_pick_tz",
    "cmd_config",
    "router",
]
