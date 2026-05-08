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
_EMPTY_SESSIONS_REPLY = "(no sessions)"
_UNNAMED_PLACEHOLDER = "(unnamed)"
# CCR-042 fix-loop: NULL ``claude_session_id`` rows render this marker in
# place of an 8-hex prefix so users see immediately that the row is not
# addressable via ``/continue``. 8 dashes were chosen so the column stays
# aligned with the 8-hex prefix used for non-NULL rows; the marker is
# unambiguous (no 8-hex string equals 8 dashes).
_NULL_CLAUDE_SESSION_ID_MARKER = "--------"
_HEX8_RE = re.compile(r"^[0-9a-f]{8}$")
_INVALID_CONTINUE_ARG_REPLY = (
    "Invalid session id. Expected 8 hex characters (e.g. /continue 76581b99)."
)
_DIVIDER_MESSAGE = "— — — new session — — —"

# CCR-037: cap on ``Session.name`` written by ``/rename``. Mirrors
# ``ccr.claude.manager._SESSION_NAME_MAX_LEN`` so the auto-fill and the
# manual rename produce names of the same length budget; the listing
# alignment stays predictable. Duplicated rather than imported because
# the bot layer does not otherwise import the manager module for value
# constants.
_SESSION_NAME_MAX_LEN = 40

_RENAME_USAGE_HINT = (
    "Usage: /rename &lt;8-hex-prefix&gt; &lt;name&gt; "
    "or /rename current &lt;name&gt;. "
    "Example: /rename 76581b99 Refactor pairing flow."
)
_RENAME_AMBIGUOUS_REPLY = (
    "Multiple sessions match that prefix. Pass a longer prefix or use /sessions to disambiguate."
)
_RENAME_NO_ACTIVE_SESSION_REPLY = "No active session to rename."
# CCR-043: distinct from ``_RENAME_NO_ACTIVE_SESSION_REPLY`` so callers can
# tell the two pre-mutation error states apart. The CCR-044 ``[idle]``
# lifecycle window is the only path that lands here: a subprocess is held
# but ``SystemInit`` has not yet fired, so the row exists with
# ``claude_session_id IS NULL`` and there is nothing addressable to rename.
_RENAME_IDLE_ACTIVE_SESSION_REPLY = (
    "Active session has no claude_session_id yet — try again after it starts."
)


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
        await session_manager.new_session(
            prompt=None,
            started_by_tg_user_id=msg.from_user.id,
        )
    except SessionError as exc:
        await msg.answer(html.escape(str(exc)))
        return
    inf = await session_manager.info()
    pid = inf.get("pid")
    await msg.answer(f"New session started (pid {pid}).")


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
        await session_manager.continue_session(
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
    await msg.answer(f"Session resumed (pid {pid}).")


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
        await session_manager.new_session(
            prompt=None,
            started_by_tg_user_id=msg.from_user.id,
        )
    except SessionError as exc:
        await msg.answer(html.escape(str(exc)))
        return
    inf = await session_manager.info()
    pid = inf.get("pid")
    await msg.answer(f"New session started (pid {pid}).")


@router.message(Command("pid"))
async def cmd_pid(
    msg: Message,
    session_manager: SessionManager,
) -> None:
    """Reply with the subprocess pid and uptime for the active session.

    CCR-045: the local-UUID 8-hex prefix was dropped from the reply —
    the local ``Session.id`` is internal, and `/pid` is by definition
    a single-active-session command, so the implicit "this session"
    is unambiguous.

    CCR-044: the "No active session." branch covers two cases that look
    identical from the user's perspective:

    1. No subprocess is held — ``info()`` returns ``status=IDLE`` with
       every other field ``None``.
    2. A subprocess is held but ``SystemInit`` has not yet fired —
       ``info()`` returns ``status=IDLE`` with a non-``None`` ``session_id``
       / ``pid`` / ``started_at``.

    Both reach the same reply because Claude is not yet usable from the
    user's perspective in case (2); presenting it as "running" would be
    misleading. CCR-043's ``/rename current`` is the only command that
    distinguishes the two states.
    """
    inf = await session_manager.info()
    if inf.get("status") == SessionStatus.IDLE or inf.get("session_id") is None:
        await msg.answer("No active session.")
        return
    pid = inf.get("pid")
    started_at = inf.get("started_at")
    uptime = _format_uptime(started_at) if isinstance(started_at, datetime) else "?"
    # CCR-045: drop the local-UUID 8-hex prefix; pid is the user-visible
    # handle for the active subprocess. The local ``Session.id`` is
    # internal — see CCR-045 in the chat-bot BRIEF.
    await msg.answer(f"Session running · pid {pid} · uptime {uptime}")


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

    Each line is
    ``<id8> · <status> · <name> · HH:MM DD-MM · by @<username>``, where
    ``<id8>`` is the first 8 hex characters of ``Session.claude_session_id``
    (CCR-042 — addresses chat sessions by the Claude id rather than the local
    row UUID). Rows where ``claude_session_id IS NULL`` (legacy /
    pre-CCR-041 / failed-init) render the literal marker
    ``--------`` (8 dashes) in the same slot — explicitly NOT a local-UUID
    prefix. The marker signals "this row is not addressable via
    ``/continue``": the manager's prefix-match path skips NULL rows, so any
    8-hex prefix the listing surfaces is guaranteed to be resolvable by
    ``/continue``. The full UUID is no longer rendered. Resume chains
    (multiple rows sharing one ``claude_session_id`` after CCR-041) all show
    the same 8-hex prefix — that is intended; ``/continue <prefix>`` re-uses
    it. The timestamp is rendered via :func:`format_user_datetime` in
    ``"short"`` mode using the calling user's timezone preference (or UTC
    when the caller has none / is unpaired). The name slot uses
    ``Session.name`` (auto-filled from the first user prompt by
    :class:`SessionManager`, overwritable via ``/rename``); rows with
    ``NULL`` name render ``(unnamed)``. The "by" slot is ``by @<username>``
    (joined on ``paired_users`` keyed by ``started_by_tg_user_id``); a
    missing/no-username paired row falls back to ``by <tg_user_id>``.
    Replies with the stable empty-state string ``"(no sessions)"`` when the
    DB has no rows. The body is chunked through :func:`chunk_text` so very
    long listings stay below Telegram's 4096-char limit.

    CCR-044: rows with ``status='idle'`` are filtered out at the SQL layer
    — they have not yet acquired a ``claude_session_id`` and are not
    addressable via ``/continue`` / ``/rename``. The filter sits in the
    ``WHERE`` clause so ``_SESSIONS_LIMIT=20`` continues to bound 20
    displayed rows, never "20 candidates of which some get skipped".
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
                select(Session)
                .where(Session.status != SessionStatus.IDLE.value)
                .order_by(Session.started_at.desc())
                .limit(_SESSIONS_LIMIT),
            )
        ).all()
        starter_ids = {
            row.started_by_tg_user_id for row in rows if row.started_by_tg_user_id is not None
        }
        usernames: dict[int, str | None] = {}
        if starter_ids:
            paired_rows = (
                await db.scalars(
                    select(PairedUser).where(PairedUser.tg_user_id.in_(starter_ids)),
                )
            ).all()
            usernames = {p.tg_user_id: p.tg_username for p in paired_rows}

    if not rows:
        await msg.answer(_EMPTY_SESSIONS_REPLY)
        return

    lines = [_format_session_row(row, user, usernames) for row in rows]
    body = "\n".join(lines)
    for chunk in chunk_text(body):
        await msg.answer(chunk)


