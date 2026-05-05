"""Interactive REPL for owner-side pairing operations.

``python -m ccr console`` opens a ``ccr>`` prompt that wraps the same pure
``ccr.auth.pairing`` functions the one-shot CLI subcommands call. The REPL
adds:

* word-completion for ``pair approve <pending-code>`` and ``pair revoke
  <paired-id>`` refreshed against the DB before each prompt;
* a ``status`` command that prints DB stats and best-effort pings the
  ``/healthz`` endpoint (which lands in Phase 11 — a connection error here
  is expected and handled gracefully);
* a ``--once "<cmd>"`` non-interactive shortcut driven from
  :func:`ccr.cli._cmd_console` for scripted use and tests.

Output is plain stdout via ``sys.stdout.write`` (matching the one-shot CLI
in :mod:`ccr.cli`) so the REPL is trivially scriptable and the
``_format_table`` helper from :mod:`ccr.cli` is reused verbatim.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory

from ccr.auth import pairing
from ccr.cli import _format_paired_row, _format_pending_row, _format_table
from ccr.db.engine import AsyncSessionMaker, create_engine_from_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ccr.config import Settings


CommandHandler = Callable[[list[str], "Settings"], Awaitable[None]]


_STATIC_COMMAND_WORDS: tuple[str, ...] = (
    "pair",
    "list",
    "pending",
    "approve",
    "revoke",
    "invite",
    "session",
    "save",
    "status",
    "help",
    "exit",
    "quit",
)


_HEALTHZ_TIMEOUT_SECONDS = 2.0


def _emit(line: str) -> None:
    """Write a single line to stdout. Centralised so tests + lint stay clean."""
    sys.stdout.write(line + "\n")


def print_banner(settings: Settings) -> None:
    """Print a one-line banner before the prompt loop starts."""
    _emit(f"ccr console — type 'help' for commands  (data dir: {settings.data_dir})")


async def _with_session(
    settings: Settings,
    callback: Callable[[AsyncSession], Awaitable[None]],
) -> None:
    """Run ``callback`` inside a fresh async session bound to settings' DB.

    Mirrors the helper in :mod:`ccr.cli` but takes ``settings`` as an
    argument so the REPL can be exercised against a temp-file DB in tests.
    """
    engine = create_engine_from_settings(settings)
    factory = AsyncSessionMaker(engine)
    try:
        async with factory() as session:
            await callback(session)
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Command handlers — each takes (args_after_command_name, settings).
# --------------------------------------------------------------------------- #


async def _cmd_pair_list(_args: list[str], settings: Settings) -> None:
    async def _run(session: AsyncSession) -> None:
        rows = await pairing.list_paired(session)
        if not rows:
            _emit("(empty)")
            return
        headers = ["tg_user_id", "username", "label", "status", "approved_at"]
        formatted = [_format_paired_row(u) for u in rows]
        _emit(_format_table(headers, formatted))

    await _with_session(settings, _run)


async def _cmd_pair_pending(_args: list[str], settings: Settings) -> None:
    async def _run(session: AsyncSession) -> None:
        rows = await pairing.list_pending(session)
        if not rows:
            _emit("(empty)")
            return
        headers = ["code", "tg_user_id", "username", "created_at", "expires_at"]
        formatted = [_format_pending_row(pc) for pc in rows]
        _emit(_format_table(headers, formatted))

    await _with_session(settings, _run)


async def _cmd_pair_approve(args: list[str], settings: Settings) -> None:
    if not args:
        _emit("Usage: pair approve <code>")
        return
    code = args[0]

    async def _run(session: AsyncSession) -> None:
        try:
            user = await pairing.approve(session, code)
        except pairing.PairingError as exc:
            _emit(str(exc))
            return
        role = "owner" if user.is_owner else "paired"
        _emit(f"Approved Telegram user {user.tg_user_id} ({role})")

    await _with_session(settings, _run)


async def _cmd_pair_revoke(args: list[str], settings: Settings) -> None:
    if not args:
        _emit("Usage: pair revoke <tg_user_id>")
        return
    try:
        tg_user_id = int(args[0])
    except ValueError:
        _emit(f"Invalid tg_user_id: {args[0]!r}")
        return

    async def _run(session: AsyncSession) -> None:
        try:
            await pairing.revoke(session, tg_user_id)
        except pairing.CannotRevokeOwnerError:
            _emit("Cannot revoke owner.")
            return
        except pairing.PairingError as exc:
            _emit(str(exc))
            return
        _emit(f"Revoked Telegram user {tg_user_id}")

    await _with_session(settings, _run)


async def _cmd_pair_invite(args: list[str], settings: Settings) -> None:
    if not args:
        _emit("Usage: pair invite <tg_user_id> [label]")
        return
    try:
        tg_user_id = int(args[0])
    except ValueError:
        _emit(f"Invalid tg_user_id: {args[0]!r}")
        return
    label: str | None = args[1] if len(args) > 1 else None

    async def _run(session: AsyncSession) -> None:
        try:
            user = await pairing.invite(session, tg_user_id, label)
        except pairing.PairingError as exc:
            _emit(str(exc))
            return
        role = "owner" if user.is_owner else "paired"
        _emit(f"Invited Telegram user {user.tg_user_id} ({role})")

    await _with_session(settings, _run)


async def _cmd_session_save(args: list[str], settings: Settings) -> None:
    if not args:
        _emit("Usage: session save <claude_session_id>")
        return
    claude_session_id = args[0]

    async def _run(session: AsyncSession) -> None:
        from ccr.claude.import_session import (  # noqa: PLC0415
            ClaudeSessionFileNotFoundError,
            DuplicateClaudeSessionError,
            ImportSessionError,
            NoOwnerError,
            import_claude_session,
        )

        try:
            row = await import_claude_session(session, claude_session_id)
        except ClaudeSessionFileNotFoundError as exc:
            _emit(str(exc))
            return
        except DuplicateClaudeSessionError as exc:
            _emit(f"Already imported: {exc}")
            return
        except NoOwnerError:
            _emit("No owner registered. Pair the owner first.")
            return
        except ImportSessionError as exc:
            _emit(str(exc))
            return
        _emit(f"Imported Claude session {claude_session_id} as {row.id.hex[:8]}")

    await _with_session(settings, _run)


async def _cmd_status(_args: list[str], settings: Settings) -> None:
    db_path = settings.data_dir / "ccr.db"
    paired_count = 0
    pending_count = 0

    async def _run(session: AsyncSession) -> None:
        nonlocal paired_count, pending_count
        paired = await pairing.list_paired(session)
        paired_count = sum(1 for u in paired if u.revoked_at is None)
        pending = await pairing.list_pending(session)
        pending_count = len(pending)

    await _with_session(settings, _run)

    _emit(f"DB: {db_path}")
    _emit(f"Paired users: {paired_count}")
    _emit(f"Pending codes: {pending_count}")

    healthz_url = f"{str(settings.public_url).rstrip('/')}/healthz"
    try:
        async with httpx.AsyncClient(timeout=_HEALTHZ_TIMEOUT_SECONDS) as client:
            response = await client.get(healthz_url)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _emit(f"Server: unreachable ({type(exc).__name__})")
    except httpx.RequestError as exc:
        _emit(f"Server: unreachable ({type(exc).__name__})")
    else:
        _emit(f"Server: OK ({response.status_code})")


async def _cmd_help(_args: list[str], _settings: Settings) -> None:
    lines = (
        "Commands:",
        "  pair list                 List paired Telegram users.",
        "  pair pending              List outstanding pairing codes.",
        "  pair approve <code>       Approve a pairing code.",
        "  pair revoke <tg_user_id>  Revoke a paired Telegram user.",
        "  pair invite <tg_user_id> [label]",
        "                            Pre-approve a Telegram user (owner-only).",
        "  session save <claude_session_id>",
        "                            Import an existing local Claude session.",
        "  status                    Show DB stats and ping the web server.",
        "  help                      Show this help message.",
        "  exit | quit               Leave the console.",
    )
    for line in lines:
        _emit(line)


class ExitConsoleError(Exception):
    """Raised by the exit/quit commands to break the prompt loop."""


async def _cmd_exit(_args: list[str], _settings: Settings) -> None:
    raise ExitConsoleError


# Order matters: longest prefix wins inside :func:`dispatch`.
COMMANDS: dict[str, CommandHandler] = {
    "pair list": _cmd_pair_list,
    "pair pending": _cmd_pair_pending,
    "pair approve": _cmd_pair_approve,
    "pair revoke": _cmd_pair_revoke,
    "pair invite": _cmd_pair_invite,
    "session save": _cmd_session_save,
    "status": _cmd_status,
    "help": _cmd_help,
    "exit": _cmd_exit,
    "quit": _cmd_exit,
}


def _match_command(line: str) -> tuple[str, list[str]] | None:
    """Return ``(command_key, remaining_args)`` for the longest-matching command.

    Matching compares whitespace-normalised tokens so trailing or repeated
    spaces in user input don't defeat the lookup. Returns ``None`` when
    nothing matches.
    """
    tokens = line.split()
    if not tokens:
        return None
    # Try the 2-token prefix first, then the 1-token prefix.
    for prefix_len in (2, 1):
        if len(tokens) < prefix_len:
            continue
        candidate = " ".join(tokens[:prefix_len])
        if candidate in COMMANDS:
            return candidate, tokens[prefix_len:]
    return None


async def dispatch(line: str, settings: Settings) -> None:
    """Run one command against ``settings``.

    Empty / whitespace-only input is a no-op (matches typical REPL UX);
    unknown input prints the help hint without crashing.
    """
    stripped = line.strip()
    if not stripped:
        return
    matched = _match_command(stripped)
    if matched is None:
        _emit("Unknown command. Type 'help'.")
        return
    command_key, args = matched
    handler = COMMANDS[command_key]
    await handler(args, settings)


async def build_completer(settings: Settings) -> WordCompleter:
    """Build a :class:`WordCompleter` reflecting the current DB state.

    Pulled fresh on every prompt so newly created codes / newly paired
    users show up in tab-completion immediately.
    """
    pending_codes: list[str] = []
    paired_ids: list[str] = []

    async def _run(session: AsyncSession) -> None:
        nonlocal pending_codes, paired_ids
        pending = await pairing.list_pending(session)
        pending_codes = [pc.code for pc in pending]
        paired = await pairing.list_paired(session)
        paired_ids = [str(u.tg_user_id) for u in paired if u.revoked_at is None]

    await _with_session(settings, _run)

    words = [*_STATIC_COMMAND_WORDS, *pending_codes, *paired_ids]
    return WordCompleter(words, sentence=True)


async def run(settings: Settings, *, once: str | None = None) -> None:
    """Run the console.

    When ``once`` is provided the prompt loop is skipped entirely — exactly
    one command is dispatched and the function returns. This is the path
    used by ``python -m ccr console --once "<cmd>"`` and by the unit tests
    in ``tests/test_console.py``: it avoids wiring up prompt_toolkit's
    input/output machinery for non-interactive use.
    """
    if once is not None:
        with contextlib.suppress(ExitConsoleError):
            await dispatch(once, settings)
        return

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    history_path = settings.data_dir / "console_history"
    session: PromptSession[str] = PromptSession(
        message="ccr> ",
        history=FileHistory(str(history_path)),
    )
    print_banner(settings)
    while True:
        try:
            completer = await build_completer(settings)
            line = await session.prompt_async(completer=completer)
        except (EOFError, KeyboardInterrupt):
            return
        try:
            await dispatch(line.strip(), settings)
        except ExitConsoleError:
            return
        except Exception as exc:  # noqa: BLE001 — REPL must not crash on handler errors
            sys.stderr.write(f"Error: {exc}\n")


__all__ = [
    "COMMANDS",
    "ExitConsoleError",
    "build_completer",
    "dispatch",
    "print_banner",
    "run",
]
