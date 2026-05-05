# Context: auth

## Files
- src/ccr/auth/__init__.py — re-exports the public pairing + allowlist surface.
- src/ccr/auth/pairing.py — pure async pairing operations (`create_code`, `list_pending`, `list_paired`, `get_owner`, `approve`, `revoke`, `invite`) and domain errors `PairingError` / `CannotRevokeOwnerError`. Functions take an `AsyncSession` first; `approve`/`revoke`/`invite` commit inline.
- src/ccr/auth/allowlist.py — `is_paired` and `is_owner` predicates filtering on `revoked_at IS NULL`.
- src/ccr/cli.py — wires `pair {list,pending,approve,revoke,invite}` subcommands; opens a fresh `AsyncSession` per command via `Settings()` + `create_engine_from_settings`. Output strings (`"(empty)"`, `"Approved Telegram user <id> (owner|paired)"`, `"Cannot revoke owner."`) are pinned by the acceptance tests.
- tests/test_pairing.py — pure-function lifecycle tests (collision retry, owner auto-promotion, expired/reused codes rejected, revoke owner refused, invite happy path) plus subprocess-driven CLI tests using a temp `DATA_DIR` and hand-seeded `pairing_codes` rows.

## Relations
- depends on: core (`ccr.db.models`, `ccr.db.engine`, `ccr.config.Settings`).
- used by: chat-bot (CCR-006 onward — `/start` will call `create_code` / `notify_owner`), console (CCR-005 — REPL handlers reuse the same async pairing functions), web-viewer (CCR-011 — JWT module will live alongside in `src/ccr/auth/tokens.py`).

## Change history
- [CCR-004]: Added the auth package: `pairing.py` (8-char-hex codes with 5x collision retry, owner auto-promotion via partial unique index, soft-revoke with owner guard, idempotent `invite`), `allowlist.py` (`is_paired`/`is_owner`), wired the `ccr pair {list,pending,approve,revoke,invite}` CLI subcommands with column-aligned output, and full lifecycle tests (`tests/test_pairing.py`).