def _format_session_row(
    row: Session,
    user: PairedUser | None,
    usernames: dict[int, str | None],
) -> str:
    if row.claude_session_id is not None:
        # CCR-042: render the 8-hex prefix of ``claude_session_id`` so the
        # listing stays compact and the prefix matches the one ``/continue``
        # accepts. ``claude_session_id`` is stored as a Python ``str``
        # (UUID-formatted) per CCR-036 — slice the str directly with
        # ``[:8]``; the ``uuid.UUID`` ``.hex`` accessor only applies to
        # the internal ``Session.id`` UUID, which is no longer surfaced.
        session_id_label = html.escape(row.claude_session_id[:8])
    else:
        # CCR-042 fix-loop: rows with ``claude_session_id IS NULL`` are not
        # addressable via ``/continue`` (the manager's prefix-match arm
        # skips NULL rows). Render a marker rather than the local-UUID
        # prefix so the listing never surfaces a token that ``/continue``
        # cannot resolve. This is the alignment invariant: every 8-hex
        # prefix shown is matchable; the marker is the only "non-prefix"
        # value the slot can hold.
        #
        # CCR-044: this marker is now a legacy-only artefact. Going-forward
        # ``/new`` rows are inserted ``status='idle'`` and remain idle until
        # ``_update_claude_session_id`` lands a non-NULL ``claude_session_id``
        # in the same UPDATE that flips ``status`` to ``'running'``; the
        # ``cmd_sessions`` query filter excludes ``idle`` rows entirely. The
        # only NULL rows that reach this branch are pre-CCR-044 legacy
        # rows whose terminal status is ``stopped`` / ``completed`` /
        # ``crashed`` and whose ``_update_claude_session_id`` write never
        # landed before the row finalised.
        session_id_label = _NULL_CLAUDE_SESSION_ID_MARKER
    status = html.escape(row.status)
    name_display = _UNNAMED_PLACEHOLDER if row.name is None else html.escape(row.name)
    started = format_user_datetime(row.started_at, user, "short")
    by_display = _format_started_by(row.started_by_tg_user_id, usernames)
    return f"<code>{session_id_label}</code> · {status} · {name_display} · {started} · {by_display}"


