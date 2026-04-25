# Context: core

## Files
- pyproject.toml — project metadata, runtime + dev deps, ruff/mypy/pytest config, `[project.scripts] ccr = "ccr.cli:main"`.
- uv.lock — generated lockfile produced by `uv sync`.
- .gitignore — ignores Python build artefacts, `.venv/`, `data/`, `.env`, IDE files, ruff/mypy caches.
- src/ccr/__init__.py — exposes `__version__ = "0.1.0"`.
- src/ccr/__main__.py — `python -m ccr` entrypoint that calls `ccr.cli:main`.
- src/ccr/cli.py — argparse skeleton with `serve`, `console`, `pair {list,pending,approve,revoke,invite}`, `doctor`, `init-db` subcommands; every handler currently raises `NotImplementedError` via the `_stub(name)` helper. Exposes `build_parser()` for tests.
- tests/test_cli.py — covers `--help` exit 0, unknown subcommand exit 2, missing subcommand exit 2, dispatch to `NotImplementedError`, and end-to-end `python -m ccr` subprocess invocations.

## Relations
- depends on: (none — this is the root scaffold)
- used by: every other feature (auth, console, chat-bot, claude-runtime, web-viewer, bootstrap) inherits the package layout, dep set, and CLI dispatch defined here.

## Change history
- [CCR-001]: initial Python package scaffold — pyproject with full Phase 1 dep set, `src/ccr/{__init__,__main__,cli}.py`, argparse subcommand tree (NotImplementedError stubs), test suite for CLI dispatch behaviour, `.gitignore`, `uv.lock`.
