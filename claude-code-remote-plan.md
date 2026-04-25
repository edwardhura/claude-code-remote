# Architecture & Implementation Plan: Claude Code Remote

## 1. Project Summary

A self-hosted Telegram bot, one instance per project, that turns a phone into a remote control for a local Claude Code CLI session. Pairing is owner-controlled via a dedicated console app on the project host: the owner approves themselves first and may then approve trusted friends. The bot forwards prompts to a managed Claude Code subprocess, streams structured events back to chat, handles permission prompts via inline buttons, and exposes a read-only multi-tab web viewer plus a localhost-app preview proxy through a user-supplied tunnel (Tailscale Funnel by default). Everything runs on the developer's own machine.

## 2. Application Type & Architecture Style

- **Archetype:** Telegram bot + paired web service + local console app. Single project, multi-paired-user (owner-controlled), multi-service in-process.
- **Style:** Modular monolith — one Python process running aiogram + FastAPI + the Claude Code subprocess manager on a shared asyncio event loop, with an internal pub/sub event bus connecting them. The console is a separate short-lived process that talks to the same SQLite DB.
- **Reason:** All concerns share one host machine, one DB, and one event stream. Splitting bot from web server would force IPC for no gain. The console is intentionally separate so it can be invoked without disturbing the running server.

## 3. Tech Stack

| Layer | Choice | Reason (only if non-obvious) |
|---|---|---|
| Language | Python 3.12+ | aiogram + Claude SDK ergonomics; native async subprocess |
| Bot framework | aiogram 3.x | Async-native, FSM, robust callback_query handling |
| Web framework | FastAPI 0.115+ | Same event loop as bot; SSE + reverse proxy easy |
| ASGI server | uvicorn | Standard FastAPI runner |
| HTTP client | httpx | Async; powers the localhost-app reverse proxy |
| Validation | Pydantic v2 + pydantic-settings | Boundary validation for events and config |
| ORM | SQLAlchemy 2.x async | Future-proof; supports Alembic |
| Migrations | Alembic | Reversible schema changes for OSS upgraders |
| Database | SQLite (`data/ccr.db`) | Local, single-host, no server needed |
| Event store | JSONL files (`data/logs/<id>.jsonl`) | Streaming-native, cheap to tail for SSE |
| Console UI | prompt_toolkit 3.x | Interactive REPL with completion + history |
| Auth (bot) | Owner-controlled pairing + allowlist | Friends invited only by owner |
| Auth (web) | Short-lived JWT in URL → HttpOnly cookie | Resists link-sharing, screenshot leaks |
| JWT lib | PyJWT 2.x | Minimal, well-maintained |
| Background work | None (asyncio tasks suffice) | No queue needed; everything is event-loop scoped |
| Terminal renderer | xterm.js 5.5.x via jsDelivr CDN + SRI | Per user request; SRI guards against CDN tampering |
| Logging | structlog | Structured, easy to grep |
| Tests | pytest + pytest-asyncio + httpx test client | Standard async stack |
| Lint/format | ruff (lint + format) | Single tool replaces black/isort/flake8 |
| Type check | mypy --strict on `src/ccr` | Catches event-schema drift early |
| Package manager | uv | Fast, lockfile, single-file project |
| Tunnel (external) | Tailscale Funnel (documented default) | User-supplied; server is tunnel-agnostic |
| Deployment target | Local PC + tunnel | No cloud; local-first is the product |

## 4. Repository Layout

```
claude-code-remote/
├── pyproject.toml
├── uv.lock
├── README.md
├── .env.example
├── .gitignore
├── .gitmodules
├── .pre-commit-config.yaml
├── .github/
│   └── workflows/
│       └── ci.yml
├── install.sh
├── alembic.ini
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 0001_initial.py
│       └── 0002_paired_users_chat_id.py
├── src/
│   └── ccr/
│       ├── __init__.py
│       ├── __main__.py              # `python -m ccr` → cli.main()
│       ├── cli.py                   # argparse: serve | console | pair {list,pending,approve,revoke,invite} | doctor | init-db
│       ├── server.py                # composite runner: bot task + uvicorn task
│       ├── config.py                # pydantic-settings Settings
│       ├── logging_setup.py
│       ├── db/
│       │   ├── __init__.py
│       │   ├── engine.py            # async engine + session factory
│       │   └── models.py            # SQLAlchemy models
│       ├── auth/
│       │   ├── __init__.py
│       │   ├── pairing.py           # generate_code, approve, revoke, list, owner-check
│       │   ├── allowlist.py         # is_paired(tg_user_id), is_owner(tg_user_id)
│       │   └── tokens.py            # mint_token, verify_token (JWT)
│       ├── console/
│       │   ├── __init__.py
│       │   └── app.py               # prompt_toolkit REPL: pair commands, status
│       ├── claude/
│       │   ├── __init__.py
│       │   ├── events.py            # Pydantic models for stream events
│       │   ├── process.py           # ClaudeProcess: spawn, write, read, kill
│       │   ├── manager.py           # SessionManager: lifecycle, status
│       │   └── log.py               # JsonlSessionLog: append, tail, prune
│       ├── events/
│       │   └── bus.py               # EventBus: publish/subscribe (asyncio)
│       ├── bot/
│       │   ├── __init__.py
│       │   ├── app.py               # build_dispatcher, start_polling
│       │   ├── middlewares.py       # AllowlistMiddleware
│       │   ├── formatting.py        # event_to_messages(event) chunked
│       │   ├── keyboards.py         # permission inline keyboards
│       │   ├── notify.py            # notify_owner, broadcast_paired
│       │   └── handlers/
│       │       ├── __init__.py
│       │       ├── pairing.py       # /start when unpaired
│       │       ├── session.py       # /new /stop /clear + plain text
│       │       ├── permission.py    # callback_query for permission btns
│       │       ├── view.py          # /view /last /preview
│       │       └── passthrough.py   # /cost /model /compact whitelist
│       └── web/
│           ├── __init__.py
│           ├── app.py               # FastAPI factory
│           ├── auth.py              # /auth handoff + cookie middleware
│           ├── sessions.py          # /api/sessions, /api/sessions/{id}/events (SSE)
│           ├── proxy.py             # /app/{port}/{path:path} reverse proxy
│           ├── viewer.py            # /viewer route serving HTML
│           └── static/
│               ├── viewer.html
│               ├── viewer.js
│               └── viewer.css
├── templates/                       # git submodule → dev-stack-agents
│   ├── skills/
│   └── agents/
├── tests/
│   ├── conftest.py
│   ├── fakes/
│   │   └── fake_claude.py
│   ├── fixtures/
│   │   └── events/
│   ├── test_config.py
│   ├── test_db_models.py
│   ├── test_pairing.py
│   ├── test_console.py
│   ├── test_tokens.py
│   ├── test_claude_events.py
│   ├── test_session_manager.py
│   ├── test_bot_pairing.py
│   ├── test_bot_session.py
│   ├── test_bot_permission.py
│   ├── test_passthrough.py
│   ├── test_web_auth.py
│   ├── test_sse_stream.py
│   ├── test_viewer.py
│   ├── test_proxy.py
│   └── test_doctor.py
└── data/                            # gitignored at runtime
    ├── ccr.db
    └── logs/
```

## 5. Data Model

Persistent SQLite tables. Event payloads live as JSONL on disk and are *not* in SQL.

**`paired_users`** — approved Telegram user IDs.
- Fields: `id (uuid, pk)`, `tg_user_id (bigint, unique, not null)`, `tg_username (text, nullable)`, `label (text, nullable)`, `is_owner (boolean, not null, default false)`, `approved_by_tg_user_id (bigint, nullable)`, `approved_at (timestamptz, not null)`, `revoked_at (timestamptz, nullable)`, `last_chat_id (bigint, nullable)`
- Indexes: unique on `tg_user_id`; partial unique on `is_owner` where `is_owner = true` (only one owner)
- Notes: `revoked_at IS NULL` defines an active allowlist entry. No hard deletes. The first approval auto-promotes to owner. `approved_by_tg_user_id` is null for the owner, set to owner's id for friends.

**`pairing_codes`** — short-lived pairing offers awaiting console approval.
- Fields: `id (uuid, pk)`, `code (text, unique, not null, 8 chars)`, `tg_user_id (bigint, not null)`, `tg_username (text, nullable)`, `created_at (timestamptz, not null)`, `expires_at (timestamptz, not null)`, `used_at (timestamptz, nullable)`
- Indexes: unique on `code`; `(tg_user_id, used_at)` for "is there a pending code for this user"
- Notes: TTL 15 min. Approving sets `used_at` and inserts/updates `paired_users`.

