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
---

## CCR-022: Richer `/agents` reply (Running + Library) [todo]
Phase: n/a (post-CCR-010 UX polish)
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/passthrough.py` — remove `"agents"` from `BLOCKED_INTERACTIVE`; add a dedicated `cmd_agents`-style branch (or a dispatch to a new `agents.py` handler — developer's call) that builds a two-section reply: "Running" (currently active subagents — see Notes for the data-source decision) and "Library" (configured agents on disk). Replace the canned `f"Interactive command — run /{name} in your local Claude Code terminal."` reply for `/agents` only; the other entries in `BLOCKED_INTERACTIVE` (`mcp`, `init`) stay as-is. Empty sections render with a stable placeholder (e.g. `"(none)"`).
  - `src/ccr/bot/handlers/agents.py` (optional, developer's call) — if the rendering is non-trivial, lift it out of `passthrough.py` into its own handler module and register on `session_router` (or a new `agents_router`) instead of letting `passthrough.py` carry it. Either layout is acceptable; do not split into two files unless it actually reduces complexity.
  - Possibly `src/ccr/claude/manager.py` — if "Running" is sourced from the `SessionManager` (e.g. tracked subagent ids from `tool_use` / `tool_result` events seen on the bus or in the JSONL log), expose a small read-only accessor (e.g. `running_subagents() -> list[str]` or similar). PM is not prescribing the shape — architect/team-lead settle this in Mode 1A. If "Running" turns out to be unsourceable from our side (Claude does not surface it in `-p` stream-json), drop the section gracefully or mark it `"(unknown — not exposed by claude -p)"`.
  - `tests/test_bot_passthrough.py` (existing, from CCR-010) — extend: assert `/agents` no longer returns the BLOCKED_INTERACTIVE canned line; assert the reply contains both `"Running"` and `"Library"` section headers; assert empty-state placeholder when no library agents and no running subagents; assert HTML-escaping of agent names containing `<`, `>`, `&`. Manual smoke (real Claude session with one running subagent + at least one `.claude/agents/*.md` on disk) is documented but unticked, mirroring CCR-010's pattern.
Out of scope:
  - Adding a `/subagents` command or any per-subagent tab in the web viewer.
  - Editing / creating agent library files from Telegram (read-only listing only).
  - Changing the reply for `/mcp` and `/init` — they remain in `BLOCKED_INTERACTIVE` with the existing canned text.
  - Globbing agent libraries outside `.claude/agents/` (no `~/.claude/agents/`, no project-tree walking) unless the architect decides otherwise during Mode 1A.
Acceptance:
  - [ ] `/agents` reply contains a `"Running"` section header and a `"Library"` section header.
  - [ ] With no `.claude/agents/*.md` on disk and no running subagents, the reply renders both section headers with the chosen stable empty-state placeholder (e.g. `"(none)"`) under each.
  - [ ] With `.claude/agents/foo.md` and `.claude/agents/bar.md` present, the Library section lists `foo` and `bar` (sort order developer's call but must be stable across calls — alphabetical recommended).
  - [ ] `/agents` no longer returns the literal `"Interactive command — run /agents in your local Claude Code terminal."` string returned by CCR-010 (regression check on the BLOCKED_INTERACTIVE removal).
  - [ ] `/mcp` and `/init` still return the BLOCKED_INTERACTIVE canned text (regression — only `/agents` is being lifted out).
  - [ ] Agent names containing `<`, `>`, `&` are HTML-escaped in the reply (consistent with `formatting.py` conventions used in CCR-008/CCR-018).
  - [ ] `pytest tests/test_bot_passthrough.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
  - [ ] Manual smoke (unticked, not blocking review per CCR-010 precedent): with a real Claude session running a subagent and `.claude/agents/*.md` on disk, `/agents` shows the running subagent under "Running" and the library files under "Library".
Depends on: CCR-010
Notes:
  Phase n/a in the plan — post-CCR-010 UX polish, same precedent as CCR-018, CCR-019, CCR-020, CCR-021. The router from CCR-010 is the surface this ticket extends.
  **Design decision for team-lead Mode 1A (architect candidate).** What sources "Running" and "Library"?
    - "Library" is straightforward — glob `.claude/agents/*.md` from the project working directory, parse just the filename (or YAML frontmatter `name:` if present, mirroring how `dev-stack-agents` formats them under `templates/`). Filesystem-only, no claude-binary dependency.
    - "Running" is the open question:
      (a) Source from `SessionManager`: track subagent ids as they appear in `tool_use` / `Task`-style events in the JSONL log; expose a snapshot accessor. Most accurate, but requires deciding what "running" means (started but not yet returned a result?) and whether we trust those events to map cleanly.
      (b) Source from disk-only: drop the "Running" section entirely and just rename to a single "Library" reply.
      (c) Hybrid: render both sections, but mark "Running" as `"(unknown — not exposed by claude -p)"` until a follow-up ticket can wire it.
    PM recommends team-lead consider dispatching the architect (Mode 1A architect path) — this is a load-bearing call about whether `SessionManager` gains a new public accessor and whether we start tracking subagents at all. The architect should be told the developer will *not* probe a real Claude binary as part of this ticket (unlike CCR-020/CCR-021); the architect picks (a)/(b)/(c) from the existing JSONL event schema in `ccr.claude.events`.
  Reply formatting: HTML escape every interpolated name; reuse `formatting.py` chunking if the library section grows long (cap message body to ≤ 3500 chars consistent with CCR-019 conventions). Reply mode is HTML, matching the rest of the bot.
  The existing `_UNKNOWN_USAGE_HINT` in `passthrough.py` does not list `/agents` today — that hint is for unrecognised commands, not the whitelist. No change required there.

### Review log
---

## CCR-023: Richer `/cost` reply (more detail than the upstream one-liner) [todo]
Phase: n/a (post-CCR-010 UX polish)
Feature: chat-bot
Files:
  - `src/ccr/bot/handlers/passthrough.py` — `/cost` is currently in `WHITELIST` and forwards verbatim via `SessionManager.send_slash("cost", "")`. Lift `/cost` out of the verbatim-forward branch into a dedicated handler (or keep it in `passthrough.py` behind an early-return check — developer's call) that either (a) still forwards but enriches Claude's reply with locally computed context, or (b) computes a richer reply entirely on our side from the JSONL log, depending on the Step 0 probe outcome.
  - `src/ccr/bot/handlers/cost.py` (optional, developer's call) — if the enrichment logic is non-trivial, lift into its own handler module. Either layout is acceptable.
  - Possibly `src/ccr/claude/manager.py` — if the richer reply pulls from session-level aggregates (per-session token counts, per-tool call counts, elapsed time, etc.), expose a read-only accessor that walks the current session's JSONL log or maintains running counters. PM is not prescribing the shape; architect/team-lead settle in Mode 1A.
  - Possibly `src/ccr/claude/log.py` (or a new `src/ccr/claude/usage.py`) — if a JSONL-walking aggregator is needed, that's where it lives. Read-only, no schema changes.
  - `tests/test_bot_passthrough.py` (existing, from CCR-010) — extend with `/cost` cases that match whichever path the probe selects: assert the reply includes the additional fields the team picks (e.g. `Tokens:`, `Tools:`, `Elapsed:`, `Session:`); assert HTML-escaping of any interpolated values; assert behaviour with no active session matches the existing `"No active session."` reply where applicable.
  - `tests/fakes/fake_claude.py` — extend the canned JSONL fixture if the richer reply parses Claude's response (e.g. emit a synthetic `result` line with usage stats) so the tests do not need a real claude binary.
Out of scope:
  - Real-money pricing / billing integrations (we only enrich what's already in the local session — no API calls to Anthropic billing, no model-specific price tables).
  - Persisting cost / usage history across sessions in the SQL schema (the current session's JSONL is the only source — cross-session aggregates are a follow-up ticket).
  - Changing how `/cost` arguments are parsed (the whitelist forwards `/cost args`; our enriched reply ignores args unless the probe shows otherwise).
  - Modifying any other whitelist commands (`/model`, `/compact` stay verbatim-forward).
Acceptance:
  - [ ] Step 0 probe outcome documented in the developer's work summary (which of the three outcomes from Notes was hit, and which path the implementation took).
  - [ ] `/cost` reply contains at least one piece of information beyond the upstream one-liner — the exact fields are settled by the architect/team-lead in Mode 1A from the probe data, but the reply must be visibly richer than `"You are currently using your subscription to power your Claude Code usage"`.
  - [ ] `/cost` with no active session returns the existing `"No active session."` reply (regression — current CCR-010 behaviour preserved when our handler cannot enrich).
  - [ ] Interpolated values are HTML-escaped (consistent with `formatting.py` conventions used in CCR-008/CCR-018).
  - [ ] `/model` and `/compact` continue to forward verbatim via `SessionManager.send_slash` (regression — only `/cost` is being lifted out).
  - [ ] `pytest tests/test_bot_passthrough.py` passes.
  - [ ] `pytest --cov=ccr --cov-fail-under=80` passes.
  - [ ] Manual smoke (unticked, not blocking review per CCR-010 precedent): real Claude session with a few user turns; `/cost` shows our enrichment alongside (or instead of) Claude's terse line.
Depends on: CCR-010
Notes:
  Phase n/a in the plan — post-CCR-010 UX polish, same precedent as CCR-018, CCR-019, CCR-020, CCR-021. The router and whitelist from CCR-010 are the surface this ticket extends.
  **Step 0 probe — required before writing any code.** Mirror the CCR-020 / CCR-021 pattern: run `claude -p --input-format=stream-json --output-format=stream-json --verbose`, send a few user turns, then send `/cost` as a user turn (`{"type":"user","message":{"role":"user","content":"/cost"}}`) and capture the full stdout JSONL. Three documented outcomes:
    1. Claude emits `usage` / token-count fields on every `result` event (or on a dedicated `cost`-style event) — implementation pulls from the running JSONL log via a read-only aggregator (`src/ccr/claude/usage.py` or similar) and replies with a richer summary computed on our side, ignoring the upstream one-liner. Smallest path, no enrichment of Claude's text.
    2. Claude's `/cost` reply itself contains structured detail (multi-line text, JSON-in-text, fields beyond the one-liner the user saw) but only for some accounts / modes — implementation forwards `/cost` and post-processes the captured text events into a parsed reply. Brittle (text-format dependency); document fragility in the work summary.
    3. Claude emits nothing structured for `/cost` and does not include usage on `result` events — implementation either (a) computes a coarse local summary from JSONL line counts + elapsed time + parsed `assistant` text length (no real token counts) and labels it as approximate, or (b) returns BLOCKED with notes for follow-up. Default to (a) unless the architect says BLOCKED is preferable.
  The developer must document which outcome they hit and which path they took in the work summary.
  **Design decision for team-lead Mode 1A (architect candidate).** This is a load-bearing call: whether to introduce a new aggregator module, whether `SessionManager` gains a public usage accessor, and which fields the reply carries. PM recommends team-lead consider dispatching the architect, especially if outcome 1 is hit (new module, JSONL walking) or outcome 3 (a) is taken (approximation labelling matters for user trust). For outcome 2 the developer can probably proceed without an architect.
  The user's verbatim ask was "Usage should include more" — interpret that as "more than the one terse line currently returned", not as a fixed field list. The team-lead/architect picks the field list from what the probe makes available.
  Reply formatting: HTML escape every interpolated value; reuse `formatting.py` chunking if needed; cap message body to ≤ 3500 chars consistent with CCR-019. Reply mode is HTML, matching the rest of the bot.
  Note that the user said "Usage should include more" — they may be conflating `/cost` with a separate `/usage` command. Claude Code's TUI exposes both: `/cost` (this session's spend) and `/usage` (account-wide quota). If the probe shows `/usage` as a separate slash command not currently in our whitelist, flag it for a follow-up ticket; do NOT expand this ticket's scope to cover both. This ticket is scoped to `/cost` only.

### Review log
