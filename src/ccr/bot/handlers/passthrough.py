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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command, CommandObject
from sqlalchemy import select

from ccr.bot.formatting import SAFE_CHUNK
from ccr.claude.manager import NoActiveSessionError
from ccr.db.models import PairedUser
from ccr.utils import format_user_datetime

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram.types import Message
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ccr.claude.events import RateLimitEvent
    from ccr.claude.manager import SessionManager
    from ccr.claude.usage import SessionUsage
    from ccr.config import Settings


WHITELIST: frozenset[str] = frozenset({"model", "compact"})
BLOCKED_INTERACTIVE: frozenset[str] = frozenset({"mcp", "init"})

# CCR-039: humanise the snake_case codes claude emits on rate_limit_event
# into a presentation-grade label. Keys are seeded with every code that
# appears in test fixtures / production today (see tests/test_claude_events.py
# and tests/test_session_manager.py). Unknown codes fall back to a
# title-case-with-spaces transform via :func:`_humanise_code` so a new
# server-side code degrades gracefully rather than rendering raw snake_case.
RATE_LIMIT_WINDOW_LABELS: dict[str, str] = {
    "five_hour": "Five hour",
    "weekly": "Weekly",
}

OVERAGE_REASON_LABELS: dict[str, str] = {
    "group_zero_credit_limit": "Group zero credit limit",
}

_UNKNOWN_USAGE_HINT = (
    "Unknown command. Whitelisted: /new /stop /clear /view /last /preview "
    "/cost /usage /model /compact /who /agents /skills /config /rename."
)

_AGENTS_LIBRARY_GLOB_REL = ".claude/agents"
_EMPTY_PLACEHOLDER = "(none)"
_DEFAULT_MODEL_VALUE = "inherit"

_SESSION_ID_HEX_PREFIX_LEN = 8
_ONE_MINUTE_MS = 60_000
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600

# Sourced from Claude Code CLI v2.1.131 — revisit when new built-ins ship.
BUILTIN_AGENTS: dict[str, str] = {
    "Explore": "haiku",
    "Plan": "inherit",
    "general-purpose": "inherit",
    "statusline-setup": "sonnet",
    "claude-code-guide": "haiku",
}

# Frontmatter is delimited by a literal ``---`` line at the start of the
# file and a closing ``---`` line. We use a stdlib-only parser (no PyYAML
# dependency) that pulls a single ``model: <value>`` line out of the block;
# anything else inside the frontmatter is ignored. Malformed blocks degrade
# silently to "no model key" — the user sees the inherit fallback rather
# than an error.
_FRONTMATTER_DELIM = "---"
_MODEL_KEY_PATTERN = re.compile(r"^\s*model\s*:\s*(.+?)\s*$")


router = Router(name="passthrough")


