# Brief: web-viewer

## Purpose
Read-only multi-tab web viewer + localhost-app preview proxy, both behind JWT auth, served by FastAPI on `127.0.0.1` and reached over a user-supplied tunnel (Tailscale Funnel by default). The viewer streams Claude session events via SSE with replay-then-tail; the proxy forwards to a localhost dev app behind a port allowlist. Owned by **web-developer** per `WORKFLOW.md`. Not yet implemented.

## Key invariants
_(team lead appends from each developer's BRIEF update note as tickets land)_

Anticipated (from `CLAUDE.md` + plan Phases 11–13):
- **Two stateless JWT kinds**, 30-min TTL: `viewer` and `preview`. Tokens arrive as `?token=...` on `/auth`, get exchanged for an HttpOnly cookie. Preview tokens carry `payload.port` which **must match the URL port** — port-mismatch must reject in the proxy.
- Cookie name `ccr_session`; `HttpOnly`, `Secure`, `SameSite=Lax`; TTL from `COOKIE_TTL_SECONDS`.
- SSE replay-then-tail: `?from=<seq>` reads from `data/logs/<session_id>.jsonl` (JSONL `seq` is line-count derived) then tails via the in-process `EventBus`.
- xterm.js loaded from jsDelivr with **subresource-integrity hashes** pinned in HTML. Vendoring is a fallback; for now the viewer needs internet.
- Proxy strips `Cookie` and `Authorization` before forwarding to the local app and rejects `Upgrade: websocket` with 502. Hop-by-hop header stripping per RFC 7230 §6.1. `httpx.AsyncClient.stream()` for both directions.
- Web binds `127.0.0.1` (not `0.0.0.0`); permissive CORS (`allow_origins=["*"]`) is forbidden.
- Tests use `httpx.AsyncClient(app=app)` — async in-process, no real network.

## Public surface
_(team lead appends from each developer's BRIEF update note as tickets land)_

Anticipated layout:
- `src/ccr/web/app.py` — FastAPI factory, app.state wiring, CORS lockdown.
- `src/ccr/web/auth.py` — `/auth` token-exchange handoff, cookie middleware, `CookieAuthDep`.
- `src/ccr/web/sessions.py` — `/api/sessions`, `/api/sessions/{id}/log`, SSE `/api/sessions/{id}/events`, `/healthz`.
- `src/ccr/web/proxy.py` — `/app/{port}/{path}` reverse proxy.
- `src/ccr/web/viewer.py` — `/viewer` route.
- `src/ccr/web/static/{viewer.html, viewer.js, viewer.css}` — single ES-module frontend; xterm.js via CDN with SRI.
- `src/ccr/auth/tokens.py` (lives in `auth` feature, owned by python-developer) — `mint`, `verify`, `verify_kind`. The web layer **consumes** this surface but does not edit it.

## Subtleties / gotchas
_(team lead appends from each developer's BRIEF update note as tickets land)_

Anticipated:
- The `mint` / `verify` / `verify_kind` boundary lives in `auth/`. Calling bare `verify` instead of `verify_kind` at any consumer site is a security finding.
- SRI hash regen procedure should be documented in an HTML comment near the `<script>` tags so future bumps are reproducible: `curl ... | openssl dgst -sha384 -binary | openssl base64 -A`.
- `PROXY_PORT_ALLOWLIST` blank ⇒ any port allowed; non-blank ⇒ check the URL port against the allowlist *and* against `payload.port`.
- The viewer is a single ES module — no bundler, no `npm install`. Adding a build step is a scope expansion.

## Cross-feature relations
- depends on: core (Settings, async engine, logging), auth (`tokens.mint` / `verify_kind` consumed), claude-runtime (SSE consumes `JsonlSessionLog.read_from` + `EventBus.subscribe`; `SessionManager.info()` for the session list).
- used by: nothing (this is a parallel user surface; the bot is the other one).

## Status
- State: IN PROGRESS
- Tickets: CCR-011 (lives under `auth`), CCR-012, CCR-013, CCR-015
- Last updated: _(none yet — first APPROVED bumps this)_
