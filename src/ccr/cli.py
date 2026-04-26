"""Argparse skeleton for `python -m ccr` and the `ccr` console script.

Most subcommand handlers still raise :class:`NotImplementedError`; later
phases wire each one to its real implementation. The dispatch logic is
exercised by :mod:`tests.test_cli`.

CCR-003 wires up:

* ``configure_logging`` is invoked from :func:`main` before dispatch.
* ``init-db`` programmatically runs ``alembic upgrade head`` and ensures
  ``DATA_DIR`` and ``DATA_DIR/logs`` exist.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ccr.logging_setup import configure_logging

if TYPE_CHECKING:
    from argparse import _SubParsersAction


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


def _add_pair_subcommands(pair_subparsers: _SubParsersAction[argparse.ArgumentParser]) -> None:
    list_parser = pair_subparsers.add_parser("list", help="List paired Telegram users.")
    list_parser.set_defaults(func=_stub("pair list"))

    pending_parser = pair_subparsers.add_parser(
        "pending",
        help="List outstanding pairing codes.",
    )
    pending_parser.set_defaults(func=_stub("pair pending"))

    approve_parser = pair_subparsers.add_parser(
        "approve",
        help="Approve a pairing code.",
    )
    approve_parser.add_argument("code", help="Pairing code to approve.")
    approve_parser.set_defaults(func=_stub("pair approve"))

    revoke_parser = pair_subparsers.add_parser(
        "revoke",
        help="Revoke a paired Telegram user.",
    )
    revoke_parser.add_argument(
        "tg_user_id",
        type=int,
        help="Telegram user id to revoke.",
    )
    revoke_parser.set_defaults(func=_stub("pair revoke"))

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
    invite_parser.set_defaults(func=_stub("pair invite"))


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
    serve_parser.set_defaults(func=_stub("serve"))

    console_parser = subparsers.add_parser("console", help="Open the owner REPL.")
    console_parser.set_defaults(func=_stub("console"))

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