def _parse_frontmatter_model(text: str) -> str:
    """Return the ``model:`` value from a Markdown YAML frontmatter block.

    The frontmatter block is the leading ``---`` … ``---`` segment of the
    file. If the file does not start with ``---`` (no frontmatter), if the
    closing ``---`` is missing, or if no ``model:`` key is present, returns
    :data:`_DEFAULT_MODEL_VALUE` (``"inherit"``). Surrounding whitespace
    around the value is stripped; surrounding single or double quotes are
    NOT stripped because Claude Code's own values (``haiku``, ``sonnet``,
    ``opus``, ``inherit``) are unquoted in practice — adding quote-stripping
    here would mask a malformed config file rather than help.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return _DEFAULT_MODEL_VALUE
    closing_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIM:
            closing_idx = idx
            break
    if closing_idx is None:
        return _DEFAULT_MODEL_VALUE
    for line in lines[1:closing_idx]:
        match = _MODEL_KEY_PATTERN.match(line)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return _DEFAULT_MODEL_VALUE


def _list_library_agents(project_root: Path) -> list[tuple[str, str]]:
    """Return alphabetically sorted ``(name, model)`` pairs for project agents.

    Walks ``.claude/agents/*.md`` under ``project_root`` and parses each
    file's YAML frontmatter for a ``model:`` key. Files with no
    frontmatter, with malformed frontmatter, or with no ``model:`` key
    fall back to ``"inherit"`` to mirror Claude Code's own behaviour.
    Returns ``[]`` if the directory does not exist. Hidden files (leading
    ``.``) and non-``.md`` files are skipped. Read errors are swallowed
    and treated as ``"inherit"`` so a permission-denied or transient I/O
    blip on a single file does not bubble up to the user. Names and
    model values are NOT HTML-escaped here; that happens at render time.
    """
    agents_dir = project_root / _AGENTS_LIBRARY_GLOB_REL
    if not agents_dir.is_dir():
        return []
    pairs: list[tuple[str, str]] = []
    for path in agents_dir.glob("*.md"):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        model = _parse_frontmatter_model(content)
        pairs.append((path.stem, model))
    return sorted(pairs, key=lambda pair: pair[0])


def _render_agents_reply(
    running: list[str],
    project_agents: list[tuple[str, str]],
    builtin_agents: dict[str, str],
) -> str:
    def _running_section(items: list[str]) -> str:
        if not items:
            body = _EMPTY_PLACEHOLDER
        else:
            body = "\n".join(f"• {html.escape(name)}" for name in items)
        return f"<b>Running</b>\n{body}"

    def _agents_section(title: str, items: list[tuple[str, str]]) -> str:
        if not items:
            body = _EMPTY_PLACEHOLDER
        else:
            body = "\n".join(
                f"• {html.escape(name)} · {html.escape(model)}" for name, model in items
            )
        return f"<b>{title}</b>\n{body}"

    return "\n\n".join(
        [
            _running_section(running),
            _agents_section("Project agents", project_agents),
            _agents_section("Built-in agents", list(builtin_agents.items())),
        ],
    )


async def _reply_agents(
    msg: Message,
    session_manager: SessionManager,
    settings: Settings,
) -> None:
    running = session_manager.running_subagents()
    project_agents = _list_library_agents(settings.data_dir.parent)
    text = _render_agents_reply(running, project_agents, BUILTIN_AGENTS)
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


def _humanise_code(code: str, label_map: dict[str, str]) -> str:
    """Return a presentation-grade label for ``code``.

    If ``code`` is a key in ``label_map`` the mapped string is returned
    verbatim. Otherwise the snake_case code is transformed into a
    title-case-with-spaces fallback (e.g. ``"new_unknown_window"`` →
    ``"New Unknown Window"``) so a previously-unseen server-side code
    still renders something readable rather than raw snake_case. The
    output is plain ASCII for the seeded label set today; callers must
    still HTML-escape it before interpolating into a ``parse_mode="HTML"``
    payload because a future label or unknown code could in principle
    contain ``<`` / ``>`` / ``&``.
    """
    if code in label_map:
        return label_map[code]
    return code.replace("_", " ").title()


def _format_resets_at(epoch_seconds: int | None, user: PairedUser | None) -> str:
    """Render ``epoch_seconds`` as a localised timestamp + relative delta.

    Returns ``"unknown"`` when ``epoch_seconds`` is ``None`` or non-positive
    (claude does not emit zero/negative epochs in practice but the field
    is forward-compat optional). The absolute timestamp is rendered via
    :func:`format_user_datetime` in ``"full"`` mode using ``user``'s
    ``paired_users.timezone`` preference; ``user=None`` or
    ``user.timezone IS NULL`` fall back to UTC. For epochs in the past the
    timestamp is followed by ``"in the past"`` rather than a negative
    duration. Future epochs render as ``"in Xm Ys"`` for < 1 h and
    ``"in Xh Ym"`` otherwise.
    """
    if epoch_seconds is None or epoch_seconds <= 0:
        return "unknown"
    target = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    rendered = format_user_datetime(target, user, "full")
    delta_seconds = int(epoch_seconds - datetime.now(tz=UTC).timestamp())
    if delta_seconds <= 0:
        return f"{rendered} (in the past)"
    if delta_seconds < _SECONDS_PER_HOUR:
        minutes = delta_seconds // _SECONDS_PER_MINUTE
        seconds = delta_seconds % _SECONDS_PER_MINUTE
        return f"{rendered} (in {minutes}m {seconds}s)"
    hours = delta_seconds // _SECONDS_PER_HOUR
    minutes = (delta_seconds % _SECONDS_PER_HOUR) // _SECONDS_PER_MINUTE
    return f"{rendered} (in {hours}h {minutes}m)"


def _render_usage_reply(rl: RateLimitEvent, user: PairedUser | None) -> str:
    """Render a rich HTML ``/usage`` reply.

    Layout — every interpolated string field HTML-escaped, every field
    optional (a missing field is skipped, never rendered as the literal
    ``"None"``):

    .. code-block:: text

        <b>Rate-limit window:</b> {humanised_window}
        <b>Status:</b> {status}
        <b>Resets at:</b> {localised_dt} (in {delta})
        <b>Overage:</b> {overage_status}{ — humanised_reason }
        <b>Using overage:</b> yes|no

    Window codes (``rate_limit_type``) and overage reasons go through
    :func:`_humanise_code` against :data:`RATE_LIMIT_WINDOW_LABELS` /
    :data:`OVERAGE_REASON_LABELS` so users see ``"Five hour"`` rather
    than ``"five_hour"``; unknown codes degrade to a title-case-with-spaces
    transform.
    ``is_using_overage`` distinguishes ``False`` (renders ``"no"``) from
    ``None`` (line omitted entirely) via an ``is not None`` guard.
    The "Resets at" timestamp is rendered via
    :func:`format_user_datetime` in ``"full"`` mode using ``user``'s
    ``paired_users.timezone`` preference (UTC fallback when ``user`` is
    ``None`` or the column is ``NULL``); the relative ``(in {delta})``
    tail follows the formatted datetime.
    Output is truncated to :data:`SAFE_CHUNK` defensively, mirroring
    :func:`_render_cost_reply`.
    """
    info = rl.rate_limit_info
    if info is None:
        return "Rate-limit info unavailable."
    lines: list[str] = []
    if info.rate_limit_type:
        humanised_window = _humanise_code(info.rate_limit_type, RATE_LIMIT_WINDOW_LABELS)
        lines.append(f"<b>Rate-limit window:</b> {html.escape(humanised_window)}")
    if info.status:
        lines.append(f"<b>Status:</b> {html.escape(info.status)}")
    if info.resets_at:
        lines.append(f"<b>Resets at:</b> {_format_resets_at(info.resets_at, user)}")
    if info.overage_status:
        suffix = ""
        if info.overage_disabled_reason:
            humanised_reason = _humanise_code(
                info.overage_disabled_reason,
                OVERAGE_REASON_LABELS,
            )
            suffix = f" — {html.escape(humanised_reason)}"
        lines.append(f"<b>Overage:</b> {html.escape(info.overage_status)}{suffix}")
    if info.is_using_overage is not None:
        lines.append(f"<b>Using overage:</b> {'yes' if info.is_using_overage else 'no'}")
    if not lines:
        return "Rate-limit info unavailable."
    text = "\n".join(lines)
    if len(text) > SAFE_CHUNK:
        text = text[:SAFE_CHUNK]
    return text


async def _load_paired_user(
    db_factory: async_sessionmaker[AsyncSession] | None,
    tg_user_id: int | None,
) -> PairedUser | None:
    """Look up the calling :class:`PairedUser` row for timezone-aware rendering.

    Returns ``None`` when ``db_factory`` is missing (test harnesses calling
    handlers directly without a workflow data injection), when the caller
    has no ``from_user`` (rare for slash commands but defensive), or when
    no row matches ``tg_user_id``. ``format_user_datetime`` already handles
    ``None`` users with a UTC fallback so this never propagates an error.
    """
    if db_factory is None or tg_user_id is None:
        return None
    async with db_factory() as db:
        row: PairedUser | None = await db.scalar(
            select(PairedUser).where(PairedUser.tg_user_id == tg_user_id),
        )
        return row


async def _reply_usage(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    """Answer ``/usage`` with the most recent rate-limit snapshot.

    Three distinct states (per plan §Edge cases 2 + 3):

    1. No active Claude subprocess → ``"No active session."``.
    2. Active session but no ``rate_limit_event`` observed yet →
       ``"No rate-limit data received yet on this session."`` (distinct
       string so users and tests can tell it apart from state 1).
    3. Active session with a snapshot → :func:`_render_usage_reply` with
       the calling user's :class:`PairedUser` row threaded through so the
       "Resets at" timestamp is rendered in ``user.timezone`` (CCR-039).
    """
    if not session_manager.is_session_active():
        await msg.answer("No active session.")
        return
    rl = session_manager.current_rate_limit_status()
    if rl is None:
        await msg.answer("No rate-limit data received yet on this session.")
        return
    tg_user_id = msg.from_user.id if msg.from_user is not None else None
    user = await _load_paired_user(db_factory, tg_user_id)
    await msg.answer(_render_usage_reply(rl, user))


@router.message(Command(re.compile(r".+")))
async def cmd_passthrough(
    msg: Message,
    command: CommandObject,
    session_manager: SessionManager,
    settings: Settings,
    db_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Route any otherwise-unclaimed slash command into one of three branches.

    ``db_factory`` is injected by aiogram from ``dp["db_factory"]`` in
    production so ``/usage`` can look up the calling :class:`PairedUser`
    for timezone-aware rendering. The default ``None`` exists purely so
    tests calling this handler directly without a DB factory still
    function — production callers always provide one.
    """
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

    if name == "usage":
        await _reply_usage(msg, session_manager, db_factory)
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


__all__ = [
    "BLOCKED_INTERACTIVE",
    "BUILTIN_AGENTS",
    "OVERAGE_REASON_LABELS",
    "RATE_LIMIT_WINDOW_LABELS",
    "WHITELIST",
    "cmd_passthrough",
    "router",
]
