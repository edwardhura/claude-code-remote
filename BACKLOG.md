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

## CCR-043: `/rename` rekeyed to `claude_session_id[:8]`; add `/rename current <name>` for active session [todo]
Phase: n/a (post-CCR-042 chat-bot UX follow-up; part of the claude_session_id-alignment slice)
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/session.py` — rework `cmd_rename` (around lines 385- and the `_parse_rename_args` helper around line 392-413): the existing two-argument form `/rename <8-hex-prefix> <new-name>` is preserved BUT the prefix is now matched against `claude_session_id[:8]` (mirroring the `/continue` addressing from CCR-042) instead of `str(row.id)[:8]` (the local UUID prefix). Resume-chain rows sharing one `claude_session_id` are all renamed in lockstep so the chain stays visually consistent in `/sessions`. Rows with `claude_session_id IS NULL` are not addressable via `/rename <prefix>` (consistent with `/continue` and `/sessions` from CCR-042 and CCR-044 below).
  - `src/ccr/bot/handlers/session.py` — add a new branch in `cmd_rename` for the literal token `current` in the prefix slot: `/rename current <new-name>` renames the `SessionManager`'s active session (the one started by the most recent `/new` or `/continue`). Three error states, each with a stable reply string and no mutation:
    1. **No active session at all** (manager has no current session) → reply `"No active session to rename."`.
    2. **Active session exists but `claude_session_id IS NULL`** (e.g. status `[idle]` after CCR-044 — session is started but Claude has not yet returned its session id) → reply with a distinct stable error like `"Active session has no claude_session_id yet — try again after it starts."`.
    3. **Active session is fully running with a non-NULL `claude_session_id`** → look up every row sharing that `claude_session_id` (the chain) and overwrite `Session.name` on all of them (same convention as the prefix branch). Reply with the standard rename-success line.
  - `src/ccr/bot/handlers/session.py` — update `cmd_sessions` docstring (around lines 268-283) and `cmd_rename` docstring / `_RENAME_USAGE_HINT` text to reflect the new prefix semantics (`claude_session_id[:8]`) and the `current` keyword. The usage hint must HTML-entity-escape any literal `<` / `>` (precedent from CCR-037).
  - `tests/test_bot_session_handlers.py` (or wherever `/rename` is exercised — locate by grepping for `cmd_rename` / `_parse_rename_args`):
    - Update existing `/rename <prefix> <name>` tests so the prefix matches `claude_session_id[:8]` rather than `str(row.id)[:8]`.
    - Test resume-chain rename: multiple rows sharing one `claude_session_id` all get the new name in one `/rename <prefix> <name>` call.
    - Test prefix that only matches NULL-`claude_session_id` rows → stable unknown-prefix error reply (NULL rows are skipped from prefix matching, as in `/continue`).
    - Add tests for `/rename current <name>`:
      - (a) no active session → exact reply `"No active session to rename."`, no DB mutation.
      - (b) active session with `claude_session_id IS NULL` (i.e. `[idle]` status from CCR-044) → distinct stable reply (developer chooses the exact wording but it MUST differ from the no-active-session reply), no DB mutation.
      - (c) active session with a non-NULL `claude_session_id` → renames every row sharing that `claude_session_id`; standard success reply.
    - Test `/rename` with no args / whitespace only → existing usage hint behaviour preserved.
Out of scope:
  - Auto-naming logic from the first user prompt — unchanged.
  - The session-listing format — handled by CCR-042 / CCR-044.
  - Status lifecycle changes — handled by CCR-044.
  - Removing local-UUID references from other bot replies — handled by CCR-045.
Acceptance:
  - [ ] `/rename <claude-id-prefix> <new-name>` renames every row whose `claude_session_id[:8]` equals the prefix; the local-UUID prefix lookup is removed.
  - [ ] `/rename <prefix> <new-name>` where the prefix only matches NULL-`claude_session_id` rows replies with the documented unknown-prefix error and does not mutate.
  - [ ] `/rename current <new-name>` renames the active session's chain when it has a non-NULL `claude_session_id`; standard success reply.
  - [ ] `/rename current <new-name>` with no active session replies `"No active session to rename."` (verbatim) and does not mutate.
  - [ ] `/rename current <new-name>` with an active session whose `claude_session_id IS NULL` (status `[idle]` from CCR-044) replies with a distinct stable error (different wording from the no-active-session reply) and does not mutate.
  - [ ] `/rename` with no args (or only whitespace) replies with the usage hint and does not mutate; the usage hint documents both `<prefix>` and `current` forms with HTML-safe `&lt;`/`&gt;`.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
  - [ ] `ruff check src tests` passes.
  - [ ] `ruff format --check` passes.
  - [ ] `mypy src` passes.
Depends on: CCR-042, CCR-044
Notes:
  **Reworked in place 2026-05-07** (was originally "drop `/rename`'s prefix arg, add `/setname <prefix> <name>`"). User decision: keep the existing two-argument `/rename <prefix> <name>` shape but rekey the prefix to `claude_session_id[:8]`, AND introduce a `current` keyword in the prefix slot to address the active session. This keeps the surface to a single command while adding the active-session shortcut. The dropped `/setname` design from the original CCR-043 body is preserved nowhere — `/rename current` replaces it.
  Hard dep on CCR-042 (prefix convention rekeyed onto `claude_session_id`) AND CCR-044 (status `[idle]` is what tells `/rename current` to emit the "no `claude_session_id` yet" error rather than the "no active session" error — the two error states are semantically distinct only once `[idle]` exists as a persisted status).
  The "rename all rows in chain" choice mirrors what CCR-042 settled for `/continue`: resume chains share one `claude_session_id` prefix, so renaming should follow that same grouping. The current cmd_rename helper `_parse_rename_args` already loads ALL session rows for prefix matching (per the chat-bot BRIEF gotcha); the change is the field it compares against, not the loop shape.
  Mode 1A note for team-lead: skip the architect — one-file scope, semantic change to existing handler plus a new keyword branch; no new abstraction.

### Review log
  - 2026-05-06 project-manager: filed as CCR-041 follow-up — split rename into active-only `/rename` plus addressable `/setname`
  - 2026-05-07 project-manager: reworked in place — dropped `/setname`, kept `/rename <prefix> <name>` rekeyed to `claude_session_id[:8]`, added `/rename current <name>` for active session; depends on CCR-044 for the `[idle]`-status error path
---

## CCR-044: Persist `[idle]` status; transition to `[running]` on first `claude_session_id`; filter idle rows from `/sessions` and prefix lookups [todo]
Phase: n/a (post-CCR-042 lifecycle alignment; head of the claude_session_id-alignment slice)
Feature: claude-runtime
Files:
  - `src/ccr/claude/state.py` — keep the in-memory `SessionStatus.IDLE = "idle"` member but update the module docstring: `idle` is now ALSO a valid value for the SQL `Session.status` column (today the docstring says SQL accepts only `running | completed | stopped | crashed` — that is what changes). Document the transition rule: a row is INSERTed as `idle` and flips to `running` when the manager observes the first `claude_session_id` from a `SystemInit` event (or a manual `ccr session save` import — see Notes).
  - `src/ccr/claude/manager.py` — `SessionManager._db_insert_session` (and any other INSERT path for `Session` rows): write `status = SessionStatus.IDLE` instead of `SessionStatus.RUNNING` on initial insert. The existing `_update_claude_session_id` task (the fire-and-forget one that writes the column from the first `SystemInit` event, per claude-runtime BRIEF) is the right hook for the `idle → running` transition: the same UPDATE that sets `claude_session_id` also flips `status` to `RUNNING`. Keep the existing `_claude_session_id_persisted` single-shot guard semantics; on subsequent `SystemInit` events the UPDATE is a no-op.
  - `src/ccr/claude/manager.py` — `_db_lookup_resumable_claude_session_id` (around the lines documented in CCR-042): `idle` rows are skipped from prefix matching just like NULL-`claude_session_id` rows are today. This is naturally true if the query already filters on `claude_session_id IS NOT NULL` (idle rows have NULL `claude_session_id` by construction), but encode it explicitly so a future code change that decouples the two conditions does not regress.
  - `src/ccr/claude/import_session.py` — `import_claude_session` inserts rows with `status="stopped"` today (per the claude-runtime BRIEF invariant); preserve that. The manual `ccr session save <claude-id>` path provides a non-NULL `claude_session_id` at insert time so the row never passes through `idle`. Add a comment at the insert site documenting that `idle` is reserved for sessions whose `claude_session_id` has not yet been assigned (i.e. a fresh `/new` before Claude returns its session id).
  - `alembic/versions/<NNNN>_add_idle_to_session_status.py` (new migration) — additive migration. SQLite stores `Session.status` as TEXT (no enum constraint at the DB layer per the SessionStatus comment), so the migration body is mostly a doc/no-op for SQLite, but the migration MUST exist as the schema-level record that `idle` is now a valid persisted value (so anyone reading `alembic/versions/` learns the lifecycle change). Backfill plan for pre-existing rows where `claude_session_id IS NULL`: PM decision below — **do NOT backfill to `idle`**. Pre-existing NULL-`claude_session_id` rows are legacy artefacts from before the lifecycle change; they keep their existing terminal status (typically `stopped` / `completed` / `crashed`) and remain non-addressable via `/continue` / `/rename` exactly as today (CCR-042 already settled this). The migration is forward-only for the lifecycle: only NEW sessions go through `idle`. Document this decision in the migration's docstring.
  - `src/ccr/bot/handlers/session.py` — `cmd_sessions` / `_format_session_row` (around lines 268-360): filter rows with `status == SessionStatus.IDLE` from the listing query. Since the `_NULL_CLAUDE_SESSION_ID_MARKER = "--------"` rendering exists today only because NULL-`claude_session_id` rows could appear in `/sessions`, and after this ticket no NEW NULL rows surface (idle is filtered out, running rows have non-NULL claude_session_id), the marker rendering becomes obsolete for new sessions. **Decision: keep the marker rendering for now** so legacy NULL rows from before this ticket still render coherently — but remove the in-line CCR-042 fix-loop comment that frames the marker as the going-forward shape, and update the `cmd_sessions` docstring to clarify the marker is a legacy-only artefact. (Removing the marker entirely is a follow-up ticket once we are confident no NULL rows remain in production DBs.)
  - `src/ccr/bot/handlers/session.py` — `cmd_pid` (around line 213-228): the existing branch `if inf.get("status") == SessionStatus.IDLE or inf.get("session_id") is None:` is now reachable as a real persisted state (today `IDLE` is in-memory only). Audit the reply text — it says "no active session" today; the new semantics are "session exists but is idle waiting for Claude". Decision: keep the user-facing reply identical for now (idle is invisible to the user), but verify the test that exercises this branch still asserts the right thing.
  - `src/ccr/bot/handlers/session.py` — any other reads of `SessionStatus` that today implicitly assume "session row implies running" (`/clear` divider gate, etc.) — audit and update if the new `idle` state would break the assumption. The `/clear` gate is `prior_status != IDLE` today, which is correct for the new semantics (idle clears stay quiet) — no change needed there, but verify under test.
  - `tests/test_session_manager.py` (or wherever the manager lifecycle is exercised): add cases for (a) a fresh `new_session(...)` insert lands the row with `status == "idle"` and `claude_session_id IS NULL`; (b) on the first `SystemInit` event, the row's `status` flips to `"running"` and `claude_session_id` is populated atomically (single UPDATE); (c) a second `SystemInit` event does NOT re-flip status (idempotent); (d) `_db_lookup_resumable_claude_session_id` skips idle rows from prefix matching (positive: matches a `running` / `completed` / `stopped` row sharing the prefix; negative: a prefix that only matches an idle row → `SessionNotFoundError`).
  - `tests/test_bot_session_handlers.py` — assert `/sessions` does not list rows where `status == "idle"`. Existing `/sessions` rendering tests for legacy NULL-`claude_session_id` rows (the marker `--------`) stay green because those legacy rows have terminal statuses (`stopped` / `completed` / `crashed`), not `idle`.
Out of scope:
  - Removing the `_NULL_CLAUDE_SESSION_ID_MARKER` constant and its `<code>--------</code>` rendering — deferred to a follow-up once NULL rows are confirmed extinct in the field.
  - Backfilling pre-existing NULL-`claude_session_id` rows.
  - Chat-output cleanup of local UUIDs (handled by CCR-045).
  - Rekeying `/stop` / other commands (handled by CCR-046).
  - `/rename current` semantics (handled by CCR-043 rework).
Acceptance:
  - [ ] A fresh `new_session(...)` call inserts a `Session` row with `status = "idle"` and `claude_session_id IS NULL`.
  - [ ] On the first `SystemInit` event arriving for that session, the row's `status` transitions to `"running"` and `claude_session_id` is populated in the same UPDATE (single round-trip).
  - [ ] A second / later `SystemInit` event for the same session does NOT re-flip `status` (idempotent, single-shot semantics preserved per CCR-036's `_claude_session_id_persisted` guard).
  - [ ] `/sessions` does not list rows where `status == "idle"`.
  - [ ] `_db_lookup_resumable_claude_session_id` skips idle rows from prefix matching: a prefix that matches only an idle row raises `SessionNotFoundError` (consistent with the NULL-`claude_session_id` skip behaviour from CCR-042).
  - [ ] An additive Alembic migration documenting `idle` as a valid `Session.status` value is added (forward-only; pre-existing NULL-`claude_session_id` rows are NOT backfilled to `idle` and keep their existing terminal status).
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
  - [ ] `ruff check src tests` passes.
  - [ ] `ruff format --check` passes.
  - [ ] `mypy src` passes.
Depends on: CCR-036, CCR-041, CCR-042
Notes:
  **Head of the claude_session_id-alignment slice.** This ticket changes status semantics that CCR-043 (rework), CCR-045 (chat output cleanup), and CCR-046 (commands rekey + `/stop`) all reference — it must land first. The `/rename current` command (CCR-043 rework) needs `[idle]` to exist as a persisted state to surface its distinct "active session has no claude_session_id yet" error.
  **Supersedes the marker rendering trajectory from CCR-042.** CCR-042's fix-loop introduced the `--------` marker for NULL-`claude_session_id` rows in `/sessions` because such rows existed (legacy + the brief window between `new_session` insert and the first `SystemInit`). After CCR-044, the brief-window case disappears (idle rows are filtered out of `/sessions` entirely) and only legacy NULL rows surface. The marker rendering is kept for legacy backward-compat but is no longer the going-forward shape — flag this clearly in the BRIEF refresh so the reviewer does not flag the surviving `_NULL_CLAUDE_SESSION_ID_MARKER` constant as a regression. The constant's eventual removal is a separate follow-up once we are confident no NULL rows remain in production DBs (and a manual SQL backfill / row-prune step is documented).
  **Architect path likely.** This crosses the bot/manager/db boundary, modifies the SessionStatus state machine, requires an Alembic migration, and reshapes a load-bearing invariant from the claude-runtime BRIEF ("`SessionStatus.IDLE` is intentionally absent from `Session.status` SQL writes — the SQL column accepts `running | completed | stopped | crashed` only"). Team-lead Mode 1A should call the architect — small ticket count but high coordination. The architect plan should explicitly call out the BRIEF invariant flip and prescribe how `_update_claude_session_id` is augmented to also flip `status` (single UPDATE statement vs two), and confirm whether the existing `_claude_session_id_persisted` guard suffices or needs a sibling `_status_flipped` guard.
  Cross-listed BRIEF impact: claude-runtime BRIEF must be updated to remove the "`IDLE` is intentionally absent from SQL writes" invariant and replace it with the new lifecycle. chat-bot BRIEF must be updated to reflect `/sessions` filtering idle rows and the marker rendering being legacy-only.

### Review log
  - 2026-05-07 project-manager: filed — head of claude_session_id-alignment slice; persist `[idle]` and transition to `[running]` on first claude_session_id; filter idle rows from `/sessions` and prefix lookups
---

## CCR-045: Remove local-UUID `Session.id` references from bot user-facing replies; replace with pid where useful [todo]
Phase: n/a (post-CCR-044 chat-bot UX cleanup; part of the claude_session_id-alignment slice)
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/session.py` — drop the local-UUID id from user-facing reply strings:
    - `cmd_new` (around line 108): `"Session {_short_id(session_id)} started (pid {pid})."` → `"New session started (pid {pid})."`.
    - `cmd_continue` (around line 172): `"Session {_short_id(session_id)} resumed (pid {pid})."` → `"Session resumed (pid {pid})."`. (If a chat-only handle is meaningful here, the developer MAY substitute the `claude_session_id[:8]` prefix once it is known — but `cmd_continue` returns before the manager has captured `claude_session_id` for fresh-resume cases, so the simpler "Session resumed (pid {pid})" phrasing is preferred.)
    - The unnamed handler around line 209 (mirroring `cmd_new` for plain-text first prompts): same treatment as `cmd_new`.
    - `cmd_pid` (around line 222): the reply renders `inf["session_id"][:8]` today (the local UUID prefix). Replace with the pid only — `f"Session running (pid {pid})."` or equivalent. After CCR-044, an idle session has no claude_session_id to render and the local UUID is internal; pid is the user-visible handle.
  - `src/ccr/bot/handlers/session.py` — audit `_short_id` (lines 76-77): once the call sites above are rewritten, `_short_id` becomes dead code. Remove it (and any test that exercises it directly) UNLESS another live caller surfaces during the audit; in that case document the surviving caller in the BRIEF refresh.
  - `src/ccr/bot/handlers/passthrough.py` — `/cost` rendering (`_render_cost_reply` around line 249-273, called from line 302 with `session_id.hex`): the rendered `id8 = html.escape(session_id_hex[:_SESSION_ID_HEX_PREFIX_LEN])` is the local UUID's first 8 hex characters. Replace with `claude_session_id[:8]` if the session has one (look it up via the manager / db), or drop the id slot from the `/cost` reply when no `claude_session_id` is available. Developer's call which fits the `/cost` template best — the user is OK with dropping the id entirely when it cannot be replaced by a meaningful handle. Keep `_SESSION_ID_HEX_PREFIX_LEN` if reused; remove it if not.
  - `src/ccr/bot/handlers/session.py` — audit `cmd_sessions`'s `_format_started_by` and surrounding rendering for any other local-UUID surfaces; the `<id8>` slot in `_format_session_row` is already `claude_session_id[:8]` (CCR-042) and stays that way — this audit is for any OTHER incidental UUID rendering missed by the grep below.
  - **Grep canary**: a developer-side grep over `src/ccr/bot/` for `session.id`, `Session.id`, `session_id`, `_short_id`, `.hex[:8]` MUST return only callback-payload encoding sites (`perm:{session_id}:...`, `auq:{session_id}:...` in `keyboards.py` and the parsing logic in `permission.py` / `ask_user_question.py`) and internal log/structlog statements — never user-facing reply strings. The PR description should include the grep output as evidence.
  - `tests/test_bot_session_handlers.py` (and `tests/test_bot_session.py`) — update reply-string assertions for `cmd_new`, `cmd_continue`, the plain-text first-prompt handler, and `cmd_pid` to match the new pid-based phrasing. Remove tests that asserted on the `_short_id(session_id)` content if any.
  - `tests/test_bot_passthrough.py` — update `/cost` reply assertions for the new id-slot rendering (or its removal).
