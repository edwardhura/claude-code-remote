# Context: console

## Files
- `src/ccr/console/__init__.py` — package marker making `ccr.console.app` importable.
- `src/ccr/console/app.py` — interactive owner REPL: `run()` entry point, `dispatch()`, `build_completer()`, `print_banner()`, `COMMANDS` registry, `ExitConsoleError` sentinel, per-command handlers (`_cmd_pair_list`, `_cmd_pair_pending`, `_cmd_pair_approve`, `_cmd_pair_revoke`, `_cmd_pair_invite`, `_cmd_status`, `_cmd_help`, `_cmd_exit`), and helpers `_match_command` / `_with_session` / `_emit`.
- `src/ccr/cli.py` — modified: added `_cmd_console(args)` handler and `--once CMD` argument on the `console` subparser; replaced the previous `_stub("console")` default.
- `tests/test_console.py` — 29 tests covering REPL dispatch via `run(..., once=...)` against a temp-file SQLite DB, plus three end-to-end CLI tests driving `main(["console", "--once", "<cmd>"])`.

## Relations
- depends on: `core` (Settings from `ccr.config`, async engine from `ccr.db.engine`, ORM models from `ccr.db.models`)
- depends on: `auth` (pairing functions from `ccr.auth.pairing`: `list_paired`, `list_pending`, `approve`, `revoke`, `invite`, `create_code`)
- used by: nothing yet (standalone owner tool)

## Change history
- [CCR-005]: implemented interactive console REPL with `prompt_toolkit`, `--once` non-interactive mode, `status` command with `httpx` healthz ping, and full test suite (29 tests, 90%+ coverage).
