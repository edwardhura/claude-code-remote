"""Slash-command passthrough whitelist — `/model`, `/compact`, etc.

This router is the *last* one registered on the dispatcher (see
``ccr.bot.app.build_dispatcher``). aiogram resolves routers in registration
order, so by the time a slash-command update reaches this router every
command claimed by the pairing / session / permission routers has already
been handled. Anything left over is matched here by the regex-Command
filter ``Command(re.compile(r".+"))`` and routed into one of these
branches:

* ``/agents`` — answered locally with a Running + Library snapshot
  (subagents the live Claude session has dispatched, plus the
  ``.claude/agents/*.md`` files on disk).
* ``/skills`` — answered locally with the skill list reported by the most
  recent ``system/init`` event.
* ``/cost`` — answered locally with a rich, HTML-formatted summary derived
  by walking the current session's JSONL log via
  :func:`ccr.claude.usage.aggregate_session_usage`. Claude Code's `-p`
  non-interactive mode does NOT execute ``/cost`` as a slash command (it
  treats the leading ``/cost`` as plain user text), so forwarding via
  :meth:`SessionManager.send_slash` would just produce a confused
  assistant turn. The probe captured for CCR-023 confirmed this — see
  the ticket's Step 0 probe transcript.
* whitelisted (`/model`, `/compact`) — forwarded to Claude as a user
  turn via :meth:`SessionManager.send_slash`. Claude Code interprets
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

from ccr.bot.formatting import SAFE_CHUNK
from ccr.claude.manager import NoActiveSessionError

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram.types import Message

    from ccr.claude.manager import SessionManager
    from ccr.claude.usage import SessionUsage
    from ccr.config import Settings


WHITELIST: frozenset[str] = frozenset({"model", "compact"})
BLOCKED_INTERACTIVE: frozenset[str] = frozenset({"mcp", "init"})

_UNKNOWN_USAGE_HINT = (
    "Unknown command. Whitelisted: /new /stop /clear /view /last /preview "
    "/cost /model /compact /who /agents /skills."
)

_AGENTS_LIBRARY_GLOB_REL = ".claude/agents"
_EMPTY_PLACEHOLDER = "(none)"

_SESSION_ID_HEX_PREFIX_LEN = 8
_ONE_MINUTE_MS = 60_000


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


def _render_skills_reply(skills: list[str]) -> str:
    if not skills:
        body = _EMPTY_PLACEHOLDER
    else:
        body = "\n".join(f"• {html.escape(name)}" for name in skills)
    return f"<b>Skills</b>\n{body}"


async def _reply_skills(msg: Message, session_manager: SessionManager) -> None:
    if not session_manager.is_session_active():
        await msg.answer("No active session.")
        return
    skills = session_manager.available_skills()
    await msg.answer(_render_skills_reply(skills))


def _format_elapsed(elapsed_ms: int) -> str:
    """Render ``elapsed_ms`` as ``"2.2s"`` or ``"1m 5s"``.

    Mirrors :func:`ccr.bot.formatting._format_duration` so the ``/cost``
    elapsed segment is consistent with the per-turn ``✅ done · {s}s``
    line. ``0`` is rendered as ``"0.0s"`` rather than omitted so the user
    sees that no Claude turns have completed yet.
    """
    if elapsed_ms < _ONE_MINUTE_MS:
        return f"{elapsed_ms / 1000:.1f}s"
    minutes = elapsed_ms // _ONE_MINUTE_MS
    seconds = (elapsed_ms % _ONE_MINUTE_MS) // 1000
    return f"{minutes}m {seconds}s"


def _render_cost_reply(session_id_hex: str, usage: SessionUsage) -> str:
    """Render a rich HTML ``/cost`` reply.

    Layout — seven lines (eight when ``total_cost_usd > 0``), every
    interpolated value HTML-escaped:

    .. code-block:: text

        <b>Session:</b> {id8}
        <b>Turns:</b> {n}
        <b>Input tokens:</b> {n}
        <b>Output tokens:</b> {n}
        <b>Cache read tokens:</b> {n}
        <b>Tool calls:</b> {n}
        <b>Elapsed:</b> {duration}
        <b>Cost (USD):</b> ${cost:.4f}

    The cost line is included only when ``total_cost_usd > 0`` so users on
    a subscription plan (where Claude reports ``0.0``) do not see a stray
    ``$0.0000``. Output is truncated to :data:`SAFE_CHUNK` characters so a
    pathological ``Session:`` value cannot blow past Telegram's 4096 cap;
    the layout itself is well under the cap, the truncation is defensive.
    """
    id8 = html.escape(session_id_hex[:_SESSION_ID_HEX_PREFIX_LEN])
    lines = [
        f"<b>Session:</b> {id8}",
        f"<b>Turns:</b> {usage.num_turns}",
        f"<b>Input tokens:</b> {usage.input_tokens}",
        f"<b>Output tokens:</b> {usage.output_tokens}",
        f"<b>Cache read tokens:</b> {usage.cache_read_input_tokens}",
        f"<b>Tool calls:</b> {usage.tool_call_count}",
        f"<b>Elapsed:</b> {_format_elapsed(usage.elapsed_ms)}",
    ]
    if usage.total_cost_usd > 0:
        lines.append(f"<b>Cost (USD):</b> ${usage.total_cost_usd:.4f}")
    text = "\n".join(lines)
    if len(text) > SAFE_CHUNK:
        text = text[:SAFE_CHUNK]
    return text


async def _reply_cost(msg: Message, session_manager: SessionManager) -> None:
    """Answer ``/cost`` with a locally-computed usage summary.

    Returns ``"No active session."`` (regression: same string CCR-010
    used to emit when ``send_slash`` raised :class:`NoActiveSessionError`)
    if no Claude subprocess is currently held.
    """
    usage = session_manager.current_session_usage()
    session_id = session_manager.current_session_id
    if usage is None or session_id is None:
        await msg.answer("No active session.")
        return
    await msg.answer(_render_cost_reply(session_id.hex, usage))


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

    if name == "skills":
        await _reply_skills(msg, session_manager)
        return

    if name == "cost":
        await _reply_cost(msg, session_manager)
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
