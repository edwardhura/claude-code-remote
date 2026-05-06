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

## CCR-027: Plan-mode UX (deferred) [todo]
Phase: n/a (deferred — post-MCP UX)
Feature: chat-bot
Files:
  - `src/ccr/claude/process.py` — argv builder gains a `permission_mode: str | None = None` parameter; when set, appends `--permission-mode <value>` (initial value of interest: `"plan"`).
  - `src/ccr/claude/manager.py` — new `async start_in_plan_mode(prompt: str, started_by_tg_user_id: int | None) -> uuid.UUID` (or extend `new_session` with a `mode=` kwarg — developer's call) that spawns a fresh `ClaudeProcess` with `permission_mode="plan"`, captures the produced plan from the event stream, and broadcasts it to chat. On user acknowledgment, stops the plan-mode session and starts a fresh `default`-mode session with the same prompt.
  - `src/ccr/bot/handlers/session.py` — new `cmd_plan` handler: `/plan <prompt>` → kick off a plan-mode session; reply with the plan once available; render an "Approve and continue" inline button (or `/approve` command — developer's call) that triggers the default-mode handoff. `/plan` with no argument → usage hint.
  - `src/ccr/bot/formatting.py` — recognise the plan-mode `result` event (or whatever JSONL shape claude emits in `--permission-mode=plan`) and render the plan as a chat message with the approve/cancel buttons.
  - `tests/test_bot_plan_mode.py` (new) — `/plan "<prompt>"` → manager called with `permission_mode="plan"` and the prompt; the plan reply is rendered with the approve button; tapping approve → manager stops plan-mode and starts default-mode with the same prompt; tapping cancel (or no tap within timeout) → no default-mode session is started.
  - `tests/fakes/fake_claude.py` — fixture for synthetic plan-mode JSONL output.
  - Manual smoke (documented but unticked): real claude session with `--permission-mode=plan` surfaces a plan, user approves, default-mode session runs.
Out of scope:
  - Editing the plan from chat (approve-or-cancel only — no inline plan edits).
  - Plan-mode UX in the web viewer.
  - Persisting plans across restarts.
  - Other `--permission-mode` values (`acceptEdits`, `bypassPermissions`) — separate follow-ups if requested.
Acceptance:
  - [ ] `/plan <prompt>` starts a fresh claude session with `--permission-mode=plan` and the supplied prompt; argv is verified by a test inspecting `ClaudeProcess` argv.
  - [ ] The produced plan is rendered to all paired chats with an "Approve and continue" inline button (or developer's-call equivalent).
  - [ ] Approving the plan stops the plan-mode session and starts a fresh default-mode session with the original prompt.
  - [ ] Cancelling (or timeout, if architect designs one) does NOT start a default-mode session.
  - [ ] `/plan` with no argument returns a usage hint without starting a session.
  - [ ] `pytest tests/test_bot_plan_mode.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
  - [ ] Manual smoke (unticked, not blocking review per CCR-020/CCR-021 precedent): real claude session with `--permission-mode=plan`, plan rendered in Telegram, user approves, default-mode session runs.
Depends on: CCR-025, CCR-026
Notes:
  Phase n/a in the plan — post-CCR-025/CCR-026 UX work in the chat-bot cluster.
  **Priority: deferred — pick up after the chat bot is stable.** This ticket is filed now to capture scope but should NOT be picked by `/implement-ticket` ahead of higher-value work. The chat-bot-first-iteration push prioritizes CCR-024 → CCR-025 → CCR-022 → CCR-023 ahead of this; CCR-026 lands before this so the prompt/answer surface is settled.
  Why depends on CCR-026: the approve-and-continue acknowledgment is a generic "ask the user a structured question and get a typed-or-button reply" interaction — exactly the surface CCR-026 builds. If CCR-026's scope settles a generic prompt mechanism (e.g. a per-message inline-button pattern with a Future-keyed callback), CCR-027 reuses it rather than reinventing.
  Open questions for team-lead Mode 1A (probably needs the architect): how to capture "the plan" from the JSONL stream (claude in `--permission-mode=plan` emits a final `result` event whose content is the plan; the format is not yet probed); how to time out an unanswered plan; whether the default-mode handoff resumes the plan-mode session via `--continue` or starts genuinely fresh; how to surface progress between "plan produced" and "user approved".
  Step-0 probe likely required: run `claude -p --input-format=stream-json --output-format=stream-json --verbose --permission-mode=plan` with a representative prompt and capture the JSONL stream so the architect knows what shape the plan arrives in.

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
  - 2026-05-01 project-manager: reordered — chat-bot iteration prioritized
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
  - 2026-05-01 project-manager: reordered — chat-bot iteration prioritized
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
  - 2026-05-01 project-manager: reordered — chat-bot iteration prioritized
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
  - 2026-05-01 project-manager: reordered — chat-bot iteration prioritized
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
  - 2026-05-01 project-manager: reordered — chat-bot iteration prioritized
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
  - 2026-05-01 project-manager: reordered — chat-bot iteration prioritized
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
  - 2026-05-01 project-manager: reordered — chat-bot iteration prioritized
---

## CCR-021: Probe Claude `-p` permission wire format and reconcile schema [blocked]
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
  - 2026-05-01 main: Step 0 probe executed against claude v2.1.123 from a fresh /tmp cwd; outcome (c) confirmed — `claude -p` emits ZERO `permission_request` events on stdout (types seen: system, rate_limit_event, assistant, user, result; `permission_denials: []`; init shows `permissionMode: "default"`; stderr empty; exit 0). Built-in permission gating in non-interactive mode is wired via `--permission-prompt-tool <mcp_tool>`, not via stdin/stdout JSONL. Probe transcript: tmp/ccr-021-probe-1777590778.jsonl; summary: tmp/ccr-021-probe-1777590778.summary.txt.
  - 2026-05-01 main: marked [blocked] — schema reconciliation work in this ticket is moot (no schema to reconcile to); supersedes filed as CCR-024 (remove dead permission code from CCR-009) and CCR-025 (MCP permission-prompt-tool integration). This ticket stays open as a tracking pin until CCR-025 lands; revisit if upstream Claude `-p` ever exposes a stdout permission channel.
---

## CCR-040: `/agents` reply mirrors Claude Code CLI library view [todo]
Phase: n/a (post-CCR-022 polish)
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/passthrough.py` — extend `_render_agents_reply` and `_list_library_agents` (around lines 79 and 93). Per line in Project agents and Built-in agents: `<name> · <model>`.
    - Project agents: parse `model:` from each `.claude/agents/*.md` YAML frontmatter. If absent → render `inherit` (matches Claude Code CLI fallback).
    - Built-in agents: built-ins are NOT on disk. Use a constant map `BUILTIN_AGENTS = {"Explore": "haiku", "Plan": "inherit", "general-purpose": "inherit", "statusline-setup": "sonnet", "claude-code-guide": "haiku"}`. Add a comment noting the Claude Code version this was sourced from so we know to revisit.
    - Final section order: Running (existing) → Project agents → Built-in agents.
  - `tests/test_bot_passthrough.py` — extend `/agents` tests:
    - Project agent with `model: opus` in frontmatter renders `<name> · opus`.
    - Project agent without `model:` field renders `<name> · inherit`.
    - Built-in agents section is present in the reply, in the order specified by `BUILTIN_AGENTS`.
    - Final section order in the rendered reply is Running → Project agents → Built-in agents.
    - HTML escaping of agent names containing `<`, `>`, `&` (regression — CCR-022 pattern).
  - `tests/fakes/` — no changes expected; the existing project-agent fixture in `tests/` is the source for the project-section test. Add fixture frontmatter as needed.
Out of scope:
  - Discovering built-in agents from a Claude Code introspection API (the CLI does not expose one) — the constant map is the agreed substitute.
  - Editing or creating agent definitions from Telegram (read-only listing).
  - Per-subagent model configuration in any format other than YAML frontmatter.
  - Cross-referencing the running subagents (CCR-022 surface) with model info — out of scope, model info comes from project files only.
Acceptance:
  - [ ] Each project-agent line renders as `<name> · <model>`; missing `model:` frontmatter renders `inherit`.
  - [ ] A `Built-in agents` section is present, listing every entry in the constant `BUILTIN_AGENTS` map as `<name> · <model>`.
  - [ ] `BUILTIN_AGENTS` has a comment noting the Claude Code version it was sourced from (so future maintainers know to revisit).
  - [ ] Final section order in the `/agents` reply is Running → Project agents → Built-in agents.
  - [ ] HTML escaping is preserved for agent names containing `<`, `>`, `&` (regression vs. CCR-022).
  - [ ] `pytest tests/test_bot_passthrough.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
Depends on: CCR-022
Notes:
  Phase n/a — cosmetic polish of CCR-022's `/agents` output to mirror the Claude Code CLI library view. Source: `_render_agents_reply` and `_list_library_agents` in `src/ccr/bot/handlers/passthrough.py:79,93`.
  Built-ins are NOT on disk; the constant map is the only source. Treat the version comment as documentation debt — a future ticket may want to refresh the list when Claude Code ships new built-ins.
  Mode 1A note for team-lead: skip the architect — additive UX (constant map + frontmatter parser + section ordering). No new abstraction, no schema, no cross-cutting design call.

### Review log
---

## CCR-041: `/continue` UNIQUE-constraint regression — drop unique flag on `claude_session_id`, programmatic duplicate guard [todo]
Phase: n/a (post-CCR-036 bugfix)
Feature: claude-runtime
Files:
  - `alembic/versions/` — new migration that drops the unique-ness from the partial index `ix_sessions_claude_session_id_not_null` (either by dropping the index and recreating it as a non-unique partial index for query speed, or by replacing the unique index with a non-unique equivalent — developer's call). Original index added in CCR-036's migration `alembic/versions/0003_add_sessions_claude_session_id.py`.
  - `src/ccr/db/models.py` — flip `unique=True` → `unique=False` on the partial index declaration around lines 118-126 (the `Index(...)` call backing `ix_sessions_claude_session_id_not_null`). Keep the partial-index `WHERE claude_session_id IS NOT NULL` clause so the index stays useful for lookups.
  - `src/ccr/claude/import_session.py` — add a programmatic SELECT-before-INSERT duplicate check in `import_claude_session` (around lines 175-232): look up by `claude_session_id`; raise `DuplicateClaudeSessionError(claude_session_id)` if a row already exists. Keep the existing IntegrityError except branch as a defensive fallback or remove it once the pre-check is in place — developer's call.
  - `src/ccr/claude/manager.py` — no scoped behaviour change required; the existing `_update_claude_session_id` write at lines 871-876 will simply succeed instead of raising. Confirm the lookup logic in `_db_lookup_resumable_claude_session_id` (lines 1271-1284, "started_at desc → first non-null") continues to work unchanged when multiple rows share the same `claude_session_id`.
  - `tests/test_db_models.py` — remove or rewrite `test_session_partial_unique_index_on_claude_session_id` (around line 251); the constraint no longer enforces uniqueness so the existing assertion is obsolete. Replace with a test that asserts the partial index is still present (non-unique) if a query-plan/index-presence check is desired.
  - `tests/test_claude_import_session.py` (or wherever `import_claude_session` is currently tested — locate by grepping for `import_claude_session`) — keep the duplicate-import test green via the new programmatic check; assert `DuplicateClaudeSessionError` is raised on the second call rather than relying on `IntegrityError`.
  - `tests/test_session_manager.py` — add a regression test for the `/continue` chain: spawn a fresh session, end it, call `continue_session`, drive the `SystemInit` event through `_consume_events`, assert no `claude_session_id_update_failed` log entry / no `IntegrityError` is raised AND the new row's `claude_session_id` is set to the resumed value (multiple rows now share the same `claude_session_id`).
Out of scope:
  - Changing the lookup ordering logic in `_db_lookup_resumable_claude_session_id` — already correct.
  - Reworking `import_claude_session` semantics beyond moving the duplicate guard from the DB index to a programmatic check.
  - Backfilling existing NULL `claude_session_id` rows from prior failed resumes.
Acceptance:
  - [ ] Fresh session followed by `/continue` no-prefix produces no `claude_session_id_update_failed` log entry; the resumed row's `claude_session_id` equals the prior row's value.
  - [ ] `/continue <8-hex-prefix>` against the resumed row succeeds (does not raise `NoPriorSessionError`).
  - [ ] `import_claude_session` still raises `DuplicateClaudeSessionError` when called twice for the same `claude_session_id` (programmatic guard intact).
  - [ ] Multiple rows can share the same `claude_session_id` without `IntegrityError` (regression test in `tests/test_session_manager.py`).
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
  - [ ] `ruff check src tests` passes.
  - [ ] `ruff format --check` passes.
  - [ ] `mypy src` passes.
Depends on: CCR-036
Notes:
  Bug report: when a user runs `/continue` (no prefix), the bot replies "Session 73725407 resumed (pid 30666)" — the session works — but the logs contain a fire-and-forget `IntegrityError`:

  ```
  session_manager.claude_session_id_update_failed
  claude_session_id=7aa3069d-8b1f-4b98-b6f3-3c6fa1e3a0f7
  session_id=73725407-1cea-40f7-95cc-1469c2be623a
  sqlite3.IntegrityError: UNIQUE constraint failed: sessions.claude_session_id
  [SQL: UPDATE sessions SET claude_session_id=? WHERE sessions.id = ?]
  ```

  Future implementers can grep on `claude_session_id_update_failed` or `UNIQUE constraint failed: sessions.claude_session_id` to recognise the same bug.

  Root cause: `continue_session()` in `src/ccr/claude/manager.py:425-499` mints a NEW local `Session` row with a fresh `uuid4` and spawns claude with `--resume <prior_claude_session_id>`. When claude resumes, its `SystemInit` emits the *same* `session_id` as the prior conversation (because `--resume` continues that conversation). `_consume_events` at `src/ccr/claude/manager.py:871-876` then fires `_update_claude_session_id(new_local_id, event.session_id)` which tries to UPDATE the new row's `claude_session_id` to the prior row's value — colliding with the partial UNIQUE index `ix_sessions_claude_session_id_not_null` declared in `src/ccr/db/models.py:118-126` and added in CCR-036's migration. The unique index was added to prevent double-import of the same Claude session via `import_claude_session` (the `session save` console command); it did not anticipate `--resume` chains, which semantically *do* produce multiple rows pointing at the same Claude conversation.

  Visible side effects of the current bug:
  - The bot reply is correct ("Session … resumed"); the session itself works because lock + spawn + status flip happen before the failing DB write.
  - The `_update_claude_session_id` write is wrapped in `try/except` with `log.exception` (fire-and-forget) so it does not crash the session.
  - The new resumed row keeps `claude_session_id = NULL` instead of the prior row's id.
  - `/continue` no-prefix still works (`_db_lookup_resumable_claude_session_id` walks `started_at desc` and picks the first non-null — finds the prior row, resumes correctly).
  - `/continue 73725407` (the new resumed row's prefix) **fails** with `NoPriorSessionError` because the new row's `claude_session_id IS NULL` — see `src/ccr/claude/manager.py:1277-1284`.
  - Every subsequent `/continue` repeats the IntegrityError in the logs.

  Suggested direction (not part of acceptance — architect/developer free to choose option 2 if a strong reason emerges): **Option 1 — drop the unique constraint, add programmatic duplicate check.** Rationale: resume chains semantically *do* produce multiple rows pointing at the same Claude conversation; the lookup logic (`started_at desc → first non-null`) already handles this correctly. The "don't import the same Claude session twice" guard belongs at the `import_claude_session` callsite (CCR-036's `session save` console command), not as a DB-wide invariant. Move it to a SELECT-before-INSERT in `src/ccr/claude/import_session.py` and let the resume-chain rows share `claude_session_id` freely.

  Mode 1A note for team-lead: skip the architect — small, additive, single-feature; scope is "drop unique flag + programmatic duplicate check + regression test". No new abstraction, no schema beyond the index flip, no cross-cutting design call.

### Review log
  - 2026-05-06 project-manager: filed from /continue UNIQUE-constraint bug report
---