def _format_started_by(
    started_by_tg_user_id: int | None,
    usernames: dict[int, str | None],
) -> str:
    """Render the trailing ``by …`` slot for a ``/sessions`` row.

    Uses ``paired_users.tg_username`` keyed on ``started_by_tg_user_id``
    when available (rendered as ``by @<username>``). Falls back to
    ``by <tg_user_id>`` when the user is unpaired, paired but has no
    ``@username``, or the starter id is ``NULL`` entirely (in which case
    ``by —`` is emitted to mirror the prior unknown-starter rendering).
    Per CCR-037 the ``first_name`` column is explicitly NOT consulted.
    """
    if started_by_tg_user_id is None:
        return "by —"
    username = usernames.get(started_by_tg_user_id)
    if username:
        return f"by @{html.escape(username)}"
    return f"by {started_by_tg_user_id}"


@router.message(Command("rename"))
async def cmd_rename(
    msg: Message,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Manually rename a session.

    Two forms are accepted:

    * ``/rename <8-hex-prefix> <name>`` — prefix is matched against the
      first 8 hex characters of ``Session.claude_session_id`` (CCR-043,
      same convention ``/continue`` uses since CCR-042). Rows where
      ``claude_session_id IS NULL`` are skipped from prefix matching;
      they are not addressable. When the prefix matches a resume chain
      (multiple ``Session`` rows sharing one ``claude_session_id`` per
      CCR-041), every row in the chain is renamed in one commit so the
      ``/sessions`` listing stays consistent.
    * ``/rename current <name>`` — renames the active session's chain.
      ``current`` is detected before the 8-hex regex check so the literal
      keyword is never rejected as a malformed prefix. The active session
      is read from :meth:`SessionManager.info`. Two pre-mutation error
      states reply with distinct strings: no subprocess at all
      (``"No active session to rename."``) versus a subprocess held in
      the CCR-044 ``[idle]`` window where ``claude_session_id`` has not
      yet been populated (the idle-state reply is deliberately different
      so callers can tell the two situations apart).

    The new name is truncated to ``_SESSION_NAME_MAX_LEN`` characters and
    overwrites whatever ``Session.name`` previously held (NULL or another
    value) — manual rename always wins. Empty / missing args, an invalid
    prefix, an unknown prefix, an all-NULL match set, and the two
    ``current`` error states each return a stable, non-mutating reply.
    """
    if msg.from_user is None or msg.text is None:
        return

    raw_args = msg.text.removeprefix("/rename").strip()
    parts = raw_args.split(maxsplit=1)
    if parts and parts[0] == "current":
        await _handle_rename_current(
            msg,
            session_manager=session_manager,
            db_factory=db_factory,
            raw_name=parts[1] if len(parts) > 1 else "",
        )
        return

    parsed = _parse_rename_args(msg.text)
    if parsed is None:
        await msg.answer(_RENAME_USAGE_HINT)
        return
    prefix, new_name = parsed

    async with db_factory() as db:
        # Load all session rows newest-first and filter by 8-hex prefix in
        # Python — the column is stored as a 32-char UUID hex blob and
        # SQLAlchemy's ``Uuid(as_uuid=True)`` does not expose a portable
        # substring operator. The ``Session`` table is bounded (one row
        # per Claude session ever started on this host) so this is cheap
        # in practice; mirrors :meth:`SessionManager._db_lookup_resumable_claude_session_id`'s
        # full-scan + Python-side prefix filter.
        rows = (
            await db.scalars(
                select(Session).order_by(Session.started_at.desc()),
            )
        ).all()
        # CCR-043: skip rows with NULL ``claude_session_id`` *before* the
        # ``[:8]`` slice — slicing ``None`` raises ``TypeError``. NULL rows
        # are not addressable via the prefix surface (mirrors
        # ``_db_lookup_resumable_claude_session_id``'s skip rule), so they
        # never participate in prefix matching.
        matches = [
            row
            for row in rows
            if row.claude_session_id is not None and row.claude_session_id[:8] == prefix
        ]
        if not matches:
            await msg.answer(f"No session found with id {html.escape(prefix)}.")
            return
        # CCR-043: every row sharing the matched ``claude_session_id``
        # gets renamed (resume-chain rename completeness — all rows in
        # the chain show the same prefix in ``/sessions``, so renaming
        # only one would silently desync the listing).
        target_claude_id = matches[0].claude_session_id
        chain = [row for row in rows if row.claude_session_id == target_claude_id]
        for row in chain:
            row.name = new_name
        await db.commit()

    await msg.answer(
        f"Session <code>{prefix}</code> renamed to {html.escape(new_name)}.",
    )


async def _handle_rename_current(
    msg: Message,
    *,
    session_manager: SessionManager,
    db_factory: async_sessionmaker[AsyncSession],
    raw_name: str,
) -> None:
    """``/rename current <name>`` branch — rename the active session's chain.

    See :func:`cmd_rename` for the two-error-state contract; the strings
    ``_RENAME_NO_ACTIVE_SESSION_REPLY`` and
    ``_RENAME_IDLE_ACTIVE_SESSION_REPLY`` MUST stay distinct so callers
    can distinguish "nothing held" from "held but pre-SystemInit".
    """
    new_name = _truncate_session_name_for_rename(raw_name)
    if not new_name:
        await msg.answer(_RENAME_USAGE_HINT)
        return

    inf = await session_manager.info()
    if inf.get("session_id") is None:
        await msg.answer(_RENAME_NO_ACTIVE_SESSION_REPLY)
        return
    if inf.get("status") == SessionStatus.IDLE:
        # CCR-044 [idle] window: subprocess held but ``SystemInit`` has
        # not fired yet. ``claude_session_id`` is NULL on the live row;
        # rename has nothing to target.
        await msg.answer(_RENAME_IDLE_ACTIVE_SESSION_REPLY)
        return

    local_session_id = inf.get("session_id")
    async with db_factory() as db:
        active_row = await db.scalar(
            select(Session).where(Session.id == local_session_id),
        )
        if active_row is None or active_row.claude_session_id is None:
            # Defensive: the manager reports a non-idle, non-NULL-id
            # session but the DB row is missing or its
            # ``claude_session_id`` is NULL. Treat as the idle case (the
            # intent — "no claude id to address") rather than mutating
            # nothing silently.
            await msg.answer(_RENAME_IDLE_ACTIVE_SESSION_REPLY)
            return
        target_claude_id = active_row.claude_session_id
        chain_rows = (
            await db.scalars(
                select(Session).where(Session.claude_session_id == target_claude_id),
            )
        ).all()
        for row in chain_rows:
            row.name = new_name
        await db.commit()

    prefix = target_claude_id[:8]
    await msg.answer(
        f"Session <code>{html.escape(prefix)}</code> renamed to {html.escape(new_name)}.",
    )


_RENAME_ARG_PARTS = 2


def _parse_rename_args(text: str) -> tuple[str, str] | None:
    """Parse ``/rename <prefix> <name>`` into ``(prefix, truncated_name)``.

    Returns ``None`` for any input that does not yield exactly an 8-hex
    prefix plus a non-empty name (after stripping and length-capping).
    The caller renders :data:`_RENAME_USAGE_HINT` on ``None``. Splitting
    is ``maxsplit=1`` so the name may contain spaces.
    """
    args = text.removeprefix("/rename").strip()
    if not args:
        return None
    parts = args.split(maxsplit=1)
    if len(parts) < _RENAME_ARG_PARTS:
        return None
    prefix, raw_name = parts[0], parts[1].strip()
    if not raw_name or not _HEX8_RE.match(prefix):
        return None
    new_name = _truncate_session_name_for_rename(raw_name)
    if not new_name:
        return None
    return prefix, new_name


def _truncate_session_name_for_rename(text: str) -> str:
    """Apply the same length cap used by manager auto-fill to ``/rename`` input.

    Whitespace is collapsed, the result is stripped, and anything beyond
    ``_SESSION_NAME_MAX_LEN`` characters is hard-cut with a trailing
    ellipsis (U+2026). Empty input (after stripping) returns the empty
    string; the caller treats that as a usage error.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    if len(cleaned) <= _SESSION_NAME_MAX_LEN:
        return cleaned
    return cleaned[: _SESSION_NAME_MAX_LEN - 1] + "…"


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


__all__ = ["cmd_continue", "cmd_rename", "cmd_sessions", "router"]
