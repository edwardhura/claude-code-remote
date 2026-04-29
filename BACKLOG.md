# Backlog

Active ticket queue. Holds tickets in statuses `todo`, `in-progress`, and `blocked`.
On approval (`done`) or closure (`closed`), the team-lead moves the ticket entry to `DONE.md`.

Status lives in the title for grep-ability:
```
grep -E '^## CCR-[0-9]+' BACKLOG.md
```

Tickets are separated by a `---` line. Append-only within this file: never delete content,
only flip status in the title or move the entry to `DONE.md`.

---

## CCR-010: Slash-command passthrough whitelist [todo]
Phase: 9
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/passthrough.py` — `WHITELIST = {"cost", "model", "compact"}`; `BLOCKED_INTERACTIVE = {"agents", "mcp", "init"}`. Handler matches commands not already claimed by other routers.
  - `src/ccr/claude/manager.py` — add `async send_slash(self, name: str, args: str) -> None` that prepends `"/" + name + " " + args` and submits as a normal user turn (Claude Code interprets it the same as if typed in TTY).
  - `tests/test_passthrough.py` — `/cost` calls `send_slash("cost", "")`; `/agents` is blocked with the documented message; unknown `/foo` returns the usage hint.
Out of scope:
  - Expanding the whitelist.
Acceptance:
  - [ ] `pytest tests/test_passthrough.py` passes.
  - [ ] `/cost` produces a Telegram-side cost summary in the manual smoke test.
  - [ ] `/agents` produces: `"Interactive command — run /agents in your local Claude Code terminal."`
  - [ ] `/totallyunknown` returns: `"Unknown command. Whitelisted: /new /stop /clear /view /last /preview /cost /model /compact /who."`
Depends on: CCR-008
Notes:
  Three branches in the handler: whitelist (forward via `send_slash`), blocked-interactive (canned reply), unknown (usage hint). The unknown-command response lists slashes from later phases (`/view`, `/last`, `/preview`) — those land in Phase 13; the message text is fixed now to keep UX stable.

### Review log
---

## CCR-011: JWT signed-URL token module [todo]
Phase: 10
Feature: auth
Files:
  - `src/ccr/auth/tokens.py`:
    - `class TokenKind(StrEnum): VIEWER = "viewer"; PREVIEW = "preview"`
    - `def mint(kind: TokenKind, tg_user_id: int, payload: dict, settings: Settings) -> str`
    - `def verify(token: str, settings: Settings) -> VerifiedToken` (raises `TokenError`)
    - `def verify_kind(token, settings, expected: TokenKind) -> VerifiedToken`
    - Claims: `sub=tg_user_id`, `kind`, `payload`, `iat`, `exp`, `jti`
  - `tests/test_tokens.py` — mint then verify; expired token rejected (TTL = 1800 default); tampered signature rejected; wrong kind rejected.
Out of scope:
  - HTTP layer (next phase).
Acceptance:
  - [ ] `pytest tests/test_tokens.py` passes.
  - [ ] A test asserts a token minted at T=0 with TTL=1800 fails verification at T=1801.
  - [ ] A token with `kind=viewer` fails `verify_kind(..., PREVIEW)` with a clear error.
  - [ ] A test asserts the JWT payload includes `sub`, `kind`, `iat`, `exp`, `jti`.
Depends on: CCR-003
Notes:
  HS256 signing with `JWT_SECRET` from settings (≥ 32 chars enforced in CCR-003). Payload is encoded as inner JSON (e.g. `{"port": 3000}` for preview). Tests use manual `now` injection (`leeway=0`). No DB writes — tokens are stateless. This module is consumed by Phase 11 (web auth handoff) and Phase 13 (`/view`/`/last`/`/preview` URL minting in the bot).

### Review log
---

## CCR-012: Web server, auth handoff, sessions REST + SSE [todo]
Phase: 11
Feature: web-viewer
Files:
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
Out of scope:
  - Frontend HTML; reverse proxy.
Acceptance:
  - [ ] `pytest tests/test_web_auth.py tests/test_sse_stream.py` passes.
  - [ ] `python -m ccr serve` boots both bot and uvicorn; `curl http://127.0.0.1:8765/healthz` returns `{"ok": true, ...}`.
  - [ ] `curl -i "http://127.0.0.1:8765/api/sessions"` without cookie returns 401.
  - [ ] After minting a token via a small test script, `curl -L -c /tmp/c -b /tmp/c "http://127.0.0.1:8765/auth?token=...&next=/api/sessions"` returns the JSON list.
  - [ ] SSE smoke: `curl -N -b /tmp/c "http://127.0.0.1:8765/api/sessions/<id>/events"` receives `data:` lines while a session runs.
