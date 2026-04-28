# Brief: console

## Overview
The console feature delivers an interactive owner REPL (`python -m ccr console`) built on `prompt_toolkit`, giving the project owner a command-line interface for managing paired users without the Telegram bot running. It supports all pairing operations (`pair list`, `pair pending`, `pair approve`, `pair revoke`, `pair invite`), a `status` command that reports DB stats and pings the server's `/healthz` endpoint, and a `--once CMD` flag for scripted non-interactive use. The REPL shares the same pure `ccr.auth.pairing` functions as the one-shot CLI subcommands, ensuring consistent behaviour and output formatting across both interfaces.

## Files
- `src/ccr/console/__init__.py` — package marker making `ccr.console.app` importable.
- `src/ccr/console/app.py` — interactive owner REPL: `run()` entry point, `dispatch()`, `build_completer()`, `print_banner()`, `COMMANDS` registry, `ExitConsoleError` sentinel, per-command handlers (`_cmd_pair_list`, `_cmd_pair_pending`, `_cmd_pair_approve`, `_cmd_pair_revoke`, `_cmd_pair_invite`, `_cmd_status`, `_cmd_help`, `_cmd_exit`), and helpers `_match_command` / `_with_session` / `_emit`.
- `src/ccr/cli.py` — modified: added `_cmd_console(args)` handler and `--once CMD` argument on the `console` subparser.
- `tests/test_console.py` — 29 tests covering REPL dispatch via `run(..., once=...)` and end-to-end CLI integration.

Status: COMPLETE
Tickets: CCR-005
