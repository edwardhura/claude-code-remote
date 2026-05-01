"""Slash-command passthrough whitelist — `/cost`, `/model`, `/compact`, etc.

This router is the *last* one registered on the dispatcher (see
``ccr.bot.app.build_dispatcher``). aiogram resolves routers in registration
order, so by the time a slash-command update reaches this router every
command claimed by the pairing / session / permission routers has already
been handled. Anything left over is matched here by the regex-Command
filter ``Command(re.compile(r".+"))`` and routed into one of three
branches:

* ``/agents`` — answered locally with a Running + Library snapshot
  (subagents the live Claude session has dispatched, plus the
  ``.claude/agents/*.md`` files on disk).
* whitelisted (`/cost`, `/model`, `/compact`) — forwarded to Claude as a
  user turn via :meth:`SessionManager.send_slash`. Claude Code interprets
  the leading slash exactly as if it were typed in its TTY.
* blocked-interactive (`/mcp`, `/init`) — replied with a fixed string
  telling the user to run the command in their local terminal.
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
    from pathlib import Path

    from aiogram.types import Message

    from ccr.claude.manager import SessionManager
    from ccr.config import Settings


WHITELIST: frozenset[str] = frozenset({"cost", "model", "compact"})
BLOCKED_INTERACTIVE: frozenset[str] = frozenset({"mcp", "init"})

_UNKNOWN_USAGE_HINT = (
    "Unknown command. Whitelisted: /new /stop /clear /view /last /preview "
    "/cost /model /compact /who."
)

_AGENTS_LIBRARY_GLOB_REL = ".claude/agents"
_EMPTY_PLACEHOLDER = "(none)"


router = Router(name="passthrough")


def _list_library_agents(project_root: Path) -> list[str]:
    """Return alphabetically sorted ``.md`` filenames (without extension) under ``.claude/agents/``.

    Returns ``[]`` if the directory does not exist. Hidden files (leading
    ``.``) and non-``.md`` files are skipped. Names are NOT HTML-escaped here;
    that happens at render time.
    """
    agents_dir = project_root / _AGENTS_LIBRARY_GLOB_REL
    if not agents_dir.is_dir():
        return []
    names = [p.stem for p in agents_dir.glob("*.md") if p.is_file() and not p.name.startswith(".")]
    return sorted(names)


def _render_agents_reply(running: list[str], library: list[str]) -> str:
    def _section(title: str, items: list[str]) -> str:
        if not items:
            body = _EMPTY_PLACEHOLDER
        else:
            body = "\n".join(f"• {html.escape(name)}" for name in items)
        return f"<b>{title}</b>\n{body}"

    return f"{_section('Running', running)}\n\n{_section('Library', library)}"


async def _reply_agents(
    msg: Message,
    session_manager: SessionManager,
    settings: Settings,
) -> None:
    running = session_manager.running_subagents()
    library = _list_library_agents(settings.data_dir.parent)
    text = _render_agents_reply(running, library)
    await msg.answer(text)


@router.message(Command(re.compile(r".+")))
async def cmd_passthrough(
    msg: Message,
    command: CommandObject,
    session_manager: SessionManager,
    settings: Settings,
) -> None:
    """Route any otherwise-unclaimed slash command into one of three branches."""
    name = command.command
    args = command.args or ""

    if name == "agents":
        await _reply_agents(msg, session_manager, settings)
        return

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