Out of scope:
  - Status-lifecycle changes (handled by CCR-044).
  - Command argument rekeying (handled by CCR-046; `/rename` by CCR-043 rework).
  - Callback data payloads (`perm:`, `auq:`, `cfg:`) — those keep the local-UUID encoding because `session_id` is unambiguous server-side and the callback data is server-controlled, not user-typed.
  - Internal log lines / structlog statements — those keep the local UUID for debugging.
  - Web viewer / SSE routes (use `Session.id` UUID via path params; those are not chat-output).
Acceptance:
  - [ ] `cmd_new`, `cmd_continue`, the plain-text first-prompt handler, and `cmd_pid` no longer render a local-UUID prefix in their replies; pid is exposed where helpful, otherwise the id reference is dropped entirely.
  - [ ] `/cost` reply no longer renders a local-UUID prefix; either substitutes `claude_session_id[:8]` (when available) or drops the id slot.
  - [ ] `_short_id` is removed (or its remaining caller is documented) and the chat-bot BRIEF reflects the change.
  - [ ] A grep over `src/ccr/bot/` for local-UUID surfacing (`session.id`, `Session.id`, `_short_id`, `.hex[:8]`) returns only callback-payload encoding sites and internal logs — no user-facing reply strings. The PR includes the grep output as evidence.
  - [ ] All existing reply-string tests are updated to the new phrasing; no test still asserts on `Session {<8-hex>} started/resumed`.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
  - [ ] `ruff check src tests` passes.
  - [ ] `ruff format --check` passes.
  - [ ] `mypy src` passes.
