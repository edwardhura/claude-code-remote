# Context: console

## Files
- `src/ccr/console/__init__.py` — package marker making `ccr.console.app` importable.
- `src/ccr/console/app.py` — interactive owner REPL: `run()` entry point, `dispatch()`, `build_completer()`, `print_banner()`, `COMMANDS` registry, `ExitConsoleError` sentinel, per-command handlers (`_cmd_pair_list`, `_cmd_pair_pending`, `_cmd_pair_approve`, `_cmd_pair_revoke`, `_cmd_pair_invite`, `_cmd_session_save`, `_cmd_status`, `_cmd_help`, `_cmd_exit`), and helpers `_match_command` / `_with_session` / `_emit`; `_STATIC_COMMAND_WORDS` includes `session` and `save` (CCR-036); `_match_command` sweeps 2-token `session save` prefix (CCR-036).
- `src/ccr/cli.py` — modified: added `_cmd_console(args)` handler and `--once CMD` argument on the `console` subparser; replaced the previous `_stub("console")` default; `session` subcommand group with `save` member added (CCR-036); top-level subparser metavar updated to `{serve,console,pair,session,doctor,init-db}` (CCR-036).
- `tests/test_console.py` — 29 tests covering REPL dispatch via `run(..., once=...)` against a temp-file SQLite DB, plus three end-to-end CLI tests driving `main(["console", "--once", "<cmd>"])`; extended (CCR-036) with `session save` REPL coverage (usage hint, success, duplicate).

## Relations
- depends on: `core` (Settings from `ccr.config`, async engine from `ccr.db.engine`, ORM models from `ccr.db.models`)
- depends on: `auth` (pairing functions from `ccr.auth.pairing`: `list_paired`, `list_pending`, `approve`, `revoke`, `invite`, `create_code`)
- depends on: `claude-runtime` (`ccr.claude.import_session.import_claude_session` called by `session save`) (added CCR-036)
- used by: nothing yet (standalone owner tool)

## Change history
- [CCR-005]: implemented interactive console REPL with `prompt_toolkit`, `--once` non-interactive mode, `status` command with `httpx` healthz ping, and full test suite (29 tests, 90%+ coverage).
- [CCR-036]: added `session save <claude_session_id>` to both one-shot CLI (`src/ccr/cli.py` `session` subcommand group) and interactive REPL (`_cmd_session_save` handler, `_STATIC_COMMAND_WORDS` extension, `_match_command` 2-token sweep, `_cmd_help` entry); both entrypoints call `ccr.claude.import_session.import_claude_session`; `tests/test_console.py` extended with `session save` REPL coverage.
