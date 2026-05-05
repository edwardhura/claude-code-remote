# Brief: auth

## Purpose
Owner-controlled pairing and JWT minting. The bot's `/start` and the console REPL approve / revoke Telegram users; once paired, a user can drive the Claude session and receive broadcasts. Phase 11 adds JWT signed-URL tokens on top, used by the web viewer / preview proxy. The pairing functions are pure-async and shared between the CLI subcommands, the console REPL, and the bot's `/start` flow.

## Key invariants
- Allowlist is keyed on the immutable `tg_user_id`, never on `@username`.
- First pairing approval auto-promotes the user to `is_owner = TRUE` (single-owner partial unique index in `core` enforces uniqueness).
- Owner can never be revoked — `revoke` raises `CannotRevokeOwnerError` for the owner row.
- After the owner exists, every subsequent approval requires that the caller is acting as / on behalf of the existing owner (UI rule, enforced at the `/start` flow + console).
- Pairing codes are 8-char hex with a 5× collision retry on insert; a code is single-use and expires (`used_at IS NULL` + TTL window).
- All write paths (`approve`, `revoke`, `invite`) commit inline. Read paths (`list_paired`, `list_pending`, `is_paired`, `is_owner`) only read.
- Soft-revoke: `revoke()` sets `revoked_at`; `is_paired` / `is_owner` filter on `revoked_at IS NULL`.

## Public surface
- `src/ccr/auth/__init__.py` — re-exports the public pairing + allowlist surface.
- `src/ccr/auth/pairing.py` — pure async pairing operations: `create_code`, `list_pending`, `list_paired`, `get_owner`, `approve`, `revoke`, `invite`. Functions take an `AsyncSession` first. Domain errors `PairingError`, `CannotRevokeOwnerError`.
- `src/ccr/auth/allowlist.py` — predicates `is_paired(session, tg_user_id)`, `is_owner(session, tg_user_id)`; both filter on `revoked_at IS NULL`.
- `src/ccr/cli.py` — wires `pair {list, pending, approve, revoke, invite}` subcommands; opens a fresh `AsyncSession` per command via `Settings()` + `create_engine_from_settings`. Output strings are pinned by tests: `"(empty)"`, `"Approved Telegram user <id> (owner|paired)"`, `"Cannot revoke owner."`.
- (Planned, CCR-011) `src/ccr/auth/tokens.py` — JWT mint / verify with two `kind`s (`viewer`, `preview`); 30-min TTL; HS256 with secret from `Settings.JWT_SECRET`; `verify_kind(token, kind)` enforces `kind` claim. The web layer consumes this; python-developer owns the file, web-developer reads it.

## Subtleties / gotchas
- The 8-char hex space is small (16⁸ ≈ 4.3B) — collision retry is a real path; tests cover it.
- `approve` / `revoke` / `invite` commit inline; if a caller has a wrapping transaction, double-commit will raise.
- `invite` is idempotent: re-inviting an already-paired user no-ops gracefully, returning the existing row.
- Console REPL and the bot's `/start` flow share the *same* pairing functions — output strings differ but the DB transitions are identical.
- (Planned, CCR-011) Two JWT kinds are stateless and 30-min TTL. Preview tokens carry `payload.port` which the proxy must check against the URL port — port-mismatch must reject. Never accept `algorithm="none"`. `verify_kind` is what consumers should call, not the bare `verify`.

## Cross-feature relations
- depends on: core (`ccr.db.models`, `ccr.db.engine`, `ccr.config.Settings`).
- used by: chat-bot (`/start`, allowlist middleware, owner-bootstrap), console (REPL pairing handlers), web-viewer (planned — JWT verify on `/auth` and proxy port check).

## Status
- State: IN PROGRESS
- Tickets: CCR-004, CCR-011
- Last updated: CCR-004 (2026-04-26)