Depends on: CCR-044
Notes:
  **Why depends on CCR-044:** CCR-044 changes when `claude_session_id` becomes available (it is NULL during `[idle]`, populated on transition to `[running]`). `cmd_new` and the plain-text first-prompt handler return BEFORE the first `SystemInit` event arrives, so even after CCR-044 they cannot substitute `claude_session_id[:8]` for the local UUID — the right answer there is the pid-based phrasing. Landing this ticket before CCR-044 would not be wrong but would risk reviewers asking "why not use claude_session_id?" without the lifecycle context.
  Pid is the Claude subprocess pid, already exposed via `manager.pid` / `inf["pid"]` (used in `cmd_new` today). It is a stable user-visible handle for the active subprocess.
  Mode 1A note for team-lead: skip the architect — stylistic + grep-and-replace across handlers + snapshot test updates; no new abstraction. Reviewer focus: the grep canary, plus confirming the callback-payload sites are NOT touched.

### Review log
  - 2026-05-07 project-manager: filed — chat-output cleanup, drop local-UUID from user-facing replies, replace with pid where useful
---

## CCR-046: Rekey `/stop` and remaining session-reference commands to `claude_session_id[:8]`; audit residual surfaces [todo]
Phase: n/a (post-CCR-044 chat-bot command alignment; part of the claude_session_id-alignment slice)
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/session.py` — `cmd_stop` (around line 112-130): today `/stop` takes no argument and stops whatever the manager's active session is (single-session invariant). The user wants it to "work the same with claude session id" — interpreted as: `/stop` continues to work with no argument (stops the active session), AND optionally accepts a `<claude-id-prefix>` argument that targets a row by its `claude_session_id[:8]`. If the prefix matches the currently-running session, stop it; if the prefix matches a NON-active session, the reply is a stable "session is not the active session" error (we do not stop arbitrary historical rows — they are already stopped). If the prefix matches no row, stable unknown-prefix error. With CCR-044's `[idle]` lifecycle, `/stop` on the active session works whether the session is idle or running.
    - Alternative interpretation if developer/team-lead disagrees: the no-arg `/stop` is unchanged and "rekey to claude_session_id" is a no-op for `/stop` because it never accepted an argument. In that case, the ticket's `/stop` work reduces to confirming no local-UUID surfaces leak in the reply strings (cross-checked with CCR-045) and documenting in the BRIEF that `/stop` is single-session-invariant-only. **Flag the choice in the developer's BRIEF update note so team-lead can record it.**
  - `src/ccr/bot/handlers/session.py` — audit ALL remaining bot commands for session-reference arguments and confirm each is rekeyed onto `claude_session_id[:8]` or explicitly does not take a session reference. Today's command list per chat-bot BRIEF: `/start`, `/new`, `/stop`, `/clear`, `/who`, `/pid`, `/sessions`, `/continue`, `/rename`, `/answer`, `/agents`, `/skills`, `/cost`, `/usage`, `/config`, `/model`, `/compact`, plus the `/mcp` / `/init` redirects. Per-command audit checklist:
    - `/continue <prefix>` — rekeyed in CCR-042 (done, no work).
    - `/sessions` — listing rekeyed in CCR-042 (done, no work).
    - `/rename <prefix> <new-name>` and `/rename current <new-name>` — handled by CCR-043 rework.
    - `/answer <id8> <text>` — `<id8>` is the AskUserQuestion `tool_use_id` prefix, NOT a session reference. No change. Document in the BRIEF that this id8 is intentionally a different namespace.
    - `/start`, `/new`, `/clear`, `/who`, `/pid`, `/agents`, `/skills`, `/cost`, `/usage`, `/config`, `/model`, `/compact` — none currently take a session-reference argument. Confirm and document.
    - `/stop` — see above.
    - The list of any other command that takes a session reference must be enumerated and either rekeyed in this ticket or split into a follow-up ticket with a clear rationale.
  - `tests/test_bot_session_handlers.py` (and `tests/test_bot_session.py`) — add `/stop` tests for the chosen semantics: (a) no-arg `/stop` continues to work (active session, both `[idle]` and `[running]` states from CCR-044); (b) IF the developer/team-lead chooses the prefix-arg variant: `/stop <claude-id-prefix>` matching the active session stops it, matching a non-active row replies with a stable error, matching no row replies with a stable unknown-prefix error.
Out of scope:
  - Status-lifecycle changes (handled by CCR-044).
  - Chat-output cleanup (handled by CCR-045).
  - `/rename` rework (handled by CCR-043 rework).
  - Web viewer / SSE routes (use `Session.id` UUID via path params; those are not chat command surfaces).
Acceptance:
  - [ ] `/stop` audit completed and documented: either the no-arg form is preserved as-is (with a BRIEF entry noting `/stop` is single-session-invariant-only and takes no session reference), or `/stop <claude-id-prefix>` is added with the prefix matched against `claude_session_id[:8]` and the three reply branches above implemented.
  - [ ] No-arg `/stop` continues to work and stops the active session in both `[idle]` and `[running]` states (CCR-044 lifecycle).
  - [ ] An audit of every bot command listed in the chat-bot BRIEF is documented in the developer's BRIEF update note: each command is either rekeyed onto `claude_session_id[:8]` already (or in this ticket), or explicitly noted as taking no session reference, or split into a follow-up ticket.
  - [ ] No bot command (other than `/answer`, whose id8 is a `tool_use_id` not a session reference) accepts a local-UUID prefix as an argument anywhere.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
  - [ ] `ruff check src tests` passes.
  - [ ] `ruff format --check` passes.
  - [ ] `mypy src` passes.
Depends on: CCR-044
Notes:
  **Why depends on CCR-044:** the `[idle]` lifecycle state changes what "active session" means for `/stop` (today an idle session is not in the DB; after CCR-044 it is). The audit and tests for `/stop` need that lifecycle settled.
  **Why no dep on CCR-042:** CCR-042 already rekeyed `/continue` and `/sessions`; this ticket extends the same pattern to anything left over. The audit step is the load-bearing deliverable — the user wants confidence that EVERY command surface is aligned, not just `/stop`.
  Mode 1A note for team-lead: skip the architect — small scope, primarily an audit + a single-handler change. The audit's output (the documented per-command list in the dev report) is what the BRIEF refresh consumes. Reviewer focus: the audit is complete and any "split into a follow-up ticket" decision is supported by clear reasoning rather than a punt.

### Review log
  - 2026-05-07 project-manager: filed — `/stop` rekey + audit residual session-reference surfaces; user explicitly called out `/stop` and asked for confidence that all commands are aligned
---