Depends on: CCR-007, CCR-008, CCR-011
Notes:
  Plan lists Phase 11 deps as Phases 7 + 10, but `serve()` orchestration here adds `web_task` to the existing `gather(bot_task, broadcast_task)` from Phase 6 — so CCR-008 is also a hard dep. Most files land under `src/ccr/web/` (web-developer territory); `src/ccr/server.py` is python-developer territory but the change is small (~10 lines wiring uvicorn into the existing gather). Orchestrator should route to web-developer with awareness that the server.py edit is included; not splitting per the "don't over-slice" rule (the trigger for splitting is `src/ccr/{bot,auth,claude,console,db,events}/` AND `src/ccr/web/`, and server.py isn't in those subpackages). SSE implemented manually as `StreamingResponse`; heartbeat comment every 15 s. `next` validation whitelist prefixes only.

### Review log
---

## CCR-013: Multi-tab viewer frontend (xterm.js via CDN) [todo]
Phase: 12
Feature: web-viewer
Files:
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
Out of scope:
  - Per-subagent tabs; writing back to sessions; theming; offline use.
Acceptance:
  - [ ] `pytest tests/test_viewer.py` passes.
  - [ ] Manual: open `/viewer` after `/view` link from Telegram → see at least one tab; run `/new "say hello"` → live text appears in the active tab within 2 s; tab indicator goes from green → grey on `result`.
  - [ ] Manual offline-test: with the dev machine's internet disabled, viewer page fails to load (expected with CDN choice). Documented as a known trade-off in README.
  - [ ] Disconnect (kill server) for 5 s, restart → viewer reconnects and resumes from last seq without showing duplicates.
  - [ ] Resize: on a 360 px viewport, tab strip scrolls horizontally; terminal fills remaining height; no horizontal scroll on the body.
  - [ ] Browser dev tools confirm xterm.js loaded with `integrity` matching; tampering the SRI hash makes the page fail to load (manual check).
Depends on: CCR-012
Notes:
  All files in `src/ccr/web/` — web-developer territory. Generate SRI hashes for `xterm@5.5.0` JS and CSS at ticket-completion time and commit them in `viewer.html`; document the regeneration command in a comment. Single ES module, no bundler. Dark bg + ANSI-256. Reconnect with exponential backoff to 30 s and `?from=` resume on `EventSource` errors. Status indicator colours via CSS classes driven by `data-state` attribute. Manual smoke checklist for mobile + desktop belongs in README. The "/view link from Telegram" referenced in acceptance is implemented in CCR-014 (Phase 13); team lead should test that step after CCR-014 lands.

### Review log
---

## CCR-014: Bot handlers `/view`, `/last`, `/preview` [todo]
Phase: 13
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/view.py`:
    - `/view` → mint VIEWER token; reply `f"{PUBLIC_URL}/auth?token={t}&next=/viewer"` with a "valid 30 min" note.
    - `/last` → look up most recent session id; mint VIEWER token; reply with `next=/viewer?session={id}`.
    - `/preview <port>` → validate `1 ≤ port ≤ 65535` and (if `PROXY_PORT_ALLOWLIST` set) port is in it; mint PREVIEW token with `payload={"port": port}`; reply with `next=/app/{port}/`.
Out of scope:
  - WebSocket proxying.
Acceptance:
  - [ ] `/preview 999999` returns `"Port out of range"`.
  - [ ] With `PROXY_PORT_ALLOWLIST=3000`, `/preview 9000` returns `"Port 9000 not in PROXY_PORT_ALLOWLIST."`.
  - [ ] `/view` link works with no active session and shows the session list (empty state).
Depends on: CCR-011, CCR-012
Notes:
  Split from Phase 13: this ticket owns the bot-side handlers (python-developer territory `src/ccr/bot/handlers/`). The proxy half is CCR-015. Split fires because Phase 13 touches both `src/ccr/bot/` and `src/ccr/web/`. URL minting helper: `def build_url(settings, kind, payload, next_path) -> str`. The `/view` empty-state acceptance assumes `/viewer` exists (CCR-013) — depend on CCR-012 for the `/auth` → cookie flow that the URL relies on; CCR-013 (viewer HTML) makes the empty state render but is not strictly required for the bot reply itself.

### Review log
---

## CCR-015: Localhost reverse proxy [todo]
Phase: 13
Feature: web-viewer
Files:
  - `src/ccr/web/proxy.py`:
    - `@app.api_route("/app/{port:int}/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"])`
    - Cookie auth required; verify token kind == PREVIEW and `payload.port == port`.
    - Use a singleton `httpx.AsyncClient(timeout=30, follow_redirects=False)`. Stream both directions.
    - Strip `Cookie` and `Authorization` request headers; rewrite `Host` to `127.0.0.1:{port}`; pass through `X-Forwarded-{For,Proto,Host,Prefix}`.
    - Return upstream `Content-Type` and body verbatim. Reject `Upgrade: websocket` with 502 + clear error body.
  - `tests/test_proxy.py` — spin a tiny test server on a random port; assert `/app/<port>/foo` returns its body, with cookie auth, and 401 without; 403 if port not allowlisted; 403 if cookie token's payload.port doesn't match URL port.
Out of scope:
  - WebSocket proxying.
Acceptance:
  - [ ] `pytest tests/test_proxy.py` passes.
  - [ ] Manual: run `python -m http.server 9000` in another terminal; `/preview 9000` → tap link → see the directory listing.
Depends on: CCR-012, CCR-014
Notes:
  Split from Phase 13: this ticket owns the proxy under `src/ccr/web/` (web-developer). `CCR-014` is the bot-handler half. The manual `/preview 9000` acceptance straddles both — listed here because the proxy is what makes it work end-to-end (the bot handler is mocked in CCR-014 tests). Streaming via `StreamingResponse` over `client.stream()`. Hop-by-hop header stripping per RFC 7230 § 6.1. Optional `PROXY_PORT_ALLOWLIST` enforcement (the bot side rejects upfront in CCR-014; the proxy still enforces defensively). Tests use a pytest-managed upstream fixture.

### Review log
---

## CCR-016: Doctor subcommand and serve preflight checks [todo]
Phase: 14
Feature: bootstrap
Files:
  - `src/ccr/server.py` — preflight checks: Claude CLI present, DB up to date, `PUBLIC_URL` reachable best-effort warning (`httpx.get(public_url, timeout=2)` and log only).
  - `src/ccr/cli.py` — `doctor` subcommand printing all preflight check results.
  - `tests/test_doctor.py` — doctor command runs and exits 0 on healthy environment, 1 on broken.
Out of scope:
  - docker-compose; cloud deployment; CI release pipeline.
Acceptance:
  - [ ] `python -m ccr doctor` prints `OK` for: Python version, claude binary, DB ready, JWT_SECRET set, PUBLIC_URL reachable, xterm.js CDN reachable.
Depends on: CCR-007, CCR-012
Notes:
  Split from Phase 14: this ticket owns the python-developer pieces (`src/ccr/server.py` preflight, `src/ccr/cli.py` doctor, `tests/test_doctor.py`). The sysops half (`install.sh`, `.gitmodules`, `README.md`) is CCR-017. Split fires per the rule "phase touches `install.sh` … and source code". `serve()` startup uses the same checks as `doctor` but only logs warnings rather than exiting. Plan lists Phase 14 deps as "all previous phases" — encoded here as CCR-007 (claude binary check, SessionManager) + CCR-012 (server.py orchestration, healthz endpoint). DB and JWT_SECRET checks rely on CCR-003.

### Review log
---

## CCR-017: install.sh, submodule, README, final repo polish [todo]
Phase: 14
Feature: bootstrap
Files:
  - `install.sh`:
    - Check Python ≥ 3.12, `uv` installed (offer one-line installer command), `claude` on PATH.
    - `git submodule update --init --recursive`.
    - `uv sync`.
    - Generate `JWT_SECRET=$(openssl rand -hex 32)` and write to `.env` if absent (copying from `.env.example` first).
    - `mkdir -p data/logs`.
    - `python -m ccr init-db`.
    - Print next steps (Telegram bot token, tunnel setup, console).
  - `.gitmodules` — single submodule `templates/ → <dev-stack-agents repo URL>` on `main`.
  - `README.md`:
    - Project summary
    - "Run locally in 60 seconds" — exact commands
    - Console walkthrough (how to invite a friend)
    - Tunnel setup (Tailscale Funnel; alternatives noted)
    - Bot command reference + console command reference
    - Troubleshooting
    - Security model section: trust boundaries, what 30-min tokens protect against, why CDN xterm.js
    - License (confirm with project owner)
Out of scope:
  - docker-compose; cloud deployment; CI release pipeline.
Acceptance:
  - [ ] On a clean VM with Python 3.12 + uv + Claude Code installed: `git clone --recurse-submodules ... && cd ... && ./install.sh` completes without errors in under 2 min.
  - [ ] `pytest` (full suite) passes.
  - [ ] `mypy src` passes.
  - [ ] `ruff check src tests` passes.
  - [ ] README "60 seconds" section, when followed verbatim, results in a working bot reachable via Telegram with the bootstrap user paired.
  - [ ] README "invite a friend" walkthrough, when followed, results in a second paired user receiving session output in their chat.
Depends on: CCR-013, CCR-014, CCR-015, CCR-016
Notes:
  Split from Phase 14: this ticket owns the sysops pieces (`install.sh`, `.gitmodules`, `README.md`) plus the final repo-wide test/lint/type pass. The python-developer doctor work is CCR-016. `install.sh` uses `set -euo pipefail` and idempotent steps. The "60 seconds" README walkthrough must use these exact commands per plan §8 Phase 14 task 4: `git clone --recurse-submodules`, `cd claude-code-remote`, `./install.sh`, edit `.env` (TELEGRAM_BOT_TOKEN, PUBLIC_URL), `python -m ccr serve`, send `/start` in Telegram, then `python -m ccr pair approve <code>` (or `python -m ccr console`). Include the "invite a friend" walkthrough verbatim from plan task 5. README author should confirm license with project owner before merging.

### Review log
---

## CCR-019: `/sessions` command + orphan reconciliation on startup [todo]
Phase: 6 (post-CCR-018 UX polish)
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/session.py` — new `cmd_sessions` handler registered on `session_router`. Query `SELECT id, started_at, status, started_by_tg_user_id, first_prompt, ended_at, exit_reason FROM sessions ORDER BY started_at DESC LIMIT 20`. One line per session: `<id8> · <status> · <started_at iso> · <first_prompt truncated to 60 chars or "—"> · <by @username or tg_user_id>`. HTML mode like the rest of the bot. Empty DB replies a stable fixed string (e.g. `"(no sessions)"`). Cap message body to ≤ 3500 chars (reuse chunking from `formatting.py` if needed).
  - `src/ccr/claude/manager.py` — new `async reconcile_orphans(self) -> int` method (or equivalent helper) that updates any DB row with `status='running'` and no live in-memory session to `status='crashed'`, `exit_reason='bot restart'`, `ended_at=now()`. Returns count reconciled. Logs one structured line per row reconciled. Idempotent — second call must be a no-op.
  - `src/ccr/server.py` — call `manager.reconcile_orphans()` once during `serve()` startup, before the dispatcher and broadcast tasks start.
  - `tests/test_bot_session.py` — extend: `/sessions` on empty DB returns `"(no sessions)"` (or whatever stable string is picked); `/sessions` with 3 seeded rows returns 3 lines in `started_at`-desc order containing the id8 prefixes; long-prompt truncation to 60 chars; status field passed through verbatim.
  - `tests/test_orphan_reconcile.py` (new) — seed a row with `status='running'`, instantiate a fresh `SessionManager`, call `reconcile_orphans()`, assert the row is now `status='crashed'`, `exit_reason='bot restart'`, `ended_at` populated; second call returns 0 / no-op. (PM note: test file location is the developer's call — folding into `tests/test_session_manager.py` is also acceptable.)
Out of scope:
  - Resuming sessions (handled in CCR-020).
  - Deleting old sessions.
  - Pagination beyond `LIMIT 20`.
Acceptance:
  - [ ] `/sessions` on a fresh DB replies with the chosen stable empty-state string (e.g. `"(no sessions)"`).
  - [ ] After 3 seeded sessions across mixed statuses, `/sessions` reply contains all three id8 prefixes in `started_at`-desc order.
  - [ ] After a simulated bot restart (insert a `running` row, instantiate a fresh manager, call `reconcile_orphans`, then `/sessions`), the row appears as `crashed` with `exit_reason='bot restart'`.
  - [ ] `pytest tests/test_bot_session.py tests/test_orphan_reconcile.py` (or wherever the orphan test lives) passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
Depends on: CCR-018
Notes:
  Phase n/a in the plan — this is post-CCR-018 UX polish in the same Phase 6 cluster, same precedent as CCR-018 sitting in chat-bot despite touching `claude/manager.py`. The orphan problem: on bot crash/kill the child `claude` subprocess dies with the parent but the `Session` row stays `status='running'` forever because `_db_finalize_session` never runs. After restart, the DB lies. `/sessions` listing would surface that lie unless reconcile fixes it on startup.
  Reply formatting: HTML escape `first_prompt` and `tg_username` before interpolation (consistent with CCR-008/CCR-018 conventions in `session.py` and `formatting.py`). The `db_factory` is already on workflow data — handler just opens a short-lived session.
  Reconcile criteria: `Session.status == 'running' AND id != current_session_id_or_None`. On a fresh-start manager with `_proc=None`, there is no live session to exclude — the simple `WHERE status='running'` predicate is sufficient. Wrap in a single transaction; commit eagerly. Log `session_manager.orphan_reconciled` per row with `session_id`, `started_at`.
  Recommended ordering: ship before CCR-020 so `/continue` inherits a clean DB story for "what's the most recent prior session" — without it, `/continue` would have to filter out potentially-still-running rows that aren't actually running.

### Review log
---

## CCR-020: `/continue` command [todo]
Phase: 6 (post-CCR-018 UX polish)
Feature: chat-bot
Files:
  - `src/ccr/claude/process.py` — `ClaudeProcess.__init__` gains an optional `resume: bool | str = False` parameter. `start()` appends `--continue` (bool path) or `--resume <id>` (str path) before `--input-format` in the argv it builds.
  - `src/ccr/claude/manager.py` — new `async continue_session(self, *, started_by_tg_user_id: int | None) -> uuid.UUID` method. Stops any current session, picks the most recent finished `Session` row (per outcome 2: pull its `claude_session_id`), spawns a new `ClaudeProcess` with the resume flag/id, inserts a fresh `Session` row (mints a new local `uuid.uuid4()`; for outcome 2, also stashes the resumed `claude_session_id` on the new row so future `/continue` chains keep working).
  - `src/ccr/db/models.py` (outcome 2 only) — add `claude_session_id: Mapped[str | None]` to `Session`.
  - `alembic/versions/0002_session_claude_id.py` (outcome 2 only) — new migration adding the column.
  - `src/ccr/claude/manager.py` `_consume_events` (outcome 2 only) — when `SystemInit` arrives, extract Claude's internal session id from the event's raw payload and persist it on the current `Session` row. `SystemInit` already preserves unknown fields via `extra="allow"`; the developer confirms the field name from the probe step.
  - `src/ccr/bot/handlers/session.py` — new `cmd_continue` handler:
    - If a session is already running → `"Session already running. /stop first or /clear to start fresh."`
    - If there is no prior session in the DB → `"No prior session to continue."`
    - Otherwise call `manager.continue_session(...)` and reply `f"Session {id8} resumed (pid {pid})."` (mirrors the `(pid N)` style from CCR-018).
  - `tests/test_session_manager.py` — extend using the fake claude script: fake reads its argv and emits a marker so the test can assert `--continue` (or `--resume <id>`) was passed through. (Outcome 2) extend to assert `claude_session_id` is captured from the `system.init` event and persisted on the row.
  - `tests/test_bot_continue.py` (new) — `/continue` happy path; `/continue` with a running session returns the canned reply; `/continue` with no prior session returns the canned reply.
Out of scope:
  - Picking which prior session to continue when there are several (default: most recent; explicit `/continue <id>` is a follow-up ticket).
  - A `/resume <id>` UI on the `/sessions` listing.
  - JSONL-replay fallback for outcome 3 (escalate via `BLOCKED` instead).
Acceptance:
  - [ ] Step 0 probe outcome documented in the developer's work summary (which of `--continue` / `--resume <id>` / neither works in `-p stream-json` mode).
  - [ ] `/continue` after a finished session shares conversation context with the prior session (manual smoke: `/new` → "remember the word banana" → `/stop` → `/continue` → "what word did I ask you to remember" should answer "banana").
  - [ ] `/continue` with no prior session returns `"No prior session to continue."`.
  - [ ] `/continue` while a session is running returns `"Session already running. /stop first or /clear to start fresh."`.
  - [ ] (Outcome 2 only) `claude_session_id` column exists, populated on `system.init`, used by the next `/continue`.
  - [ ] `pytest tests/test_bot_continue.py tests/test_session_manager.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
Depends on: CCR-018, CCR-019
Notes:
  Phase n/a in the plan — post-CCR-018 UX polish in the same Phase 6 cluster, same chat-bot feature precedent as CCR-018.
  **Step 0 probe — required before writing any code.** Confirm whether `claude -p --input-format=stream-json --output-format=stream-json --verbose --continue` (and / or `--resume <id>`) works. Three outcomes:
    1. `--continue` works in `-p` stream-json mode → smallest path: pass the flag, fresh subprocess, Claude figures out which conversation to resume from `~/.claude/`. ~1 day.
    2. `--resume <claude-session-id>` works but `--continue` does not → capture Claude's internal session id from the `SystemInit` event, persist on the `Session` row in a new column `claude_session_id TEXT`, add an Alembic migration, pass `--resume <id>` when continuing. ~1.5 days.
    3. Neither works in `-p` mode → return `BLOCKED — Claude Code -p mode does not support continuation; awaiting upstream`. Do NOT build a JSONL-replay fallback (lossy, brittle; team-lead/architect should weigh in before that path is taken).
  The developer must document which outcome they hit and which path they took in the work summary.
  This is a load-bearing change to the SessionManager surface (a second public lifecycle method). Mode 1A note for team-lead: worth dispatching the architect first to settle the resume-flag plumbing in `ClaudeProcess` and the `claude_session_id` capture path, especially if outcome 2 is hit. Architect should be told that the probe is the developer's job, not the architect's — the architect designs *both* paths and the developer picks at implementation time.
  CCR-019 is a hard dep so `/continue` inherits a clean orphan story: without orphan reconcile, "most recent prior session" could pick a row that says `running` but isn't. Manual smoke acceptance is documented but unticked — we cannot automate a real Claude binary in CI.

### Review log
---

## CCR-021: Probe Claude `-p` permission wire format and reconcile schema [todo]
Phase: 8 (post-CCR-009 wire-format reconciliation)
Feature: claude-runtime
Files:
  - `src/ccr/claude/process.py` — fix the misattributed `TODO(CCR-019)` comment at the `send_permission_response` site (delete or repoint to CCR-021); reconcile the `send_permission_response` payload shape against the wire format observed in the Step 0 probe (currently a best-effort `{"type": "permission_response", "request_id", "choice"}` guess).
  - `src/ccr/claude/events.py` — adjust `PermissionRequest` field names / types if the probe shows divergence (current best-effort fields: `request_id`, `tool_name`, `input`, `options`).
  - `tests/fakes/fake_claude.py` — update permission-request fixture lines to mirror the real wire format if it differs from today's guess.
  - `tests/test_claude_process.py` and / or `tests/test_session_manager.py` — keep schema tests in sync with whatever the probe establishes; existing CCR-009 permission-gating tests must continue to pass.
Out of scope:
  - Building a full `python -m ccr serve` end-to-end harness — that's separate web/wiring work.
  - MCP `--permission-prompt-tool` integration if Claude turns out to gate permissions via that channel — escalate as a follow-up ticket rather than expand scope here.
  - Changing the bot-side button UX or the broadcast pause/buffer logic from CCR-009 — those layers are correct and out of scope here.
Acceptance:
  - [ ] Step 0 probe outcome documented in the developer's work summary: which of the three outcomes was hit (a) emits matching `permission_request` → minor field renames at most; (b) emits a different shape → reconcile schema and fixtures; (c) emits nothing / handled out-of-band → return BLOCKED with notes for follow-up.
  - [ ] The `TODO(CCR-019)` comment in `src/ccr/claude/process.py` (currently around line 152, at the `send_permission_response` site) is removed or repointed to CCR-021.
  - [ ] If the probe shows the schema diverged: `PermissionRequest` in `events.py` and the `send_permission_response` payload in `process.py` are updated to match the observed wire format; `tests/fakes/fake_claude.py` permission fixtures are aligned; `tests/test_claude_process.py` / `tests/test_session_manager.py` still pass.
  - [ ] If the probe hits outcome (c): ticket returns `BLOCKED — claude -p does not emit permission events on stdout; alternative mechanism (MCP / different flag / awaiting upstream) needs design`, mirroring CCR-020's outcome-3 escape hatch.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
Depends on: CCR-009
Notes:
  Phase n/a in the plan — this is post-CCR-009 upstream-validation work, same chat-bot/claude-runtime cluster precedent as CCR-019 / CCR-020 sitting under "post-CCR-NNN" qualifiers. Filed under `claude-runtime` because the wire format is owned there; the bot just consumes whatever schema this ticket settles on.
  **Step 0 probe — required before writing any code.** Mirror the CCR-020 pattern: run `claude -p --input-format=stream-json --output-format=stream-json --verbose` by hand with a prompt that requires Bash or Edit permission (something like "run `ls -la`" or "edit foo.txt"), capture the actual stdout JSONL, and document what you saw. Three documented outcomes:
    1. Emits a JSONL line with `type: "permission_request"` and the fields we guessed — minor field renames at most.
    2. Emits a different shape (different `type` discriminator, different field names, nested under `tool_use`, etc.) — reconcile `PermissionRequest`, `send_permission_response`, and the fake-claude fixtures to match, keep CCR-009's gating tests green.
    3. Emits nothing on stdout for permission gating (auto-denies, uses MCP `--permission-prompt-tool`, prompts on stderr, or some other channel) — return BLOCKED with notes; do NOT improvise a fallback before team-lead/architect weighs in.
  Why this exists: CCR-009's bot-side code is correct and tested against `tests/fakes/fake_claude.py`, but the fake's permission fixture and our `PermissionRequest` / `send_permission_response` shapes are best-effort guesses against the real claude binary. The smoke gap surfaced when a user tried to trigger a real permission prompt and saw nothing reach Telegram — the inline-button plumbing has never been exercised against a real claude session.
  The misattributed TODO at `src/ccr/claude/process.py:152` reads `TODO(CCR-019): confirm permission_response wire format end-to-end against a live claude session`; CCR-019 (line 602 in TICKETS.md) is `/sessions` command + orphan reconciliation and does not own this work. Cleaning that comment up is part of acceptance.
  Mode 1A note for team-lead: skip the architect — small, additive, validation-driven; scope is "probe + reconcile schema if needed". If the probe hits outcome (c) the ticket exits via BLOCKED rather than expanding scope.

### Review log