**`sessions`** — Claude Code session metadata. Events are NOT here.
- Fields: `id (uuid, pk)`, `started_at (timestamptz, not null)`, `ended_at (timestamptz, nullable)`, `status (text enum: 'running'|'completed'|'stopped'|'crashed', not null)`, `started_by_tg_user_id (bigint, nullable)`, `first_prompt (text, nullable, truncated 500)`, `exit_reason (text, nullable)`, `last_event_at (timestamptz, nullable)`
- Indexes: `(status)`, `(started_at desc)`
- Notes: `last_event_at` powers viewer "stuck" detection (>30 s yellow, >2 min red). One session is `running` at a time globally; `started_by_tg_user_id` records who kicked it off.

ER sketch:

```
paired_users (1) ──< pairing_codes        (a code resolves to either a new or existing user)
paired_users (owner) ──< paired_users     (approved_by_tg_user_id self-reference, owner approves friends)
sessions: standalone, no FK              (started_by tracked as bigint, no FK to allow revoked users in history)
```

## 6. API / Service Contracts

### 6.1 Telegram bot commands

| Command | Auth | Behaviour |
|---|---|---|
| `/start` | none | If sender unpaired → emit pairing code, notify owner via DM. If paired → show session status. |
| `/new` | paired | Stop running session if any, spawn fresh Claude Code subprocess. |
| `/stop` | paired | Kill running subprocess. Idempotent. |
| `/clear` | paired | Equivalent to `/stop` + `/new`. |
| `/view` | paired | Mint signed URL for `/viewer`, return as Telegram message. |
| `/last` | paired | Mint signed URL for `/viewer?session=<last_id>`. |
| `/preview <port>` | paired | Validate port 1–65535; mint signed URL for `/app/<port>/`. |
| `/cost`, `/model`, `/compact` | paired | Forward to active session as a Claude Code slash command. |
| `/agents`, `/mcp`, `/init`, others | paired | Reject: "Run on your machine." |
| `/who` | paired | List paired users (owner sees full list with IDs; friends see count + owner's username). |
| Plain text | paired | If no session: spawn one and use this as the first prompt. Else forward as user turn. |

Inline button callbacks (permission prompts):

```
callback_data format: "perm:<session_id>:<request_id>:<choice>"
choice ∈ {approve, skip, abort, ...}  # populated from the event's options field
```

### 6.2 Console commands (`python -m ccr console`)

REPL loop on the project host. Owner-only after first pairing.

| Command | Behaviour |
|---|---|
| `help` | List commands. |
| `status` | Print server reachable?, paired users count, sessions running, DB path. |
| `pair list` | Table of paired users: id, tg_username, is_owner, approved_at, revoked_at. |
| `pair pending` | Table of unused, unexpired pairing codes. |
| `pair approve <code>` | Approve a pending code. First approval → becomes owner. Subsequent approvals require the *current* shell user to also be owner (verified via OS check, see Phase 3). |
| `pair revoke <tg_user_id>` | Mark revoked. Cannot revoke owner. |
| `pair invite <tg_user_id> [--label X]` | Pre-approve a Telegram ID (owner only). Friend's first `/start` is auto-paired without producing a code. |
| `exit` / `quit` / Ctrl-D | Leave console. |

The same operations are available as one-shot subcommands (`python -m ccr pair list`, etc.) for scripting.

### 6.3 Web HTTP endpoints

```
GET /healthz
  auth: none
  returns: 200 application/json {"ok": true, "session_status": "running"|"idle"}

GET /auth?token=<jwt>&next=<path>
  auth: token query param (signed JWT)
  effect: validates JWT → sets HttpOnly cookie `ccr_session` (30 min TTL) → 302 to `next`
  errors: 401 (invalid/expired token), 400 (bad next path)

GET /viewer
  auth: cookie
  returns: 200 text/html (static viewer.html)

GET /api/sessions
  auth: cookie
  returns: 200 application/json
    [{ id: uuid, status: enum, started_at: iso, ended_at: iso|null,
       last_event_at: iso|null, first_prompt: string|null,
       started_by_tg_user_id: bigint|null }]

GET /api/sessions/{id}/events?from=<seq>
  auth: cookie
  returns: 200 text/event-stream
    SSE messages: { seq: int, type: string, ts: iso, payload: object }
  behaviour: replays JSONL from `from` (default 0), then tails live via EventBus

GET /api/sessions/{id}/log
  auth: cookie
  returns: 200 application/x-ndjson (raw JSONL download)

ANY /app/{port:int}/{path:path}
  auth: cookie (token must have kind=PREVIEW with payload.port == port)
  effect: reverse-proxy to http://127.0.0.1:{port}/{path} preserving method, body, headers
          (strips Cookie/Authorization, rewrites Host, sets X-Forwarded-*)
  errors: 502 (upstream unreachable), 403 (port not in allowlist or token mismatch)
```

### 6.4 Internal event bus (in-process)

```python
# Topics:
"session.event"    payload: {session_id: UUID, event: ClaudeEvent}
"session.status"   payload: {session_id: UUID, status: str, ts: datetime}

# Producers: ClaudeProcess (parsed stdout JSONL), SessionManager (lifecycle)
# Consumers: bot.formatting (Telegram), web.sessions (SSE), claude.log (JSONL writer)
```

### 6.5 Claude Code event schema (consumed)

Pydantic discriminated union on `type`. Known variants the wrapper handles explicitly:

```
{type: "system", subtype: "init", session_id, model, tools, ...}
{type: "user",   message: {role, content}}
{type: "assistant", message: {role, content: [TextBlock|ToolUseBlock|ThinkingBlock]}}
{type: "result", subtype: "success"|"error_during_execution", duration_ms, total_cost_usd, ...}
{type: "permission_request", request_id, tool_use_id, tool_name, input, options: [str]}
```

Anything else → `UnknownEvent(type=str, raw=dict)`. Logged, surfaced to viewer as raw, ignored by Telegram formatter.

## 7. Environment & Configuration

`.env.example`:

```env
# Required
TELEGRAM_BOT_TOKEN=123456:ABCdef...           # from @BotFather
PUBLIC_URL=https://my-machine.tail-scale.ts.net  # base URL exposed by your tunnel
JWT_SECRET=                                    # 32+ random bytes; install.sh generates if blank

# Optional (sensible defaults)
WEB_HOST=127.0.0.1
WEB_PORT=8765
TOKEN_TTL_SECONDS=1800                         # 30 min
COOKIE_TTL_SECONDS=1800                        # 30 min
DATA_DIR=./data
CLAUDE_BIN=claude
CLAUDE_EXTRA_ARGS=
LOG_RETENTION_COUNT=200
LOG_RETENTION_DAYS=30
LOG_LEVEL=INFO
PAIRING_CODE_TTL_SECONDS=900
SUBPROCESS_GRACE_KILL_SECONDS=5
PROXY_PORT_ALLOWLIST=                          # blank = any local port; "3000,5173" to restrict
```

External services required at runtime:
- A Telegram bot token from BotFather.
- A tunnel pointing `PUBLIC_URL` at `WEB_HOST:WEB_PORT` (Tailscale Funnel documented; ngrok / Cloudflare Tunnel work the same).
- Claude Code CLI on `$PATH` (verified at startup).

## 8. Implementation Phases

---

### Phase 1: Repo & Tooling Scaffold

**Goal:** A `python -m ccr --help` entrypoint runs in a freshly cloned repo with lint, format, and CI working end-to-end.

**Dependencies:** None.

**Files to create or modify:**
- `pyproject.toml` — project metadata, deps, scripts, ruff/mypy config.
- `uv.lock` — generated.
- `.gitignore` — Python, IDE, `data/`, `.env`, `.venv/`.
- `.env.example` — full template per Section 7.
- `.pre-commit-config.yaml` — ruff, ruff-format, mypy hooks.
- `.github/workflows/ci.yml` — install uv, run `uv sync`, `ruff check`, `ruff format --check`, `mypy src`, `pytest`.
- `src/ccr/__init__.py` — exposes `__version__`.
- `src/ccr/__main__.py` — `from ccr.cli import main; main()`.
- `src/ccr/cli.py` — argparse skeleton with `serve`, `console`, `pair {list,pending,approve,revoke,invite}`, `doctor`, `init-db` subcommands stubbed (NotImplementedError bodies).
- `tests/test_cli.py` — `--help` exits 0; unknown subcommand exits 2.
- `README.md` — placeholder with project summary and "Run locally in 60 seconds" section reserved.

**Packages to install:**
- `aiogram>=3.13,<4` — bot.
- `fastapi>=0.115,<0.120` — web.
- `uvicorn[standard]>=0.32,<0.40` — ASGI.
- `httpx>=0.27,<0.30` — proxy client.
- `pydantic>=2.9,<3` — validation.
- `pydantic-settings>=2.6,<3` — config.
- `sqlalchemy[asyncio]>=2.0,<3` — ORM.
- `aiosqlite>=0.20,<0.22` — async SQLite driver.
- `alembic>=1.13,<2` — migrations.
- `pyjwt>=2.9,<3` — JWT.
- `prompt_toolkit>=3.0,<4` — console REPL.
- `structlog>=24.4,<26` — logging.
- Dev: `pytest>=8.3`, `pytest-asyncio>=0.24`, `ruff>=0.7`, `mypy>=1.13`, `pre-commit>=4.0`.

**Tasks:**
1. `uv init --package` and add deps above to `pyproject.toml`.
2. Configure ruff (line length 100, target-version py312, all rules + ignore E501 in tests).
3. Configure mypy (strict, exclude `alembic/versions`).
4. Set `pyproject.toml` `[project.scripts] ccr = "ccr.cli:main"`.
5. Stub `cli.py` argparse with all subcommands raising NotImplementedError.
6. Write CI workflow.
7. `git init`, commit, run pre-commit.

**Acceptance criteria:**
- `uv sync` succeeds.
- `python -m ccr --help` exits 0 and lists `serve`, `console`, `pair`, `doctor`, `init-db`.
- `python -m ccr pair list` exits with `NotImplementedError` (proves dispatch works).
- `ruff check src tests` passes.
- `mypy src` passes.
- `pytest` runs `test_cli.py` green.
- CI workflow file is valid YAML.

**Out of scope:** Any business logic, DB, env loading.

---

### Phase 2: Configuration, DB Schema, Migrations

**Goal:** A typed `Settings` object loads `.env`, an async SQLAlchemy engine connects to `data/ccr.db`, Alembic creates the three tables from Section 5.

**Dependencies:** Phase 1 complete.

**Files to create or modify:**
- `src/ccr/config.py` — `Settings(BaseSettings)` with all env vars from Section 7, `model_config=SettingsConfigDict(env_file=".env", extra="ignore")`. Field validators: `JWT_SECRET` ≥ 32 chars, `WEB_PORT` 1–65535, `PROXY_PORT_ALLOWLIST` parsed to `set[int] | None`, `TOKEN_TTL_SECONDS` 60–86400.
- `src/ccr/logging_setup.py` — `configure_logging(level: str)` using structlog with console renderer.
- `src/ccr/db/engine.py` — `create_engine_from_settings(settings)`, `AsyncSessionMaker`, `get_session()` async context manager.
- `src/ccr/db/models.py` — `Base = DeclarativeBase`, `PairedUser`, `PairingCode`, `Session` SQLAlchemy models matching Section 5 (UUIDs as `Uuid(as_uuid=True)`, partial unique index on `is_owner = true`).
- `alembic.ini` — `sqlalchemy.url = sqlite+aiosqlite:///data/ccr.db`, `script_location = alembic`.
- `alembic/env.py` — async-aware env that imports `Base.metadata` from `ccr.db.models`.
- `alembic/versions/0001_initial.py` — creates `paired_users`, `pairing_codes`, `sessions`.
- `src/ccr/cli.py` — implement `init-db` subcommand that runs `alembic upgrade head` programmatically, ensures `DATA_DIR` and `DATA_DIR/logs` exist.
- `tests/test_config.py` — Settings loads from a temp `.env`; rejects short JWT_SECRET; parses port allowlist correctly.
- `tests/test_db_models.py` — engine bound to in-memory SQLite, `Base.metadata.create_all` runs, can insert + query a `PairedUser`; partial unique on `is_owner` prevents two owners.

**Packages to install:** none new.

**Tasks:**
1. Implement `Settings` with full validation.
2. Wire structlog in `logging_setup`; `cli.main` calls it before dispatch.
3. Define the three SQLAlchemy models per Section 5. Use `Mapped[...]` syntax.
4. Configure Alembic for async; `env.py` reads URL from `Settings`.
5. Author `0001_initial.py` (manual, not autogenerated, to keep it readable).
6. Add `init-db` subcommand that creates `DATA_DIR` and runs `alembic upgrade head`.
7. Write tests using `pytest-asyncio` and an in-memory `sqlite+aiosqlite:///:memory:` engine.

**Code sketches:**

```python
# src/ccr/config.py
class Settings(BaseSettings):
    telegram_bot_token: SecretStr
    public_url: HttpUrl
    jwt_secret: SecretStr
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    token_ttl_seconds: int = 1800
    cookie_ttl_seconds: int = 1800
    data_dir: Path = Path("./data")
    claude_bin: str = "claude"
    proxy_port_allowlist: set[int] | None = None
    # ... validators ...
```

**Acceptance criteria:**
- `cp .env.example .env && JWT_SECRET=$(openssl rand -hex 32) ... && python -m ccr init-db` exits 0 and creates `data/ccr.db`.
- `sqlite3 data/ccr.db ".tables"` lists `alembic_version`, `paired_users`, `pairing_codes`, `sessions`.
- `pytest tests/test_config.py tests/test_db_models.py` passes.
- `alembic downgrade base && alembic upgrade head` round-trips without error.
- A test asserts that inserting a second `PairedUser` with `is_owner=True` raises `IntegrityError`.

**Out of scope:** Any auth or business logic; only schema and config.

---

### Phase 3: Pairing Auth — Storage, Owner Model, CLI Subcommands

**Goal:** A first-time user gets a pairing code; the operator runs `python -m ccr pair approve <code>` from the project terminal; the first approval becomes owner; subsequent approvals require an existing owner; allowlist checks return True for active paired users.

**Dependencies:** Phase 2 complete.

**Files to create or modify:**
- `src/ccr/auth/pairing.py`:
  - `async def create_code(db, tg_user_id, tg_username) -> PairingCode` (8-char base32, TTL from settings)
  - `async def list_pending(db) -> list[PairingCode]`
  - `async def approve(db, code: str) -> PairedUser` — first approval auto-promotes to owner; later approvals set `approved_by_tg_user_id` to current owner
  - `async def revoke(db, tg_user_id: int) -> None` — refuses if target is owner
  - `async def invite(db, tg_user_id, label) -> PairedUser` — owner-only, pre-approves
  - `async def list_paired(db) -> list[PairedUser]`
  - `async def get_owner(db) -> PairedUser | None`
- `src/ccr/auth/allowlist.py`:
  - `async def is_paired(db, tg_user_id: int) -> bool`
  - `async def is_owner(db, tg_user_id: int) -> bool`
- `src/ccr/cli.py` — implement one-shot subcommands `pair list`, `pair pending`, `pair approve <code>`, `pair revoke <tg_user_id>`, `pair invite <tg_user_id> [--label X]`. Pretty-print results. Each subcommand calls the same functions the console will call.
- `tests/test_pairing.py` — full lifecycle: create code → first approval becomes owner → second code → second approval is non-owner → revoke non-owner OK → revoke owner refused → expired code rejected → reused code rejected → invite happy path.

**Packages to install:** none new.

**Tasks:**
1. Implement `create_code` using `secrets.token_hex(4).upper()` collision-retried up to 5 times.
2. Implement `approve` as a transaction:
   - Look up unused, unexpired code.
   - If `paired_users` is empty → set `is_owner=True`, `approved_by_tg_user_id=NULL`.
   - Else → set `is_owner=False`, `approved_by_tg_user_id = (SELECT tg_user_id FROM paired_users WHERE is_owner)`.
   - Mark code used, upsert paired_user (insert or clear `revoked_at`).
3. Implement `revoke` to raise if target is owner.
4. Implement `invite` to bypass code creation (owner-only enforced at CLI/console layer; pure function trusts caller).
5. Implement `is_paired` = `exists(paired_users where tg_user_id = ? and revoked_at IS NULL)`.
6. Implement `is_owner` = `exists(paired_users where tg_user_id = ? and is_owner=true and revoked_at IS NULL)`.
7. Wire CLI subcommands.
8. Write tests with manual `now` injection.

**Code sketches:**

```python
# src/ccr/auth/pairing.py
async def approve(db: AsyncSession, code: str) -> PairedUser:
    pc = await db.scalar(select(PairingCode).where(
        PairingCode.code == code,
        PairingCode.used_at.is_(None),
        PairingCode.expires_at > utcnow(),
    ))
    if pc is None:
        raise PairingError("Code invalid or expired")

    pc.used_at = utcnow()
    owner = await get_owner(db)
    is_first = owner is None
    user = await _upsert_paired_user(
        db,
        tg_user_id=pc.tg_user_id,
        tg_username=pc.tg_username,
        is_owner=is_first,
        approved_by=None if is_first else owner.tg_user_id,
    )
    await db.commit()
    return user
```

**Acceptance criteria:**
- `python -m ccr pair list` prints `(empty)` on a fresh DB.
- After manually inserting a `pairing_codes` row, `python -m ccr pair approve <code>` prints `Approved Telegram user 123456789 (owner)`. A subsequent `pair list` shows the user with `is_owner=True`.
- A second pairing code, when approved, prints `Approved Telegram user 987654321 (paired)` with no owner promotion.
- `python -m ccr pair revoke <owner_id>` exits 1 with `Cannot revoke owner.`
- `python -m ccr pair revoke <friend_id>` succeeds and `pair list` no longer shows that user as active.
- `pytest tests/test_pairing.py` passes.

**Out of scope:** Bot integration (Phase 5/6); console REPL (Phase 4).

---

### Phase 4: Console REPL App

**Goal:** `python -m ccr console` opens an interactive shell on the project host. The owner (or, before any owner exists, anyone with shell access on the host) can list/approve/revoke/invite paired users from one place. Non-pairing commands like `status` show server health.

**Dependencies:** Phase 3 complete.

**Files to create or modify:**
- `src/ccr/console/app.py` — prompt_toolkit `PromptSession` with:
  - Command parser (split on whitespace, dispatch to handlers).
  - Auto-completion for `pair approve <pending-code>` and `pair revoke <paired-id>`.
  - History file at `data/console_history`.
  - Handlers calling the same `auth.pairing.*` functions used by the CLI.
  - `status` command: pings `http://{WEB_HOST}:{WEB_PORT}/healthz`, prints DB path, paired count, pending codes count.
- `src/ccr/cli.py` — wire `console` subcommand to `console.app.run()`.
- `tests/test_console.py` — drive the REPL programmatically via prompt_toolkit's `create_pipe_input`. Assert: `pair list` outputs expected table; unknown command prints help; `pair approve <bad>` shows error; happy path approves a pre-seeded code.

**Packages to install:** none new (prompt_toolkit added in Phase 1).

**Tasks:**
1. Build a small command registry (`COMMANDS: dict[str, Callable]`).
2. Implement table rendering with simple aligned columns (no extra dep).
3. Wire auto-completion using `WordCompleter` whose words are refreshed before each prompt by querying the DB.
4. Implement `status` using `httpx.get(public_url + "/healthz", timeout=2)` and DB queries.
5. Provide `--once "<cmd>"` flag for non-interactive scripted use (executes one command and exits).
6. Write tests using `prompt_toolkit.input.create_pipe_input` + `DummyOutput`.

**Code sketches:**

```python
# src/ccr/console/app.py
async def run(settings: Settings) -> None:
    session = PromptSession(
        message="ccr> ",
        history=FileHistory(str(settings.data_dir / "console_history")),
    )
    print_banner(settings)
    while True:
        try:
            line = await session.prompt_async(completer=await build_completer())
        except (EOFError, KeyboardInterrupt):
            return
        await dispatch(line.strip(), settings)
```

**Acceptance criteria:**
- `python -m ccr console` opens a `ccr>` prompt and accepts commands.
- `pair list` prints a column-aligned table.
- `help` lists all commands. `unknown_cmd` prints `Unknown command. Type 'help'.`
- `python -m ccr console --once "pair list"` prints the table and exits 0.
- `pytest tests/test_console.py` passes.

**Out of scope:** Permission UI for inline buttons via console; that stays in Telegram.

---

### Phase 5: Bot Scaffold + Allowlist Middleware + Pairing Flow + Owner Notification

**Goal:** Running `python -m ccr serve` starts an aiogram polling bot. An unpaired user sending `/start` gets a pairing code and the owner receives a Telegram DM with the approve command. After console approval, `/start` shows a paired status. All non-`/start` traffic from unpaired users is silently rejected.

**Dependencies:** Phase 3 complete; Phase 4 helpful but not blocking.

**Files to create or modify:**
- `src/ccr/bot/app.py` — `def build_dispatcher(settings, db_factory) -> Dispatcher`; `async def run_polling(...)`.
- `src/ccr/bot/middlewares.py` — `class AllowlistMiddleware(BaseMiddleware)` injecting `is_paired_user` flag and `last_chat_id` updates; short-circuits non-`/start` updates from non-paired senders with `"Not paired. Send /start to request access."`
- `src/ccr/bot/notify.py` — `async def notify_owner(bot, db, message: str)` and `async def broadcast_paired(bot, db, message: str, exclude: set[int] = ...)`.
- `src/ccr/bot/handlers/pairing.py` — `/start` handler:
  - if paired → reply with `"Paired. Send a prompt to start, or /new for a fresh session."`
  - else if owner exists → call `pairing.create_code`, reply with `"Access requested. The owner has been notified."` and `notify_owner` with the approve hint
  - else → call `pairing.create_code`, reply with the code + Telegram user ID + the exact `pair approve` command (this is the bootstrap path for the very first owner)
- `src/ccr/server.py` — start dispatcher polling task; expose `async serve(settings)`.
- `src/ccr/cli.py` — implement `serve` subcommand calling `server.serve`.
- `tests/test_bot_pairing.py` — using aiogram's mock infrastructure, assert:
  - bootstrap (no owner): `/start` from unknown user replies with the code visible
  - normal (owner exists): `/start` from new user replies with "owner notified" *and* `notify_owner` was called with the code
  - paired user: `/start` shows status
  - unpaired plain text: rejection string

**Packages to install:** none new.

**Tasks:**
1. Build dispatcher with `Dispatcher()` and a router per handler module.
2. Register `AllowlistMiddleware` on `dp.message` and `dp.callback_query`. Side effect: persists `last_chat_id` on every message from a paired user (used later by notify functions).
3. Implement `/start` per the three branches above.
4. Implement `notify.py`. Owner DMs go to `paired_users.last_chat_id` for the owner; if null (owner hasn't messaged the bot since pairing), log a warning and skip.
5. Wire `cli.py serve` → `asyncio.run(server.serve(settings))`. Initially this only runs polling; uvicorn is added in Phase 10.
6. Write tests against a mocked bot.

**Code sketches:**

```python
# src/ccr/bot/handlers/pairing.py
@router.message(Command("start"))
async def cmd_start(msg: Message, db: AsyncSession, bot: Bot) -> None:
    if await is_paired(db, msg.from_user.id):
        await msg.answer("Paired. Send a prompt to start, or /new for a fresh session.")
        return

    code = await pairing.create_code(db, msg.from_user.id, msg.from_user.username)
    owner = await pairing.get_owner(db)
    if owner is None:
        await msg.answer(
            f"Bootstrap pairing.\nTelegram ID: <code>{msg.from_user.id}</code>\n"
            f"Code: <code>{code.code}</code>\n\n"
            f"On the project host:\n<code>python -m ccr pair approve {code.code}</code>",
            parse_mode="HTML",
        )
    else:
        await msg.answer("Access requested. The owner has been notified.")
        await notify_owner(
            bot, db,
            f"Pairing request from @{msg.from_user.username or '?'} "
            f"(id <code>{msg.from_user.id}</code>).\n"
            f"Approve with: <code>python -m ccr pair approve {code.code}</code>"
        )
```

**Acceptance criteria:**
- `python -m ccr serve` starts polling and logs `Bot started, awaiting updates`.
- `pytest tests/test_bot_pairing.py` passes, covering all four paths above.
- A test asserts `notify_owner` is *not* called in the bootstrap path.
- A test asserts unpaired plain text returns the fixed rejection string and never reaches a session handler.

**Out of scope:** Session commands. Web server still off.

---

### Phase 6: Session Lifecycle Handlers + Telegram Formatting + Multi-User Broadcast

**Goal:** Any paired user can run `/new`, send free-text prompts, see streamed text/thinking/tool-use in their chat without raw diff dumps, and run `/stop` or `/clear`. Session output is broadcast to *all* paired users so a friend with permission also follows along.

**Dependencies:** Phase 5 complete; Phase 7 (Claude wrapper) implemented in parallel — see ordering note.

**⚠️ Ordering note:** Phase 7 below builds the Claude subprocess wrapper. Implement Phase 7 first if not already done, then this phase. Listed in this position to keep the user-facing-feature flow readable; commit order should be Phase 7 → Phase 6.

**Files to create or modify:**
- `src/ccr/bot/formatting.py` — `def event_to_messages(event: ClaudeEvent) -> list[OutboundMessage]`:
  - text/thinking → chunks of ≤ 3500 chars split at sentence/newline boundaries
  - tool_use → one-line summary `"🔧 {tool_name} {short_args}"` (truncate args to 200 chars); never include full tool input
  - tool_result → ignored unless it carries an error → `"❌ {tool_name}: {first 200 chars}"`
  - result event → `"✅ done · {duration_ms}ms · ${cost}"` or `"❌ failed: {subtype}"`
  - permission_request → handled in Phase 8, returns `[]` here
  - unknown → `[]`
- `src/ccr/bot/handlers/session.py`:
  - `/new` → `manager.new_session(prompt=None, started_by=msg.from_user.id)`, reply `"Session <id8> started."`
  - `/stop` → `manager.stop()`, reply `"Session stopped."` (idempotent: `"No active session."` if none)
  - `/clear` → `/stop` then `/new`
  - `/who` → owner sees full list, friends see count + owner's @username
  - plain text → `manager.new_session(prompt=text, started_by=...)` if idle else `manager.send(text)`; reply `"Forwarded."`
- `src/ccr/server.py` — start a single broadcast task that subscribes to `bus.subscribe("session.event")` and forwards each formatted message to *all* paired users with `last_chat_id IS NOT NULL`. Concurrency: per-chat `asyncio.Queue` to avoid head-of-line blocking; one sender task per chat.
- `tests/test_bot_session.py` — using a fake `SessionManager` and mocked bot, assert `/new`, `/stop`, plain text behaviour.
- `tests/test_formatting.py` — feed each event variant through `event_to_messages` and assert chunking + summarization rules; assert no message exceeds 4096 chars.
- `tests/test_broadcast.py` — three paired users with `last_chat_id` set; publish an event; assert all three sender queues receive the formatted message.

**Packages to install:** none new.

**Tasks:**
1. Implement `event_to_messages` with thorough tests.
2. Implement session handlers; inject `SessionManager` via aiogram workflow data (`dp["session_manager"] = mgr`).
3. Implement broadcast in `server.serve` after Phase 7's `SessionManager` is wired.
4. Format chunking rule: prefer split on `\n\n`, fall back to `\n`, fall back to `. `, fall back to hard cut at 3500 chars. Keep code blocks intact when small.
5. Smoke-test with real `claude` binary in a separate manual checklist in README.

**Code sketches:**

```python
# src/ccr/bot/formatting.py
TELEGRAM_HARD_LIMIT = 4096
SAFE_CHUNK = 3500

def chunk_text(text: str) -> list[str]:
    if len(text) <= SAFE_CHUNK:
        return [text]
    # ... split logic with paragraph/sentence preference ...
```

**Acceptance criteria:**
- `pytest tests/test_bot_session.py tests/test_formatting.py tests/test_broadcast.py` passes.
- Formatting test asserts a 10 000-char text event becomes ≥ 3 messages, none > 4096 chars, with no mid-sentence cuts when paragraph breaks exist.
- Broadcast test asserts events fan out to all paired users with known chat IDs.
- Manual smoke: `/new` then `"List the files in this project"` produces streamed output in Telegram with file names visible but no full file contents dumped.
- `/stop` on idle session returns `"No active session."` and exits cleanly; running it twice in a row doesn't crash.

**Out of scope:** Permission inline buttons (next phase). Slash passthrough commands. `/view` / `/preview`.

---

### Phase 7: Claude Subprocess Wrapper, Event Bus, JSONL Logger

**Goal:** A `SessionManager` can spawn `claude -p --input-format=stream-json --output-format=stream-json --verbose`, write user-turn JSONL into stdin, parse stdout JSONL into typed `ClaudeEvent` objects, publish them on an in-process `EventBus`, and persist them to `data/logs/<session_id>.jsonl`.

**Dependencies:** Phase 2 complete.

**Files to create or modify:**
- `src/ccr/events/bus.py` — `class EventBus` with `subscribe(topic) -> AsyncIterator[Event]` and `async publish(topic, payload)`. Uses `asyncio.Queue` per subscriber, drops oldest on slow consumer with a warning.
- `src/ccr/claude/events.py` — Pydantic discriminated-union `ClaudeEvent = Annotated[Union[SystemInit, UserTurn, AssistantTurn, ResultEvent, PermissionRequest, UnknownEvent], Field(discriminator="type")]`. Includes inner `ContentBlock` union (text/thinking/tool_use/tool_result).
- `src/ccr/claude/process.py` — `class ClaudeProcess`:
  - `async start(self) -> None` (spawns subprocess)
  - `async send_user_turn(self, content: str | list[ContentBlock]) -> None`
  - `async send_permission_response(self, request_id: str, choice: str) -> None`
  - `async events(self) -> AsyncIterator[ClaudeEvent]`
  - `async stop(self, grace: float) -> None`
- `src/ccr/claude/log.py` — `class JsonlSessionLog` with `append(event)`, `read_from(seq) -> AsyncIterator[(seq, event)]`, `tail() -> AsyncIterator[(seq, event)]`. Sequence numbers based on file line count. `prune(retention_count, retention_days)` for startup cleanup.
- `src/ccr/claude/manager.py` — `class SessionManager`:
  - holds at most one running `ClaudeProcess` plus its `Session` row, `JsonlSessionLog`, and consumer task
  - `async new_session(prompt: str | None, started_by: int | None) -> UUID`
  - `async send(prompt: str) -> None`
  - `async send_permission(session_id, request_id, choice)`
  - `async stop() -> None`
  - `async status() -> SessionStatus`
  - on each event: write to log + publish to `bus`
  - on subprocess exit: update `Session.status` accordingly, publish synthetic `error` event on crash
- `tests/fakes/fake_claude.py` — small Python script reading JSONL from stdin and writing canned JSONL to stdout based on directives in env vars.
- `tests/test_claude_events.py` — round-trip parses each known event variant from fixture JSONL.
- `tests/test_session_manager.py` — replace subprocess with the fake; assert events persisted, bus receives them, status transitions correctly, `stop()` is idempotent, crash detected.

**Packages to install:** none new.

**Tasks:**
1. Build `EventBus` with weakref-tracked subscriber queues.
2. Define `ClaudeEvent` discriminated union; `UnknownEvent` swallows unknown `type` values.
3. Implement `ClaudeProcess` using `asyncio.create_subprocess_exec` with stdin, stdout, stderr pipes.
4. Read stdout line-by-line; `json.loads` then `TypeAdapter(ClaudeEvent).validate_python`.
5. Buffer stderr separately; surface as `UnknownEvent(type="stderr", raw={...})` only when subprocess crashes.
6. Implement `JsonlSessionLog` with append-only writes (`asyncio.Lock` for serialization) and tail via an internal `asyncio.Event` notified by `append()`.
7. Implement `SessionManager` enforcing single running session.
8. Implement startup pruning: keep last `LOG_RETENTION_COUNT` files; delete files older than `LOG_RETENTION_DAYS`.
9. Write the fake Claude script and tests.

**Code sketches:**

```python
# src/ccr/claude/process.py
class ClaudeProcess:
    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._settings.claude_bin,
            "-p", "--input-format=stream-json",
            "--output-format=stream-json", "--verbose",
            stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=self._cwd,
        )

    async def send_user_turn(self, content: str) -> None:
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
        self._proc.stdin.write((line + "\n").encode())
        await self._proc.stdin.drain()
```

**Acceptance criteria:**
- `pytest tests/test_claude_events.py tests/test_session_manager.py` passes.
- A test exercises the fake subprocess and asserts: 5 events persisted to JSONL with sequence 0–4; `EventBus` subscriber receives the same 5 events; `status()` transitions `idle → running → completed`.
- A test asserts that killing the fake subprocess mid-stream sets `Session.status = 'crashed'` and publishes a synthetic error event.
- `python -c "from ccr.claude.events import ClaudeEvent; from pydantic import TypeAdapter; print(TypeAdapter(ClaudeEvent).json_schema())"` prints a non-empty schema.

**Out of scope:** Real Claude Code invocation in tests; bot or web integration.

---

### Phase 8: Permission Inline-Button Handling

**Goal:** When Claude Code emits `permission_request`, the bot pauses event forwarding for that session, sends a Telegram message with inline buttons matching the offered options, and routes the user's tap back to the subprocess. Streaming resumes after the choice. Any paired user can answer.

**Dependencies:** Phases 6 and 7 complete.

**Files to create or modify:**
- `src/ccr/bot/keyboards.py` — `def permission_kb(session_id, request_id, options) -> InlineKeyboardMarkup` with one button per option; `callback_data=f"perm:{session_id}:{request_id}:{choice}"`.
- `src/ccr/bot/formatting.py` — augment to return `(text, keyboard)` for `PermissionRequest`. Update return type to `OutboundMessage = (text, keyboard | None)`.
- `src/ccr/bot/handlers/permission.py` — `@router.callback_query(F.data.startswith("perm:"))` handler:
  - paired check (middleware already enforces)
  - parse session_id, request_id, choice; validate choice against allowed set
  - check `session_id` matches running session; if stale, `cb.answer("Stale prompt", show_alert=True)`
  - call `manager.send_permission(session_id, request_id, choice)`
  - edit message: remove keyboard, append `"→ {choice} (by @{cb.from_user.username})"` so all paired users see who answered
- `src/ccr/claude/manager.py` — add `pending_permissions: dict[str, asyncio.Event]` and gating logic so the broadcast task pauses delivery to Telegram between a `permission_request` and the matching choice. SSE stream still gets all events live.
- `tests/test_bot_permission.py` — mocked bot + fake manager. Cases: emit `permission_request` → keyboard sent; simulate `callback_query` from a paired user → `send_permission_response` called with right args, message edited; tap from a non-paired user → 401-equivalent rejection; stale session_id → alert, no manager call.

**Packages to install:** none new.

**Tasks:**
1. Define `OutboundMessage` type and refactor `event_to_messages` to return it.
2. Update broadcast task in `server.py` to handle keyboard-bearing messages.
3. Build `permission_kb`. Buttons get short labels (`Approve`, `Skip`, `Abort`) with original choice strings preserved in `callback_data`.
4. Implement `permission.py` handler with the cases above.
5. Add gating in `SessionManager`: when a `permission_request` is published, increment a counter; the broadcast task checks `manager.is_telegram_paused(session_id)` and buffers if paused, draining when the response arrives.
6. Tests cover happy path, stale button, unknown choice, non-paired tapper.

**Code sketches:**

```python
# src/ccr/bot/keyboards.py
def permission_kb(session_id: UUID, request_id: str, options: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=opt.capitalize(),
                              callback_data=f"perm:{session_id}:{request_id}:{opt}")]
        for opt in options
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
```

**Acceptance criteria:**
- `pytest tests/test_bot_permission.py` passes.
- A test asserts feeding `permission_request{options=["approve","skip","abort"]}` produces one message with three inline buttons whose `callback_data` matches the spec.
- A test asserts tapping `Approve` calls `send_permission_response(request_id, "approve")` exactly once and the keyboard is removed.
- A test asserts the edited message includes the responder's username.
- Buffering test: while a permission is pending, an injected `text` event is held; once responded, the buffered event is delivered before any later events.

**Out of scope:** Per-session permission UI in the web viewer (post-MVP).

---

### Phase 9: Slash-Command Passthrough Whitelist

**Goal:** `/cost`, `/model`, `/compact` are forwarded to the active Claude Code session as that session's slash commands; `/agents`, `/mcp`, `/init` and any other Claude slash command receive a clear "run on your machine" reply.

**Dependencies:** Phase 6 complete.

**Files to create or modify:**
- `src/ccr/bot/handlers/passthrough.py` — `WHITELIST = {"cost", "model", "compact"}`; `BLOCKED_INTERACTIVE = {"agents", "mcp", "init"}`. Handler matches commands not already claimed by other routers.
- `src/ccr/claude/manager.py` — add `async send_slash(self, name: str, args: str) -> None` that prepends `"/" + name + " " + args` and submits as a normal user turn (Claude Code interprets it the same as if typed in TTY).
- `tests/test_passthrough.py` — `/cost` calls `send_slash("cost", "")`; `/agents` is blocked with the documented message; unknown `/foo` returns the usage hint.

**Packages to install:** none new.

**Tasks:**
1. Implement passthrough handler with three branches (whitelist vs blocked-interactive vs unknown).
2. Wire `manager.send_slash`.
3. Tests.

**Acceptance criteria:**
- `pytest tests/test_passthrough.py` passes.
- `/cost` produces a Telegram-side cost summary in the manual smoke test.
- `/agents` produces: `"Interactive command — run /agents in your local Claude Code terminal."`
- `/totallyunknown` returns: `"Unknown command. Whitelisted: /new /stop /clear /view /last /preview /cost /model /compact /who."`

**Out of scope:** Expanding the whitelist.

---

### Phase 10: JWT Signed-URL Token Module

**Goal:** A reusable module mints short-lived JWTs (30 min) for two kinds of URLs (`viewer`, `preview`) and verifies them. No DB writes — tokens are stateless.

**Dependencies:** Phase 2 complete.

**Files to create or modify:**
- `src/ccr/auth/tokens.py`:
  - `class TokenKind(StrEnum): VIEWER = "viewer"; PREVIEW = "preview"`
  - `def mint(kind: TokenKind, tg_user_id: int, payload: dict, settings: Settings) -> str`
  - `def verify(token: str, settings: Settings) -> VerifiedToken` (raises `TokenError`)
  - `def verify_kind(token, settings, expected: TokenKind) -> VerifiedToken`
  - Claims: `sub=tg_user_id`, `kind`, `payload`, `iat`, `exp`, `jti`
- `tests/test_tokens.py` — mint then verify; expired token rejected (TTL = 1800 default); tampered signature rejected; wrong kind rejected.

**Packages to install:** none new.

**Tasks:**
1. Implement using HS256 with `JWT_SECRET`.
2. Encode payload as inner JSON (e.g. `{"port": 3000}` for preview).
3. Tests with manual `now` injection (`leeway=0`).

**Acceptance criteria:**
- `pytest tests/test_tokens.py` passes.
- A test asserts a token minted at T=0 with TTL=1800 fails verification at T=1801.
- A token with `kind=viewer` fails `verify_kind(..., PREVIEW)` with a clear error.
- A test asserts the JWT payload includes `sub`, `kind`, `iat`, `exp`, `jti`.

**Out of scope:** HTTP layer (next phase).

---

### Phase 11: Web Server, Auth Handoff, Sessions REST + SSE

**Goal:** A FastAPI app, mounted in the same process as the bot, serves `/healthz`, `/auth`, `/api/sessions`, `/api/sessions/{id}/events` (SSE), `/api/sessions/{id}/log`. Authenticated by short-lived JWT in URL → HttpOnly cookie (30 min). Unauthenticated requests get 401.

**Dependencies:** Phases 7 and 10 complete.

**Files to create or modify:**
- `src/ccr/web/app.py` — `def create_app(settings, manager, bus, db_factory) -> FastAPI`; CORS allow `PUBLIC_URL` only.
- `src/ccr/web/auth.py`:
  - `GET /auth?token=<jwt>&next=<path>` → verify, set cookie `ccr_session` (30 min TTL, HttpOnly, Secure, SameSite=Lax), 302 to `next` (validate `next` starts with `/viewer` or `/app/`).
  - `class CookieAuthDep` FastAPI dependency that extracts cookie and calls `verify_kind(...)` per route requirement.
- `src/ccr/web/sessions.py`:
  - `GET /api/sessions` → list `Session` rows (ordered `started_at desc`, limit 50).
  - `GET /api/sessions/{id}/events?from=<seq>` → SSE: replay JSONL from `from` then tail via `bus.subscribe("session.event")` filtered to `id`; close on session `completed/stopped/crashed` after the final `result` event.
  - `GET /api/sessions/{id}/log` → stream the JSONL file as `application/x-ndjson`.
  - `GET /healthz` → `{"ok": true, "session_status": ...}`.
- `src/ccr/server.py` — `serve()` now starts both the dispatcher polling task *and* `uvicorn.Server(Config(app, host, port, lifespan="on"))` as concurrent tasks; cancellation cascades.
- `tests/test_web_auth.py` — using `httpx.AsyncClient(app=app)`:
  - `GET /viewer` without cookie → 401 (or redirect-to-/auth)
  - `GET /auth?token=<valid>&next=/viewer` → 302 + Set-Cookie
  - subsequent `GET /viewer` with cookie → 200
  - expired token → 401
  - `next=/evil/path` → 400
- `tests/test_sse_stream.py` — replay-then-tail: pre-seed JSONL with 3 events, subscribe with `from=0`, assert client receives 3 events, then publish 1 more to bus, assert client receives 4th.

**Packages to install:** none new (manual SSE via `StreamingResponse`).

**Tasks:**
1. Build FastAPI app factory; inject manager + bus via `app.state`.
2. Implement `/auth` with strict `next` validation (whitelist prefixes).
3. Implement SSE endpoint manually as `StreamingResponse`. Heartbeat comment every 15 s.
4. Implement cookie middleware and dependency.
5. `serve()` orchestration: `await asyncio.gather(bot_task, web_task, broadcast_task)` with mutual cancellation on first error.
6. Tests.

**Code sketches:**

```python
# src/ccr/web/sessions.py
async def stream_session(id: UUID, from_seq: int, ...):
    async def gen():
        async for seq, event in log.read_from(from_seq):
            yield f"id: {seq}\ndata: {event.model_dump_json()}\n\n"
        async for evt in bus.subscribe("session.event"):
            if evt.payload.session_id == id:
                yield f"data: {evt.payload.event.model_dump_json()}\n\n"
                if evt.payload.event.type == "result":
                    return
    return StreamingResponse(gen(), media_type="text/event-stream")
```

**Acceptance criteria:**
- `pytest tests/test_web_auth.py tests/test_sse_stream.py` passes.
- `python -m ccr serve` boots both bot and uvicorn; `curl http://127.0.0.1:8765/healthz` returns `{"ok": true, ...}`.
- `curl -i "http://127.0.0.1:8765/api/sessions"` without cookie returns 401.
- After minting a token via a small test script, `curl -L -c /tmp/c -b /tmp/c "http://127.0.0.1:8765/auth?token=...&next=/api/sessions"` returns the JSON list.
- SSE smoke: `curl -N -b /tmp/c "http://127.0.0.1:8765/api/sessions/<id>/events"` receives `data:` lines while a session runs.

**Out of scope:** Frontend HTML; reverse proxy.

---

### Phase 12: Multi-Tab Viewer Frontend (xterm.js via CDN)

**Goal:** Opening `/viewer` in a browser shows one tab per session for this project, each rendering the JSONL event stream as terminal-style output via xterm.js loaded from jsDelivr with SRI. Status indicators per tab: green (running), yellow (no events 30 s), red (no events 2 min), grey (completed/stopped/crashed). Read-only.

**Dependencies:** Phase 11 complete.

**Files to create or modify:**
- `src/ccr/web/static/viewer.html` — bare scaffold with a tab strip container, terminal container, and:
  ```html
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/xterm@5.5.0/css/xterm.css"
        integrity="sha384-..." crossorigin="anonymous">
  <script src="https://cdn.jsdelivr.net/npm/xterm@5.5.0/lib/xterm.js"
          integrity="sha384-..." crossorigin="anonymous"></script>
  <script type="module" src="/static/viewer.js"></script>
  ```
  (Real SRI hashes generated during this phase via `curl ... | openssl dgst -sha384 -binary | openssl base64 -A`.)
- `src/ccr/web/static/viewer.css` — mobile-first horizontal scrolling tab strip; xterm container fills below.
- `src/ccr/web/static/viewer.js`:
  - On load: `GET /api/sessions` → render tabs.
  - Per tab, lazy-init `Terminal` from xterm.js when activated; open `EventSource('/api/sessions/{id}/events?from=' + lastSeq)` and dispatch by event type:
    - text/thinking → `term.write(payload + "\r\n")`
    - tool_use → coloured one-line summary
    - tool_result → coloured success/error summary
    - permission_request → `[permission requested: {tool} - awaiting tap in Telegram]`
    - result → final summary; close EventSource
  - Status indicator: setInterval every 5 s recomputing from `lastEventTs`.
  - Tab close button (only for completed/stopped/crashed tabs) — local-only dismiss.
  - Reconnect: on EventSource error, retry after 2 s with exponential backoff up to 30 s; pass last received `seq` as `?from=`.
- `src/ccr/web/viewer.py` — `GET /viewer` returns `viewer.html` with cookie auth required; static files served via `FastAPI.mount("/static", StaticFiles(directory=...))`.
- `tests/test_viewer.py` — `GET /viewer` without cookie → 401; with cookie → 200 and `Content-Type: text/html`. Assert SRI hashes are present in the served HTML.

**Packages to install:** none new.

**Tasks:**
1. Generate SRI hashes for `xterm@5.5.0` JS and CSS files; commit them in `viewer.html`. Document the regeneration command in a comment.
2. Build the JS as a single ES module (no bundler).
3. Tab strip: horizontal flex container with overflow-x scroll on mobile.
4. Terminal palette: dark bg, ANSI-256 enabled.
5. Reconnect logic with exponential backoff and `?from=` resume.
6. Status indicator colours via CSS classes driven by `data-state` attribute.
7. Smoke checklist in README for manually exercising on mobile + desktop.

**Acceptance criteria:**
- `pytest tests/test_viewer.py` passes.
- Manual: open `/viewer` after `/view` link from Telegram → see at least one tab; run `/new "say hello"` → live text appears in the active tab within 2 s; tab indicator goes from green → grey on `result`.
- Manual offline-test: with the dev machine's internet disabled, viewer page fails to load (expected with CDN choice). Documented as a known trade-off in README.
- Disconnect (kill server) for 5 s, restart → viewer reconnects and resumes from last seq without showing duplicates.
- Resize: on a 360 px viewport, tab strip scrolls horizontally; terminal fills remaining height; no horizontal scroll on the body.
- Browser dev tools confirm xterm.js loaded with `integrity` matching; tampering the SRI hash makes the page fail to load (manual check).

**Out of scope:** Per-subagent tabs; writing back to sessions; theming; offline use.

---

### Phase 13: `/view`, `/last`, `/preview` + Localhost Reverse Proxy

**Goal:** Bot commands `/view`, `/last` mint signed `viewer` URLs (30 min) and reply with them. `/preview <port>` mints a `preview` token and replies with `${PUBLIC_URL}/auth?token=...&next=/app/<port>/`. The web app reverse-proxies `/app/<port>/{path}` to `http://127.0.0.1:<port>/{path}` for cookie-authed users, with optional port allowlist.

**Dependencies:** Phases 10, 11 complete.

**Files to create or modify:**
- `src/ccr/bot/handlers/view.py`:
  - `/view` → mint VIEWER token; reply `f"{PUBLIC_URL}/auth?token={t}&next=/viewer"` with a "valid 30 min" note.
  - `/last` → look up most recent session id; mint VIEWER token; reply with `next=/viewer?session={id}`.
  - `/preview <port>` → validate `1 ≤ port ≤ 65535` and (if `PROXY_PORT_ALLOWLIST` set) port is in it; mint PREVIEW token with `payload={"port": port}`; reply with `next=/app/{port}/`.
- `src/ccr/web/proxy.py`:
  - `@app.api_route("/app/{port:int}/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"])`
  - Cookie auth required; verify token kind == PREVIEW and `payload.port == port`.
  - Use a singleton `httpx.AsyncClient(timeout=30, follow_redirects=False)`. Stream both directions.
  - Strip `Cookie` and `Authorization` request headers; rewrite `Host` to `127.0.0.1:{port}`; pass through `X-Forwarded-{For,Proto,Host,Prefix}`.
  - Return upstream `Content-Type` and body verbatim. Reject `Upgrade: websocket` with 502 + clear error body.
- `tests/test_proxy.py` — spin a tiny test server on a random port; assert `/app/<port>/foo` returns its body, with cookie auth, and 401 without; 403 if port not allowlisted; 403 if cookie token's payload.port doesn't match URL port.

**Packages to install:** none new.

**Tasks:**
1. Implement the three bot handlers. URL minting helper: `def build_url(settings, kind, payload, next_path) -> str`.
2. Implement the proxy with streaming request/response bodies (`StreamingResponse` over `client.stream()`).
3. Define explicit hop-by-hop header stripping (RFC 7230 § 6.1).
4. Optional `PROXY_PORT_ALLOWLIST` enforcement.
5. Tests using a pytest-managed upstream fixture.

**Acceptance criteria:**
- `pytest tests/test_proxy.py` passes.
- Manual: run `python -m http.server 9000` in another terminal; `/preview 9000` → tap link → see the directory listing.
- `/preview 999999` returns `"Port out of range"`.
- With `PROXY_PORT_ALLOWLIST=3000`, `/preview 9000` returns `"Port 9000 not in PROXY_PORT_ALLOWLIST."`.
- `/view` link works with no active session and shows the session list (empty state).

**Out of scope:** WebSocket proxying.

---

### Phase 14: Composite Entrypoint, install.sh, Submodule, README

**Goal:** A fresh user clones the repo, runs `./install.sh`, edits `.env`, runs `python -m ccr serve`, opens Telegram, sends `/start`, runs `python -m ccr console` (or one-shot `pair approve`), and is fully operational. The README's "Run locally in 60 seconds" section walks through this exact path.

**Dependencies:** All previous phases complete.

**Files to create or modify:**
- `install.sh`:
  - Check Python ≥ 3.12, `uv` installed (offer one-line installer command), `claude` on PATH.
  - `git submodule update --init --recursive`.
  - `uv sync`.
  - Generate `JWT_SECRET=$(openssl rand -hex 32)` and write to `.env` if absent (copying from `.env.example` first).
  - `mkdir -p data/logs`.
  - `python -m ccr init-db`.
  - Print next steps (Telegram bot token, tunnel setup, console).
- `.gitmodules` — single submodule `templates/ → <dev-stack-agents repo URL>` on `main`.
- `src/ccr/server.py` — preflight checks: Claude CLI present, DB up to date, `PUBLIC_URL` reachable best-effort warning (`httpx.get(public_url, timeout=2)` and log only).
- `src/ccr/cli.py` — `doctor` subcommand printing all preflight check results.
- `README.md`:
  - Project summary
  - "Run locally in 60 seconds" — exact commands
  - Console walkthrough (how to invite a friend)
  - Tunnel setup (Tailscale Funnel; alternatives noted)
  - Bot command reference + console command reference
  - Troubleshooting
  - Security model section: trust boundaries, what 30-min tokens protect against, why CDN xterm.js
  - License (confirm with project owner)
- `tests/test_doctor.py` — doctor command runs and exits 0 on healthy environment, 1 on broken.

**Packages to install:** none new.

**Tasks:**
1. Write `install.sh` with `set -euo pipefail` and idempotent steps.
2. Add submodule reference; commit.
3. Implement `doctor` subcommand and use the same checks at `serve` startup (logging warnings).
4. Author README with these exact "Run locally in 60 seconds" commands:
   ```bash
   git clone --recurse-submodules <repo>
   cd claude-code-remote
   ./install.sh
   # edit .env: TELEGRAM_BOT_TOKEN, PUBLIC_URL
   python -m ccr serve
   # in Telegram: /start
   # in another terminal on the host:
   python -m ccr pair approve <code>   # or: python -m ccr console
   ```
5. Add the "invite a friend" walkthrough:
   ```bash
   # friend opens the bot, sends /start
   # owner gets a Telegram DM with the approve command
   # owner runs:
   python -m ccr pair approve <code>
   # or, in console:
   python -m ccr console
   ccr> pair approve <code>
   ```
6. Manual end-to-end run-through: bootstrap pair → invite friend → friend pairs → both `/new` → permission → `/view` → `/preview` → `/stop`.
7. Final pass on lint, mypy, tests.

**Acceptance criteria:**
- On a clean VM with Python 3.12 + uv + Claude Code installed: `git clone --recurse-submodules ... && cd ... && ./install.sh` completes without errors in under 2 min.
- `python -m ccr doctor` prints `OK` for: Python version, claude binary, DB ready, JWT_SECRET set, PUBLIC_URL reachable, xterm.js CDN reachable.
- `pytest` (full suite) passes.
- `mypy src` passes.
- `ruff check src tests` passes.
- README "60 seconds" section, when followed verbatim, results in a working bot reachable via Telegram with the bootstrap user paired.
- README "invite a friend" walkthrough, when followed, results in a second paired user receiving session output in their chat.

**Out of scope:** docker-compose; cloud deployment; CI release pipeline.

---

## 9. Testing Strategy

- **Unit tests (pytest + pytest-asyncio).** Each module gets a focused test file. Coverage focus areas: pairing state machine including owner promotion + revocation refusal, JWT mint/verify with 30-min TTL, event Pydantic parsing (every known type plus `UnknownEvent`), Telegram formatter chunking and tool-summarization rules, JSONL log read-from-seq + tail ordering, `SessionManager` lifecycle, console REPL command dispatch.
- **Integration tests.** A fake Claude Code subprocess (`tests/fakes/fake_claude.py`) replaces the real binary by emitting canned JSONL on stdin prompts. Drives `SessionManager` end-to-end through `EventBus` to a subscriber. Web-side: `httpx.AsyncClient(app=app)` exercises auth + SSE + sessions REST + reverse proxy. Multi-user broadcast verified with three paired users in the DB.
- **End-to-end (manual checklist in README).** Numbered scenarios: bootstrap pairing, invite friend, `/new` + plain text, permission approve from friend's account, `/view` link, `/preview <port>` against `python -m http.server`, `/stop`, subprocess crash recovery, 30-min token expiry, owner revokes friend → friend `/start` rejected.
- **Test data.** Pydantic factories via plain helper functions. JSONL fixtures in `tests/fixtures/events/*.jsonl`, parameterized.
- **Coverage target.** 80% line coverage on `src/ccr/{auth,claude,bot,web,console}`; lower on `cli.py` acceptable.
- **CI.** `ruff check`, `ruff format --check`, `mypy src`, `pytest --cov=ccr --cov-fail-under=80` on every push.

## 10. Local Development

```bash
# 1. Clone with submodule
git clone --recurse-submodules <repo-url>
cd claude-code-remote

# 2. One-shot install
./install.sh

# 3. Fill in .env (TELEGRAM_BOT_TOKEN, PUBLIC_URL, anything else custom)
$EDITOR .env

# 4. Start the bot + web server
python -m ccr serve

# 5. In another terminal: tunnel the web port
tailscale funnel --bg 8765        # or `ngrok http 8765`, etc.

# 6. From Telegram → /start
# 7. Back in the project terminal — choose one:
python -m ccr pair approve <code>          # one-shot
python -m ccr console                       # interactive REPL

# Day-to-day:
python -m ccr serve          # run server
python -m ccr console        # manage paired users
python -m ccr doctor         # health check
pytest                       # tests
ruff check src tests         # lint
mypy src                     # type check
```

Updating `templates/`:
```bash
git submodule update --remote templates
git add templates && git commit -m "chore: bump dev-stack-agents"
```

Inviting a friend (worked example):
```bash
# Friend opens your bot in Telegram, sends /start.
# Your phone gets an owner-DM with the approve command.
# On the project host, either:
python -m ccr pair approve ABCD1234
# or:
python -m ccr console
ccr> pair pending
ccr> pair approve ABCD1234
ccr> pair list
```

## 11. Deployment Notes

- **Target:** Local PC + user-supplied tunnel. No cloud target supported in MVP.
- **Tunnel reference setups:**
  - **Tailscale Funnel (default):** `tailscale funnel --bg 8765` after enabling Funnel for the tailnet. `PUBLIC_URL=https://<machine>.<tailnet>.ts.net`.
  - **ngrok:** `ngrok http 8765`; use the assigned `https://...ngrok-free.app`.
  - **Cloudflare Tunnel:** `cloudflared tunnel --url http://127.0.0.1:8765`.
- **Process supervision (optional):** A `systemd --user` unit template `claude-code-remote.service` shipped in `docs/` (post-MVP).
- **Secrets:** All in `.env`. Never logged. `JWT_SECRET` rotation invalidates outstanding URLs (acceptable; TTL is 30 min anyway).
- **Migrations on upgrade:** `git pull && uv sync && python -m ccr init-db` (idempotent — runs `alembic upgrade head`).
- **Healthcheck:** `GET /healthz` returns `{"ok": true, "session_status": ...}`; intended for tunnel uptime probes and the console `status` command.

## 12. Risks & Future Work

**Risks:**

- **Claude Code stream-json schema drift.** New event types or shape changes break parsing. *Mitigation:* `UnknownEvent` swallows unknowns; pin a known-good `claude` version range in README; an opt-in `tests/integration/test_real_claude.py` smoke (env var gated) catches drift in nightly CI.
- **Telegram rate limits.** Verbose sessions × multiple paired users can flood and trigger 429. *Mitigation:* per-chat token bucket (e.g. 20 messages / 60 s) with overflow buffered into a "see /view for full output" notice. Track this and add only if observed.
- **Subprocess deadlock on stderr backpressure.** Python's asyncio subprocess pipes can block writers if readers stall. *Mitigation:* dedicated reader tasks for both stdout and stderr, draining unconditionally; bounded internal queues drop oldest with a warning.
- **Reverse-proxy abuse if tunnel and bot token both leak.** *Mitigation:* 30-min JWT TTLs + cookie scoping + optional `PROXY_PORT_ALLOWLIST`. Document that the tunnel hostname should not be shared.
- **CDN xterm.js compromise.** *Mitigation:* SRI hashes pinned in HTML; if the CDN serves tampered bytes, the browser refuses to execute. *Cost:* page is unusable offline. Documented in README; vendoring remains a one-line change if requirements shift.
- **JWT_SECRET in plaintext .env.** Acceptable for single-user local deployment; flagged in README. Future: OS keychain integration.
- **Friend abuse of paired access.** Paired = trusted; a malicious friend can `/stop`, drive sessions, view source via tool calls. *Mitigation:* owner-only revoke is fast; owner revokes with one CLI command. No technical mitigation against trusted-but-malicious; this is a social trust boundary.
- **Telegram username spoofing in DMs.** A user can change their @username after pairing. *Mitigation:* allowlist keyed on `tg_user_id` (immutable), not username; username shown as informational only.
- **One-tunnel-many-projects collisions.** Two project bots on one machine want one tunnel. *Mitigation:* document distinct ports per project.

**Deferred (revisit triggers in parens):**

- Per-subagent kill from chat (when Claude Code exposes parent_tool_use_id reliably and users complain about "I just want to stop one branch").
- WebSocket proxying in `/app/{port}/...` (when a user reports a dev server that needs WS, e.g. Vite HMR).
- Per-paired-user roles (read-only friend vs full friend) — currently all paired users have equal rights except the owner-only approve/revoke.
- docker-compose bootstrap (when an OSS adopter actually files an issue asking for it).
- VPS / hosted deployment story (when the tool's developer wants to leave their PC off).
- Conversation persistence beyond active session — replay a past JSONL into a fresh session as context (when "I want to continue yesterday's session" becomes a recurring need).
- Custom push notifications beyond Telegram defaults (e.g. wake the screen on `permission_request`) — likely lives in Telegram client settings.
- `/agents` and `/mcp` indirect support (read-only listing) — small surface, low value until requested.
- Vendoring xterm.js for offline use — currently CDN; trivial to switch when a user requests offline support.
- Owner transfer / multiple-owners model — if the project gets shared more seriously.
