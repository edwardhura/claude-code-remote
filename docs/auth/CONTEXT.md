# Context: auth

## Files
- src/ccr/auth/__init__.py — re-exports the public pairing + allowlist surface, now also including `TokenError`, `TokenKind`, `VerifiedToken`, `mint`, `verify`, `verify_kind` (alphabetised in `__all__`).
- src/ccr/auth/pairing.py — pure async pairing operations (`create_code`, `list_pending`, `list_paired`, `get_owner`, `approve`, `revoke`, `invite`) and domain errors `PairingError` / `CannotRevokeOwnerError`. Functions take an `AsyncSession` first; `approve`/`revoke`/`invite` commit inline.
- src/ccr/auth/allowlist.py — `is_paired` and `is_owner` predicates filtering on `revoked_at IS NULL`.
- src/ccr/auth/tokens.py — JWT mint/verify module: `TokenKind` StrEnum (`viewer`/`preview`), `TokenError`, `VerifiedToken` (frozen dataclass), `mint`, `verify`, `verify_kind`. HS256-signed, stateless, default 30-min TTL from `Settings.token_ttl_seconds`.
- src/ccr/cli.py — wires `pair {list,pending,approve,revoke,invite}` subcommands; opens a fresh `AsyncSession` per command via `Settings()` + `create_engine_from_settings`. Output strings (`"(empty)"`, `"Approved Telegram user <id> (owner|paired)"`, `"Cannot revoke owner."`) are pinned by the acceptance tests.
- tests/test_pairing.py — pure-function lifecycle tests (collision retry, owner auto-promotion, expired/reused codes rejected, revoke owner refused, invite happy path) plus subprocess-driven CLI tests using a temp `DATA_DIR` and hand-seeded `pairing_codes` rows.
- tests/test_tokens.py — 14 tests covering mint/verify round-trip, all six JWT claims, expired token rejection, tampered signature rejection, `alg=none` forgery, `verify_kind` kind mismatch, and per-claim validation (missing claim, non-integer sub, unknown kind, non-object payload, empty jti). 100% coverage on `tokens.py`.

## Relations
- depends on: core (`ccr.db.models`, `ccr.db.engine`, `ccr.config.Settings`), pyjwt (already in `[project.dependencies]`).
- used by: chat-bot (CCR-006 onward — `/start` will call `create_code` / `notify_owner`; CCR-014 will call `mint` for `/view`/`/last`/`/preview` URL minting), console (CCR-005 — REPL handlers reuse the same async pairing functions), web-viewer (CCR-012 — `verify_kind` consumed by `/auth` cookie handoff and proxy port check).

## Change history
- [CCR-004]: Added the auth package: `pairing.py` (8-char-hex codes with 5x collision retry, owner auto-promotion via partial unique index, soft-revoke with owner guard, idempotent `invite`), `allowlist.py` (`is_paired`/`is_owner`), wired the `ccr pair {list,pending,approve,revoke,invite}` CLI subcommands with column-aligned output, and full lifecycle tests (`tests/test_pairing.py`).
- [CCR-011]: Added `tokens.py` (HS256 JWT mint/verify, `TokenKind`, `TokenError`, `VerifiedToken`, `mint`/`verify`/`verify_kind`, stateless 30-min TTL, `alg=none` defence-in-depth, per-claim validation); updated `__init__.py` to re-export the new symbols; added `tests/test_tokens.py` (14 tests, 100% coverage on `tokens.py`).
