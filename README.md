# Claude Code Remote

Self-hosted Telegram bot that turns your phone into a remote control for a local Claude Code CLI session. One bot per project, owner-controlled pairing, multi-user broadcast, structured event streaming, inline permission prompts, a read-only multi-tab web viewer, and a localhost-app preview proxy through a user-supplied tunnel.

## Status

Pre-release. The architecture and phased build plan live in [`claude-code-remote-plan.md`](claude-code-remote-plan.md). Tickets are tracked in [`TICKETS.md`](TICKETS.md).

## Run locally in 60 seconds

> The polished walkthrough lands in CCR-017 (Phase 14). Until then, the canonical bootstrap sequence is:
>
> ```bash
> git clone --recurse-submodules <repo-url> claude-code-remote
> cd claude-code-remote
> ./install.sh
> # edit .env: set TELEGRAM_BOT_TOKEN and PUBLIC_URL
> python -m ccr serve
> # in Telegram, send /start to your bot, then approve via:
> python -m ccr pair approve <code>
> ```

This section will be rewritten end-to-end once the bootstrap and viewer phases ship.

## Layout

```
src/ccr/        Python package (CLI, bot, web, claude wrapper, console, db).
tests/          Pytest suite mirroring src/ccr.
data/           Runtime SQLite + JSONL session logs (gitignored).
templates/      `dev-stack-agents` git submodule.
```

## Development

```bash
uv sync                           # install runtime + dev deps
uv run pytest                     # full suite
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pre-commit run --all-files
```

## License

TBD — to be confirmed by the project owner before the first tagged release.
