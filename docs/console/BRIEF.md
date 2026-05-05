# Brief: console

## Purpose
Interactive owner REPL (`python -m ccr console`) built on `prompt_toolkit`. Lets the project owner manage paired users directly from the host terminal without the Telegram bot running. Supports all pairing operations, a `status` health-check, and a `--once CMD` flag for scripted non-interactive use. Reuses the same pure `ccr.auth.pairing` functions as the one-shot CLI, so behaviour and output strings match across both surfaces.

## Key invariants
- Console runs as a **separate short-lived process** that talks to the same SQLite DB as the server. It must not be folded into the server process (modular-monolith rule from `CLAUDE.md`).
- Every pairing transition flows through `ccr.auth.pairing.*` — the REPL never bypasses those functions or talks to the DB directly. Output strings match the CLI subcommands exactly.
- `--once CMD` exits after the single dispatch (used in tests + scripts); interactive mode loops until `exit`, EOF, or `Ctrl+C`.
- `status` makes one `httpx` call to `/healthz`; failure is reported, never raised. The console keeps running.

## Public surface
- `python -m ccr console` (and the `ccr console` script) — interactive REPL.
- `python -m ccr console --once "<command>"` — one-shot dispatch, prints output, exits.
- REPL commands: `pair list`, `pair pending`, `pair approve <tg_user_id>`, `pair revoke <tg_user_id>`, `pair invite <tg_user_id>`, `status`, `help`, `exit`.
- `src/ccr/console/__init__.py` — package marker making `ccr.console.app` importable.
- `src/ccr/console/app.py::run(...)` — REPL entry point; `dispatch()`, `build_completer()`, `print_banner()`, `COMMANDS` registry, `ExitConsoleError` sentinel, per-command handlers (`_cmd_pair_list`, `_cmd_pair_pending`, `_cmd_pair_approve`, `_cmd_pair_revoke`, `_cmd_pair_invite`, `_cmd_status`, `_cmd_help`, `_cmd_exit`), helpers `_match_command` / `_with_session` / `_emit`.
- `src/ccr/cli.py::_cmd_console` + `--once CMD` argument on the `console` subparser.

## Subtleties / gotchas
- Output strings are pinned by acceptance tests — changing them breaks `test_console.py`. Match the CLI subcommand wording exactly.
- The console talks to the DB even while the server is running. SQLite WAL handles the concurrency, but long-running migrations (Alembic) should not be invoked from inside the REPL.
- `status` pings the server but never blocks the REPL on connection failure — print and continue.
- `prompt_toolkit` history is in-memory only; restart loses it. This is intentional (no on-disk history file to leak).

## Cross-feature relations
- depends on: core (`ccr.config.Settings`, `ccr.db.engine`, `ccr.db.models`), auth (`ccr.auth.pairing` functions).
- used by: nothing (standalone owner tool).

## Status
- State: COMPLETE
- Tickets: CCR-005
- Last updated: CCR-005 (2026-04-28)
