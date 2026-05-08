# Claude Code Remote

Self-hosted Telegram bot that turns your phone into a remote control for a local Claude Code CLI session. One bot per project, owner-controlled pairing, multi-user broadcast, structured event streaming, inline permission prompts, a read-only multi-tab web viewer, and a localhost-app preview proxy through a user-supplied tunnel.

## Status

Pre-release. The architecture and phased build plan live in [`claude-code-remote-plan.md`](claude-code-remote-plan.md). The active ticket queue is in [`BACKLOG.md`](BACKLOG.md); finished and closed tickets archive to [`DONE.md`](DONE.md).

The chat-bot loop is functional today: pairing, session lifecycle (`/new`, `/stop`, `/clear`, `/continue`), structured event broadcast, MCP-driven inline permission prompts, AskUserQuestion replies, slash-command passthrough, and `/cost` / `/usage` summaries. The web viewer, the localhost preview proxy, and the `/view` / `/last` / `/preview` URL-minting commands are scoped in CCR-012 – CCR-015 and not yet implemented.

## Bot commands

Only paired users can run any of these except `/start`. Plain text (no leading `/`) is forwarded to the active session as a user turn, or starts a fresh session if none is running.

### Session control

| Command | Effect |
| --- | --- |
| `/new` | Start a fresh Claude session (no initial prompt). |
| `/stop` | Stop the active session. Idempotent on idle. |
| `/continue [<id8>]` | Resume the most recent finished session, or the one whose `claude_session_id[:8]` matches the prefix shown in `/sessions`. |
| `/clear` | Stop the active session, post a "new session" divider, then start a fresh one. |
| `/pid` | Show the active session's id, OS pid, and uptime. |
| `/sessions` | List the most recent 20 sessions (newest first). Sessions still initialising (`[idle]` — no `claude_session_id` yet) are filtered out. |
| `/rename <id8> <name>` | Rename the session whose `claude_session_id[:8]` matches the prefix. When the prefix matches a resume chain, every row in the chain is renamed in one commit. |
| `/rename current <name>` | Rename the active session's chain without typing the prefix. Replies with a stable error if no session is active or the active session is still in `[idle]`. |
| `/answer <id8> <text>` | Reply to a free-text `AskUserQuestion` by 8-hex prefix (the question's `tool_use_id`, not a session id). Inline-button questions are answered by tapping. |

### Pairing

| Command | Effect |
| --- | --- |
| `/start` | First sender bootstraps as owner; later senders request access (owner gets a DM with the approval command). |
| `/who` | Pairing status — owners see the full table; friends see the count plus the owner handle. |

### Telemetry & introspection

| Command | Effect |
| --- | --- |
| `/cost` | Per-session usage summary computed locally from the JSONL log (turns, tokens, tool calls, elapsed, cost). |
| `/usage` | Most recent rate-limit snapshot (window, status, reset time, overage state). |
| `/agents` | Subagents the active session has spawned, plus the agent library under `.claude/agents/`. |
| `/skills` | Skills reported by the active session's `system/init` event. |

### Passthrough to Claude

`/model` and `/compact` are forwarded to Claude as a user turn — Claude Code interprets the leading slash exactly as if you typed it in its TTY.

`/mcp` and `/init` are blocked: those are interactive-only commands and the bot will tell you to run them in your local terminal.

Anything else not in this list returns the canonical usage hint. `/view`, `/last`, and `/preview` appear in that hint but are not yet implemented (CCR-014).

## Setup

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
