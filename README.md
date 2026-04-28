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

## Try it now (current state through CCR-006)

The bot can talk over Telegram, gate non-paired users, and bootstrap the first owner via the console. Session execution, the web viewer, the proxy, and `/view` / `/last` / `/preview` URLs all land in later tickets.

### Prerequisites

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).
- A Telegram bot token from [`@BotFather`](https://t.me/BotFather) (`/newbot`).
- (One-time) `uv sync` and `uv run python -m ccr init-db`.

### 1. Configure `.env`

```bash
cp .env.example .env  # if you don't have one yet
# edit .env: paste your bot token into TELEGRAM_BOT_TOKEN
# JWT_SECRET can be generated with: openssl rand -hex 32
```

`PUBLIC_URL` is unused until CCR-012 (the web server); keep the placeholder.

### 2. Start the bot (terminal 1)

```bash
uv run python -m ccr serve
```

You should see `Bot started, awaiting updates` in the logs. Leave it running.

### 3. Bootstrap the first owner (Telegram + terminal 2)

In Telegram, open your bot and send `/start`. With no owner paired yet, the bot replies:

```
Bootstrap pairing.
Telegram ID: <your-id>
Code: <8-char-code>

On the project host:
python -m ccr pair approve <code>
```

Run that command in a second terminal:

```bash
uv run python -m ccr pair approve <code>
```

It prints `Approved Telegram user <id> (owner)`. Send `/start` again — the bot now replies:

```
Paired. Send a prompt to start, or /new for a fresh session.
```

`/new` and prompt forwarding don't do anything yet (they land in CCR-008).

### 4. Verify the allowlist

From a different (unpaired) Telegram account, send any plain text to the bot. Expected reply:

```
Not paired. Send /start to request access.
```

The owner gets a DM only after the unpaired user sends `/start`:

```
Pairing request from @<username> (id <id>).
Approve with: python -m ccr pair approve <code>
```

### Owner ops via the console

`uv run python -m ccr console` opens a REPL with `pair list`, `pair pending`, `pair approve <code>`, `pair revoke <id>`, `pair invite <id> [--label X]`, `status`, `help`. The same operations are available as one-shot subcommands:

```bash
uv run python -m ccr pair list
uv run python -m ccr pair pending
uv run python -m ccr pair approve <code>
uv run python -m ccr pair revoke <tg-user-id>
uv run python -m ccr pair invite <tg-user-id> --label friend
```

Owner cannot be revoked. The first approved pairing auto-promotes to owner.

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
