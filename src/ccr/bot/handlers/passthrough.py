"""Slash-command passthrough whitelist — `/cost`, `/model`, `/compact`, etc.

This router is the *last* one registered on the dispatcher (see
``ccr.bot.app.build_dispatcher``). aiogram resolves routers in registration
order, so by the time a slash-command update reaches this router every
command claimed by the pairing / session / permission routers has already
been handled. Anything left over is matched here by the regex-Command
filter ``Command(re.compile(r".+"))`` and routed into one of three
branches:

* whitelisted (`/cost`, `/model`, `/compact`) — forwarded to Claude as a
  user turn via :meth:`SessionManager.send_slash`. Claude Code interprets
  the leading slash exactly as if it were typed in its TTY.
* blocked-interactive (`/agents`, `/mcp`, `/init`) — replied with a fixed
  string telling the user to run the command in their local terminal.
* unknown — replied with the canonical usage hint listing every slash
  command we currently expose. Phase 13 commands (`/view`, `/last`,
  `/preview`) are intentionally listed even before they are implemented
  to keep the message stable across releases.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command, CommandObject

from ccr.claude.manager import NoActiveSessionError

if TYPE_CHECKING:
    from aiogram.types import Message

    from ccr.claude.manager import SessionManager


WHITELIST: frozenset[str] = frozenset({"cost", "model", "compact"})
BLOCKED_INTERACTIVE: frozenset[str] = frozenset({"agents", "mcp", "init"})

_UNKNOWN_USAGE_HINT = (
    "Unknown command. Whitelisted: /new /stop /clear /view /last /preview "
    "/cost /model /compact /who."
)


router = Router(name="passthrough")


@router.message(Command(re.compile(r".+")))
async def cmd_passthrough(
    msg: Message,
    command: CommandObject,
    session_manager: SessionManager,
) -> None:
    """Route any otherwise-unclaimed slash command into one of three branches."""
    name = command.command
    args = command.args or ""

    if name in WHITELIST:
        try:
            await session_manager.send_slash(name, args)
        except NoActiveSessionError:
            await msg.answer("No active session.")
        return

    if name in BLOCKED_INTERACTIVE:
        await msg.answer(
            f"Interactive command — run /{html.escape(name)} in your local Claude Code terminal.",
        )
        return

    await msg.answer(_UNKNOWN_USAGE_HINT)


__all__ = ["BLOCKED_INTERACTIVE", "WHITELIST", "cmd_passthrough", "router"]
