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

## CCR-035: `format_user_datetime` helper + bot-wide datetime standardisation [todo]
Phase: n/a (post-CCR-034 UX polish)
Feature: chat-bot
Files:
  - `src/ccr/utils.py` (new — or `src/ccr/bot/datetime.py`; developer's call) — `format_user_datetime(dt: datetime, user: PairedUser | None, mode: Literal["full", "short", "time"]) -> str`. All output 24h. Modes:
    - `full` → `HH:MM DD-MM-YYYY`
    - `short` → `HH:MM DD-MM`
    - `time`  → `HH:MM`
    Applies `user.timezone` (loaded via `zoneinfo.ZoneInfo`) when set; falls back to UTC when `user is None` or `user.timezone is None` or the stored zone fails to resolve (defensive — log a warning, fall back to UTC, do not raise).
  - `src/ccr/bot/` — sweep all bot-side timestamp rendering call sites and replace inline `strftime` / ad-hoc formatting with calls to `format_user_datetime`. Likely sites (developer must confirm via grep): wherever `started_at` / `ended_at` / reset-time strings are interpolated today. Web viewer (`src/ccr/web/`) is OUT of scope.
  - `.claude/agents/python-developer.md` — append a durable rule under Conventions / project rules: **"When rendering datetimes in bot replies (`src/ccr/bot/`), use `ccr.utils.format_user_datetime` (or wherever the helper lands). Never call `strftime` inline in `src/ccr/bot/`."** Phrase as a project rule so future tickets inherit it.
  - `tests/test_utils_datetime.py` (new) — unit tests: each mode returns the expected format; user with `Europe/Berlin` shifts a UTC instant correctly; `user=None` falls back to UTC; `user.timezone="Not/Real"` falls back to UTC and logs a warning; DST-edge sanity check.
  - `tests/test_bot_*` — update any existing test that asserts an inline timestamp format to match the new helper output (mostly `tests/test_bot_session.py` and any other passthrough/usage test that pins `started_at` rendering).
Out of scope:
  - Web-viewer datetime rendering (`src/ccr/web/`) — that has its own browser-side concerns; explicit out of scope per the user's note "Web viewer is out of scope."
  - Adding new modes beyond `full` / `short` / `time` — file follow-ups if needed.
  - Changing how `paired_users.timezone` is set — that's CCR-034.
  - Any non-bot caller (CLI, console, internal logging) — those keep ISO/UTC. The rule is bot-scoped.
Acceptance:
  - [ ] `format_user_datetime(dt, user, "full")` returns `HH:MM DD-MM-YYYY` (24h).
  - [ ] `format_user_datetime(dt, user, "short")` returns `HH:MM DD-MM`.
  - [ ] `format_user_datetime(dt, user, "time")` returns `HH:MM`.
  - [ ] When `user.timezone` is set to a valid IANA zone, the helper applies that zone (test: a UTC-noon datetime renders with `Europe/Berlin` offset of +1 or +2 depending on DST).
  - [ ] When `user is None` OR `user.timezone is None` OR the stored zone is unresolvable, the helper falls back to UTC and (for the unresolvable case) logs a warning via structlog.
  - [ ] `grep -rnE "strftime\(" src/ccr/bot/` returns no matches (every bot-side rendering goes through the helper).
  - [ ] `.claude/agents/python-developer.md` contains the new rule about using the helper.
  - [ ] `pytest tests/test_utils_datetime.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
Depends on: CCR-034
Notes:
  Phase n/a in the plan — post-CCR-034. The helper is the prerequisite for CCR-037 (sessions list display) and CCR-039 (`/usage` cosmetics). Filing as a standalone ticket so those two tickets can depend on a stable helper and don't each reinvent formatting.
  The grep canary (`strftime\(`) on `src/ccr/bot/` is the durable enforcement; the python-developer.md rule is the documentation half.
  Mode 1A note for team-lead: skip the architect — small, additive, single-file helper plus a sweep + agent-rule append. No new abstraction, no schema, no cross-cutting design call.
  Helper signature is suggested, not prescribed; developer can choose the exact module path and pattern (e.g. accept a `tg_user_id` instead of a `PairedUser`, looking up the row internally). Whichever shape lands, all bot call sites must use it consistently.

### Review log
---

## CCR-036: `claude_session_id` column, resume rework, `session save` CLI, sync skill [todo]
Phase: n/a (post-CCR-020 — restructures resume around Claude's session id)
Feature: claude-runtime
Files:
  - `alembic/versions/000X_add_sessions_claude_session_id.py` (new migration) — add nullable `claude_session_id TEXT` column to `sessions` plus a partial unique index where `claude_session_id IS NOT NULL` (prevents double-import of the same Claude session). Additive. Hand-written.
  - `src/ccr/db/models.py` — add `claude_session_id: Mapped[str | None]` field on `Session` with the partial unique index declared.
  - `src/ccr/claude/manager.py` — when consuming Claude's `system.init` event (visible at `src/ccr/claude/events.py:87,181`), extract its `session_id` and persist to `sessions.claude_session_id` for the current row. Drop the existing "find latest finished row by our UUID" path in `continue_session` / `_db_lookup_most_recent_finished` and replace with a lookup by `claude_session_id`. `claude --resume <claude_session_id>` is the only resume path; rows with `claude_session_id IS NULL` are not resumable (raise `NoPriorSessionError` or a more specific `NotResumableError` — developer's call).
  - `src/ccr/claude/process.py` — `resume` argv handling: when `resume` is a string, pass it as `--resume <claude_session_id>` rather than the bare `--continue` flag used today.
  - `src/ccr/cli.py` — new subcommand group `session` with first member `session save <claude_session_id>`. Argparse dispatch under the existing `python -m ccr` entrypoint, mirroring the existing `pair` subcommand-group pattern (see `_add_pair_subcommands` and the `_cmd_pair_*` family at `src/ccr/cli.py:281-323`). Room for `session list`, `session rename` later (do not implement now unless trivially natural).
  - `src/ccr/console/app.py` — register `session save` in the interactive REPL the same way `pair` is registered today: add an entry to the `COMMANDS` dispatch dict (currently at `src/ccr/console/app.py:243-253`), extend `_STATIC_COMMAND_WORDS` (at `src/ccr/console/app.py:45-56`) with `session` and `save`, extend `_match_command`'s prefix-length sweep (currently `(2, 1)` at line 267) to also try a 2-token `session save` prefix, add a corresponding REPL handler (`_cmd_session_save`) following the `_cmd_pair_approve` shape, and extend the `help` output (`_cmd_help` at line 217) with a `session save <claude_session_id>` line.
  - `src/ccr/console/` (or wherever the import logic best lives) — both the one-shot CLI handler (`src/ccr/cli.py`) and the REPL handler (`src/ccr/console/app.py`) call into a shared async function (analogue of `ccr.auth.pairing.approve`) that reads Claude's local session file at `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` and auto-fills:
    - `claude_session_id` ← argv.
    - `started_at` ← first event timestamp.
    - First user prompt → seeds `name` (per CCR-037's auto-derive rule; if CCR-037 has not landed yet, write to a placeholder field or postpone the auto-fill — developer's call coordinated with CCR-037).
    - `status` = `stopped`.
    - `started_by_tg_user_id` = the `tg_user_id` from the row in `paired_users` with `is_owner = true`.
    Do NOT copy the JSONL into `data/logs/` — Claude owns its history; our JSONL is only created when a continuation streams.
  - `templates/ccr/skills/sync-claude-session-with-remote/SKILL.md` (new) — thin wrapper skill: detects the user's most recent local Claude session (or accepts an explicit id arg), calls `python -m ccr session save <id>`, surfaces the resulting row prefix and a reminder to `/sessions` from the bot. Lives under `templates/ccr/` (the CCR-specific subfolder of `templates/`); `templates/` is the boilerplate downstream projects copy into their own `.claude/`. **Do NOT place this under `.claude/skills/` — that path was wrong in the original draft.**
  - `tests/test_session_save_cli.py` (new) — end-to-end for both entrypoints: a hand-crafted Claude JSONL at a tmp path; (a) `python -m ccr session save <id>` populates a row with the correct fields; (b) `python -m ccr console --once "session save <id>"` populates a row with the same fields against the same DB; double-import of the same `claude_session_id` (whether via CLI or console) is rejected by the partial unique index (IntegrityError or a clean handler error).
  - `tests/test_console.py` — extend with `session save` REPL coverage (parity with the existing `pair approve` console tests): unknown-arg usage hint, success path, duplicate-id handling.
  - `tests/test_session_manager.py` — extend with `claude_session_id` extraction on `system.init` and the new resume-by-claude-id path; remove or update the prior "resume by our UUID prefix" tests as the path is dropped.
  - `tests/test_claude_process.py` — argv test: `resume="abc-123"` produces `--resume abc-123`.
Out of scope:
  - `session list` / `session rename` — leave the CLI group open for them but do not implement now.
  - Copying Claude's JSONL into `data/logs/` on import — explicitly NOT done; Claude owns its history.
  - Web-viewer surfaces of `claude_session_id` — the column is server-side; viewer-side display is a follow-up if needed.
  - Migrating existing `sessions` rows to backfill `claude_session_id` from old logs — operational wipe of throwaway test rows is acceptable per the user's note; the migration itself stays additive.
  - The standardised `/sessions` listing format that uses `claude_session_id` — that's CCR-037.
Acceptance:
  - [ ] Alembic migration adds nullable `claude_session_id` column + partial unique index where the value is non-null; `alembic downgrade base && alembic upgrade head` round-trips clean.
  - [ ] On a session started by CCR, `sessions.claude_session_id` is populated from the `system.init` event before the row is written / on the first event consumption (verified by a session-manager test driving fake-claude through a `system.init` line).
  - [ ] Two attempts to insert a `Session` row with the same non-null `claude_session_id` raise `IntegrityError` (partial unique index test).
  - [ ] `python -m ccr session save <claude_session_id>` reads `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`, creates a row with `claude_session_id` set, `started_at` from the first event, `status="stopped"`, `started_by_tg_user_id` = the owner's `tg_user_id`, and (per CCR-037 auto-derive rule) seeds `name` from the first user prompt.
  - [ ] `python -m ccr session save <claude_session_id>` does NOT copy the JSONL into `data/logs/`.
  - [ ] `session save <id>` is invokable both as a one-shot CLI subcommand (`python -m ccr session save <id>`) and as a command inside the interactive console (`python -m ccr console`, then `session save <id>` at the prompt), in parity with the existing `pair` subcommands. Both entrypoints call the same underlying import function and produce the same DB row.
  - [ ] Inside the REPL, `session save` with no argument prints a usage hint (e.g. `Usage: session save <claude_session_id>`) without raising — matches the `pair approve` no-arg behaviour at `src/ccr/console/app.py:122-124`.
  - [ ] The console `help` output lists `session save <claude_session_id>`; `_STATIC_COMMAND_WORDS` includes `session` and `save` so tab-completion offers them.
  - [ ] `/continue` resumes via `claude --resume <claude_session_id>` (verified by a `ClaudeProcess` argv test).
  - [ ] A `Session` row with `claude_session_id IS NULL` is not resumable; `/continue` against it returns a clear error.
  - [ ] `templates/ccr/skills/sync-claude-session-with-remote/SKILL.md` exists and documents the wrapper flow (detect most-recent Claude session OR accept explicit id; invoke `python -m ccr session save`; surface result + `/sessions` hint). The file lives under `templates/ccr/`, NOT under `.claude/skills/`.
  - [ ] `pytest tests/test_session_save_cli.py tests/test_session_manager.py tests/test_claude_process.py tests/test_console.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
Depends on: CCR-020
Notes:
  Phase n/a — post-CCR-020 restructure of resume semantics. The current `SessionManager.resume()` reuses our row's most recent finished session, but the JSONL Claude actually replays is its own (`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`). This couples resume to our row id (fragile) and disallows continuing sessions started outside the bot.
  **Architect candidate for team-lead Mode 1A.** Surface spans `src/ccr/claude/{manager,process,events}.py`, `src/ccr/cli.py`, `src/ccr/console/app.py`, a new console import path, a new migration, a new skill, and reworks an existing subsystem (resume). The architect should pick:
    - Where the import logic lives (a new `src/ccr/console/import_session.py`, a method on `SessionManager`, or a free function under `src/ccr/claude/`). The shared function must be callable from BOTH `src/ccr/cli.py` (one-shot) AND `src/ccr/console/app.py` (REPL) — same dual-entrypoint pattern that `ccr.auth.pairing.approve` already serves today.
    - Whether `claude_session_id` is captured on `system.init` and persisted there, or batched with the next DB write.
    - How `session save` discovers Claude's local session-file directory (probably `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` per the user's spec — but the encoded-cwd format needs probing).
    - Whether the auto-derived `name` seed in `session save` integrates with CCR-037 directly (CCR-037 owns the auto-derive rule) or stages a placeholder until CCR-037 lands. PM recommends sequencing this ticket BEFORE CCR-037 so the column exists; coordinate auto-derive scope at team-lead Mode 1A.
  Existing `sessions` rows are throwaway test data — fine to drop / wipe operationally. The migration itself remains additive (the wipe is operational).
  Why CCR-037 depends on this and not vice-versa: CCR-037 renders `claude_session_id` in the `/sessions` list. Without this column, CCR-037's listing format cannot be implemented as specified.
  **Dual-entrypoint reference.** The `pair` subcommand family is the worked example to mirror: one-shot at `src/ccr/cli.py:281-323` (`_add_pair_subcommands` + `_cmd_pair_*`), REPL at `src/ccr/console/app.py:121-183, 243-273` (`_cmd_pair_*` handlers + `COMMANDS` dict + `_match_command` 2-token prefix sweep). Both layers call the same pure async functions in `ccr.auth.pairing`. `session save` follows the same shape: shared import function + thin CLI wrapper + thin REPL wrapper.
  **`templates/` submodule status — risk to flag.** Per `CLAUDE.md` "Conventions", `templates/` is intended to be a git submodule pointing at `dev-stack-agents`, but the `.gitmodules` wiring is owned by CCR-017 (Phase 14, not yet landed). At ticket-pickup time the developer must verify whether `templates/` is currently a real submodule or a regular directory. If submodule, writing to `templates/ccr/skills/sync-claude-session-with-remote/SKILL.md` requires committing inside the submodule and bumping the parent-repo pointer — that may exceed this ticket's scope and should be flagged back to the team-lead rather than silently absorbed. If `templates/` is still a regular directory (likely current state — `.gitmodules` does not yet exist in the repo root), the file lands as a plain in-tree commit and there is no submodule coordination needed. Either way: **do not place the skill under `.claude/skills/`** — that was the original draft's mistake.

### Review log
---

## CCR-037: Session human name + standardised `/sessions` listing format [todo]
Phase: n/a (post-CCR-036 — finalised listing format)
Feature: chat-bot
Files:
  - `alembic/versions/000X_add_sessions_name.py` (new migration) — add nullable `name TEXT` column to `sessions`. Additive. Hand-written.
  - `src/ccr/db/models.py` — add `name: Mapped[str | None]` field on `Session`.
  - `src/ccr/claude/manager.py` — when the first user prompt of a session arrives, auto-fill `sessions.name` ONLY when `name IS NULL` (so a manual rename sticks). Truncate to ~40 chars (developer's call: at word boundary or hard cut + ellipsis). No `name_is_custom` flag — the auto-fill-only-when-null rule covers it.
  - `src/ccr/bot/handlers/session.py` — extend `cmd_sessions` so each row renders as:
    ```
    <claude_session_id> · <status> · <name> · HH:MM DD-MM · by @<username>
    ```
    where:
    - `<claude_session_id>` is the column from CCR-036; for legacy rows where it is NULL, fall back to our UUID prefix (today's behaviour).
    - `<status>` unchanged.
    - `<name>` from this ticket; if NULL, render a placeholder (e.g. `(unnamed)` — developer's call) or omit the field cleanly.
    - Datetime via `format_user_datetime(started_at, user, "short")` from CCR-035.
    - "by" rule: join on `paired_users.username` (already captured at pairing time) and render `by @<username>`. Fallback when username is missing: `by <tg_user_id>` (no first_name; user explicitly excluded that).
  - `src/ccr/bot/handlers/session.py` (or a sibling handler module — developer's call) — new `/rename` command for the manual rename path. Suggested shape: `/rename <session_id_prefix> <name>` writes to `sessions.name`. Alternative: an inline `Rename` button on each session row (developer's call between command vs. button — pick the cleaner UX). One path, not both.
  - `src/ccr/bot/handlers/passthrough.py` — update `_UNKNOWN_USAGE_HINT` to include `/rename` if a command path is chosen.
  - `tests/test_bot_session.py` — extend with: name auto-fill on first user prompt; auto-fill is suppressed when `name` already set; manual rename writes to the column; `/sessions` listing renders the new format with all five fields; `claude_session_id` fallback to UUID prefix for legacy NULL rows; missing-username falls back to `by <tg_user_id>`.
Out of scope:
  - Sourcing the rename target via anything other than session-id prefix or button-tap (no fuzzy match).
  - Renaming a session via web viewer.
  - Persisting `name_is_custom` — explicitly out per the user's "one column only" steer.
  - Backfilling `name` for legacy rows — leave NULL; the listing renders the placeholder.
  - Display name fallback via `paired_users` (the user excluded `first_name`).
Acceptance:
  - [ ] Alembic migration adds nullable `name` column to `sessions`; `alembic downgrade base && alembic upgrade head` round-trips clean.
  - [ ] On a fresh session, the first user prompt seeds `sessions.name` truncated to ~40 chars; subsequent prompts do NOT overwrite it.
  - [ ] A row with `name` already set is not auto-overwritten on the next session start (manual-rename-sticks rule).
  - [ ] Manual rename path (`/rename <prefix> <name>` or inline button — developer's call) writes the new value to `sessions.name`.
  - [ ] `/sessions` listing renders each row in the format `<claude_session_id> · <status> · <name> · HH:MM DD-MM · by @<username>`, using `format_user_datetime(..., "short")` from CCR-035.
  - [ ] Rows where `claude_session_id IS NULL` (legacy) fall back to our UUID prefix in that slot.
  - [ ] Rows where `paired_users.username` is missing fall back to `by <tg_user_id>` (no `first_name`).
  - [ ] All interpolated values are HTML-escaped (consistent with `formatting.py` conventions used in CCR-008/CCR-018).
  - [ ] `pytest tests/test_bot_session.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
Depends on: CCR-035, CCR-036
Notes:
  Phase n/a — post-CCR-036 finalised listing format. Combines findings 2 (`@username` rule), 3 (human name + manual rename), and 5b (final listing template) into a single ticket because they all converge on the same render path.
  The user's design steers to keep here:
    - Format: `by @<username>` with fallback `by <tg_user_id>`. **Do not include `first_name`.**
    - Auto-fill name only when `name IS NULL`; no separate `name_is_custom` flag.
    - Manual rename path: command OR button — developer's call between the two; one path, not both.
  Mode 1A note for team-lead: probably skip the architect — additive UX (one column, one migration, listing render extension, one new command/button). The only mildly load-bearing call is command vs. button for rename; developer's call inside the ticket scope.
  This ticket depends on CCR-035 (the `format_user_datetime` helper for the `short` mode) and CCR-036 (the `claude_session_id` column). Order matters; do not pick this up until both are landed.

### Review log
---

## CCR-038: `/answer` HTML parse-error fix + bot-wide static-string audit [todo]
Phase: n/a (bugfix — chat-bot UX, follow-up to CCR-026)
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/ask_user_question.py` — at line 64, replace the static `_USAGE_HINT = "Usage: /answer <8-char-id> <text>"` with HTML-escaped placeholders: `Usage: /answer &lt;8-char-id&gt; &lt;text&gt;`. Reuse the `html.escape` pattern already used in `src/ccr/bot/formatting.py:220`.
  - `src/ccr/bot/handlers/ask_user_question.py` — at the usage-hint reply site (around line 154 in `cmd_answer`), wrap the send with a scoped `try/except TelegramBadRequest` and log via structlog so a single bad string cannot take down the dispatcher. Keep the exception scope narrow — do NOT broaden to `Exception` or `TelegramAPIError`.
  - `src/ccr/bot/` — audit pass: grep for `msg.answer(...)` / `cb.answer(...)` / `bot.send_message(...)` strings in `src/ccr/bot/` containing raw `<...>` placeholders that aren't escaped. Fix any others found.
  - `tests/test_bot_ask_user_question.py` — regression test: `/answer` with no args returns the (now HTML-safe) usage hint and does NOT raise. Bonus: a unit test driving the `try/except` path with a simulated `TelegramBadRequest` proves the handler logs and returns cleanly instead of bubbling.
  - `tests/test_bot_*` — if the audit pass fixes additional sites, add focused regression tests for each.
Out of scope:
  - Switching the bot's default `parse_mode` away from HTML.
  - Moving every static string through a sanitiser helper — only the specific `<...>` placeholder cases.
  - Refactoring how `_USAGE_HINT`-style constants are defined module-wide.
Acceptance:
  - [ ] `/answer` with no args replies with the HTML-escaped usage hint (`Usage: /answer &lt;8-char-id&gt; &lt;text&gt;` rendered as `Usage: /answer <8-char-id> <text>` in Telegram) and does NOT raise `TelegramBadRequest`.
  - [ ] Reproducer no longer triggers: typing `/answer` with no args produces a clean reply, not a stack trace into `cmd_answer`.
  - [ ] The static-string audit found no further raw-`<...>` cases (or fixed all that it found — list them in the developer's work summary).
  - [ ] The defensive `try/except TelegramBadRequest` is scoped to the static-string send only; no broader exception catch.
  - [ ] `pytest tests/test_bot_ask_user_question.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
Depends on: CCR-026
Notes:
  Phase n/a — pure bugfix follow-up to CCR-026. Reproducer captured by user: `/answer` with no args triggers `TelegramBadRequest: Bad Request: can't parse entities: Unsupported start tag "8-char-id" at byte offset 15` because the bot's default `parse_mode` is HTML and `<8-char-id>` is parsed as a tag. Stack trace lands at `src/ccr/bot/handlers/ask_user_question.py:154`.
  Mode 1A note for team-lead: skip the architect — single-line fix + a small audit pass + a defensive try/except. The audit pass is what justifies a ticket vs. a one-line patch.

### Review log
---

## CCR-039: `/usage` cosmetics — humanise codes + helper-driven datetime [todo]
Phase: n/a (post-CCR-032 polish)
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/passthrough.py` — extend `_render_usage_reply` and helpers around line 228 with constant maps:
    - `RATE_LIMIT_WINDOW_LABELS = {"five_hour": "Five hour", ...}` — humanise `rate_limit_event.resets_at` window codes.
    - `OVERAGE_REASON_LABELS = {"group_zero_credit_limit": "Group zero credit limit", ...}` — humanise overage reason codes.
    Unknown code → fall back to a humanised form (title-case + spaces from the snake_case key) so a new code from Claude degrades gracefully.
  - `src/ccr/bot/handlers/passthrough.py` — replace the raw ISO timestamp on the "Resets at" line with `format_user_datetime(resets_at, user, "full")` from CCR-035. Keep the existing relative `(in {delta})` tail as-is.
  - `src/ccr/bot/handlers/passthrough.py` — light prettifying pass on overall block layout / wording (developer's discretion within the new constants).
  - `tests/test_bot_passthrough.py` — extend `/usage` tests:
    - Each known window code renders with its mapped label (one test per known code seeded today).
    - Each known overage reason renders with its mapped label.
    - An unknown code falls back to a title-cased humanised form (e.g. `"new_unknown_window"` → `"New unknown window"`).
    - "Resets at" timestamp renders via the helper in `full` mode (HH:MM DD-MM-YYYY); user with a non-UTC `paired_users.timezone` shifts it; user with no timezone preference falls back to UTC.
    - Regression: the relative `(in {delta})` tail is still present.
Out of scope:
  - Changing the underlying `RateLimitEvent` model or wire format.
  - Changing how `current_rate_limit_status()` is populated (CCR-032's substrate).
  - The web-viewer rendering of usage data.
  - Unifying overage reason labels with any future billing surface.
Acceptance:
  - [ ] `/usage` reply uses humanised labels for all window codes seeded in `RATE_LIMIT_WINDOW_LABELS` (no raw `five_hour` etc. visible).
  - [ ] `/usage` reply uses humanised labels for all overage reason codes seeded in `OVERAGE_REASON_LABELS` (no raw `group_zero_credit_limit` etc. visible).
  - [ ] Unknown codes degrade gracefully via a title-case + spaces fallback (verified by a test with a synthetic unknown code).
  - [ ] "Resets at" timestamp uses `format_user_datetime(..., "full")` from CCR-035; user-timezone shift verified by a test with a non-UTC `paired_users.timezone`.
  - [ ] The relative `(in {delta})` tail remains on the "Resets at" line (regression).
  - [ ] `grep -nE 'strftime\(' src/ccr/bot/handlers/passthrough.py` returns no matches (confirms no inline strftime regressed in).
  - [ ] `pytest tests/test_bot_passthrough.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
Depends on: CCR-032, CCR-035
Notes:
  Phase n/a — cosmetic polish of CCR-032's `/usage` output. Source: `_render_usage_reply` and helpers around `src/ccr/bot/handlers/passthrough.py:228`.
  Constant-maps pattern (vs. branching logic) keeps adding new codes a one-line change. The `title-case-fallback` rule is the safety net for unknown codes.
  Mode 1A note for team-lead: skip the architect — additive cosmetic ticket, scope is two constant maps + helper call + light layout polish.
  Hard depend on CCR-035 because the "Resets at" timestamp is one of the first sites switching to `format_user_datetime`.

### Review log
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
