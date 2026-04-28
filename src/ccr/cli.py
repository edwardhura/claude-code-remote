"""Argparse skeleton for `python -m ccr` and the `ccr` console script.

Most subcommand handlers still raise :class:`NotImplementedError`; later
phases wire each one to its real implementation. The dispatch logic is
exercised by :mod:`tests.test_cli`.

CCR-003 wires up:

* ``configure_logging`` is invoked from :func:`main` before dispatch.
* ``init-db`` programmatically runs ``alembic upgrade head`` and ensures
  ``DATA_DIR`` and ``DATA_DIR/logs`` exist.

CCR-004 wires up the ``pair`` family. The CLI is a one-shot operator tool:
the operator is assumed to have shell access on the project host and is
treated as the owner for the purposes of ``pair invite`` (the bot/console
layers will enforce a Telegram-side owner check). The output strings here
are load-bearing — the acceptance tests pin them verbatim.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ccr.logging_setup import configure_logging

if TYPE_CHECKING:
    from argparse import _SubParsersAction

    from sqlalchemy.ext.asyncio import AsyncSession

    from ccr.db.models import PairedUser, PairingCode


Handler = Callable[[argparse.Namespace], None]


def _stub(name: str) -> Handler:
    """Return a handler that raises :class:`NotImplementedError` when called."""

    def handler(_args: argparse.Namespace) -> None:
        message = f"{name} is implemented in a later phase"
        raise NotImplementedError(message)

    handler.__name__ = f"_cmd_{name.replace(' ', '_').replace('-', '_')}"
    return handler


def _cmd_init_db(_args: argparse.Namespace) -> None:
    """Ensure data dirs exist and run ``alembic upgrade head`` programmatically."""
    # Imported lazily so `python -m ccr --help` (and the rest of the CLI)
    # remains responsive even if Alembic / Settings would fail to import or
    # validate, e.g. when running on a freshly cloned repo without a `.env`.
    from alembic import command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    from ccr.config import Settings  # noqa: PLC0415

    settings = Settings()  # type: ignore[call-arg]

    data_dir: Path = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)

    repo_root = Path.cwd()
    alembic_ini = repo_root / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    db_path = (data_dir / "ccr.db").resolve()
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    command.upgrade(cfg, "head")


def _format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render aligned plain-text columns. No external deps."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    separator_line = "  ".join("-" * widths[i] for i in range(len(headers))).rstrip()
    body_lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    ]
    return "\n".join([header_line, separator_line, *body_lines])


async def _with_session(
    callback: Callable[[AsyncSession], Awaitable[None]],
) -> None:
    """Run ``callback`` against a fresh AsyncSession bound to the configured DB."""
    from ccr.config import Settings  # noqa: PLC0415
    from ccr.db.engine import (  # noqa: PLC0415
        AsyncSessionMaker,
        create_engine_from_settings,
    )

    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine_from_settings(settings)
    factory = AsyncSessionMaker(engine)
    try:
        async with factory() as session:
            await callback(session)
    finally:
        await engine.dispose()


def _format_paired_row(user: PairedUser) -> list[str]:
    flags: list[str] = []
    if user.is_owner:
        flags.append("owner")
    if user.revoked_at is not None:
        flags.append("revoked")
    if not flags:
        flags.append("paired")
    return [
        str(user.tg_user_id),
        user.tg_username or "-",
        user.label or "-",
        ",".join(flags),
        user.approved_at.isoformat(timespec="seconds"),
    ]


def _format_pending_row(pc: PairingCode) -> list[str]:
    return [
        pc.code,
        str(pc.tg_user_id),
        pc.tg_username or "-",
        pc.created_at.isoformat(timespec="seconds"),
        pc.expires_at.isoformat(timespec="seconds"),
    ]


async def _async_pair_list() -> None:
    from ccr.auth.pairing import list_paired  # noqa: PLC0415

    async def _run(session: AsyncSession) -> None:
        rows = await list_paired(session)
        if not rows:
            sys.stdout.write("(empty)\n")
            return
        headers = ["tg_user_id", "username", "label", "status", "approved_at"]
        formatted = [_format_paired_row(u) for u in rows]
        sys.stdout.write(_format_table(headers, formatted) + "\n")

    await _with_session(_run)


async def _async_pair_pending() -> None:
    from ccr.auth.pairing import list_pending  # noqa: PLC0415

    async def _run(session: AsyncSession) -> None:
        rows = await list_pending(session)
        if not rows:
            sys.stdout.write("(empty)\n")
            return
        headers = ["code", "tg_user_id", "username", "created_at", "expires_at"]
        formatted = [_format_pending_row(pc) for pc in rows]
        sys.stdout.write(_format_table(headers, formatted) + "\n")

    await _with_session(_run)


async def _async_pair_approve(code: str) -> int:
    from ccr.auth.pairing import PairingError, approve  # noqa: PLC0415

    exit_code = 0

    async def _run(session: AsyncSession) -> None:
        nonlocal exit_code
        try:
            user = await approve(session, code)
        except PairingError as exc:
            sys.stderr.write(f"{exc}\n")
            exit_code = 1
            return
        role = "owner" if user.is_owner else "paired"
        sys.stdout.write(f"Approved Telegram user {user.tg_user_id} ({role})\n")

    await _with_session(_run)
    return exit_code


async def _async_pair_revoke(tg_user_id: int) -> int:
    from ccr.auth.pairing import (  # noqa: PLC0415
        CannotRevokeOwnerError,
        PairingError,
        revoke,
    )

    exit_code = 0

    async def _run(session: AsyncSession) -> None:
        nonlocal exit_code
        try:
            await revoke(session, tg_user_id)
        except CannotRevokeOwnerError:
            sys.stderr.write("Cannot revoke owner.\n")
            exit_code = 1
            return
        except PairingError as exc:
            sys.stderr.write(f"{exc}\n")
            exit_code = 1
            return
        sys.stdout.write(f"Revoked Telegram user {tg_user_id}\n")

    await _with_session(_run)
    return exit_code


async def _async_pair_invite(tg_user_id: int, label: str | None) -> int:
    from ccr.auth.pairing import PairingError, invite  # noqa: PLC0415

    exit_code = 0

    async def _run(session: AsyncSession) -> None:
        nonlocal exit_code
        try:
            user = await invite(session, tg_user_id, label)
        except PairingError as exc:
            sys.stderr.write(f"{exc}\n")
            exit_code = 1
            return
        role = "owner" if user.is_owner else "paired"
        sys.stdout.write(f"Invited Telegram user {user.tg_user_id} ({role})\n")

    await _with_session(_run)
    return exit_code


def _cmd_console(args: argparse.Namespace) -> None:
    from ccr.config import Settings  # noqa: PLC0415
    from ccr.console.app import run  # noqa: PLC0415

    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(run(settings, once=args.once))


def _cmd_serve(_args: argparse.Namespace) -> None:
    from ccr import server  # noqa: PLC0415
    from ccr.config import Settings  # noqa: PLC0415

    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(server.serve(settings))


def _cmd_pair_list(_args: argparse.Namespace) -> None:
    asyncio.run(_async_pair_list())


def _cmd_pair_pending(_args: argparse.Namespace) -> None:
    asyncio.run(_async_pair_pending())


def _cmd_pair_approve(args: argparse.Namespace) -> None:
    code: str = args.code
    rc = asyncio.run(_async_pair_approve(code))
    if rc != 0:
        raise SystemExit(rc)


def _cmd_pair_revoke(args: argparse.Namespace) -> None:
    tg_user_id: int = args.tg_user_id
    rc = asyncio.run(_async_pair_revoke(tg_user_id))
    if rc != 0:
        raise SystemExit(rc)


def _cmd_pair_invite(args: argparse.Namespace) -> None:
    label: str | None = args.label
    rc = asyncio.run(_async_pair_invite(args.tg_user_id, label))
    if rc != 0:
        raise SystemExit(rc)


def _add_pair_subcommands(pair_subparsers: _SubParsersAction[argparse.ArgumentParser]) -> None:
    list_parser = pair_subparsers.add_parser("list", help="List paired Telegram users.")
    list_parser.set_defaults(func=_cmd_pair_list)

    pending_parser = pair_subparsers.add_parser(
        "pending",
        help="List outstanding pairing codes.",
    )
    pending_parser.set_defaults(func=_cmd_pair_pending)

    approve_parser = pair_subparsers.add_parser(
        "approve",
        help="Approve a pairing code.",
    )
    approve_parser.add_argument("code", help="Pairing code to approve.")
    approve_parser.set_defaults(func=_cmd_pair_approve)

    revoke_parser = pair_subparsers.add_parser(
        "revoke",
        help="Revoke a paired Telegram user.",
    )
    revoke_parser.add_argument(
        "tg_user_id",
        type=int,
        help="Telegram user id to revoke.",
    )
    revoke_parser.set_defaults(func=_cmd_pair_revoke)

    invite_parser = pair_subparsers.add_parser(
        "invite",
        help="Pre-approve a Telegram user (owner-only).",
    )
    invite_parser.add_argument(
        "tg_user_id",
        type=int,
        help="Telegram user id to invite.",
    )
    invite_parser.add_argument(
        "--label",
        default=None,
        help="Optional label for the invited user.",
    )
    invite_parser.set_defaults(func=_cmd_pair_invite)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser.

    Public so tests can introspect the resulting tree without invoking
    :func:`main`.
    """
    parser = argparse.ArgumentParser(
        prog="ccr",
        description="Claude Code Remote — Telegram bot wrapping a local Claude Code CLI session.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{serve,console,pair,doctor,init-db}",
        required=True,
    )

    serve_parser = subparsers.add_parser("serve", help="Run the bot and web server.")
    serve_parser.set_defaults(func=_cmd_serve)

    console_parser = subparsers.add_parser("console", help="Open the owner REPL.")
    console_parser.add_argument(
        "--once",
        metavar="CMD",
        default=None,
        help="Run one command and exit.",
    )
    console_parser.set_defaults(func=_cmd_console)

    pair_parser = subparsers.add_parser("pair", help="Manage Telegram pairing.")
    pair_subparsers = pair_parser.add_subparsers(
        dest="pair_command",
        metavar="{list,pending,approve,revoke,invite}",
        required=True,
    )
    _add_pair_subcommands(pair_subparsers)

    doctor_parser = subparsers.add_parser("doctor", help="Run preflight diagnostics.")
    doctor_parser.set_defaults(func=_stub("doctor"))

    init_db_parser = subparsers.add_parser(
        "init-db",
        help="Create the SQLite database and run migrations.",
    )
    init_db_parser.set_defaults(func=_cmd_init_db)

    return parser


def _initial_log_level() -> str:
    """Read ``LOG_LEVEL`` from env without forcing full Settings validation.

    `Settings()` requires `TELEGRAM_BOT_TOKEN`, `PUBLIC_URL`, and
    `JWT_SECRET`; we cannot afford to crash `--help` on a fresh checkout.
    """
    import os  # noqa: PLC0415

    return os.environ.get("LOG_LEVEL", "INFO")


def main(argv: Sequence[str] | None = None) -> None:
    """Parse command-line arguments and dispatch to the chosen handler."""
    configure_logging(_initial_log_level())
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler = args.func
    handler(args)


__all__ = ["build_parser", "main"]
