# Context: claude-runtime

## Files
- src/ccr/events/__init__.py — re-exports `EventBus` and `Subscriber`
- src/ccr/events/bus.py — `EventBus` with WeakSet-tracked subscriber queues, snapshot-iterating publish, drop-oldest backpressure with WARN, async-generator `subscribe()` with explicit `finally` discard
- src/ccr/claude/__init__.py — public surface re-exports for the claude subpackage
- src/ccr/claude/state.py — `SessionStatus(StrEnum)` with five states: IDLE, RUNNING, COMPLETED, STOPPED, CRASHED
- src/ccr/claude/events.py — Pydantic v2 schema; `ClaudeEvent` plain Union over `_KnownEvent` discriminated union + `UnknownEvent`; `parse_event()` with double-fallback; `ContentBlock` inner/outer variants (text, thinking, tool_use, tool_result); `ResultUsage` sub-model with optional `usage` field on `ResultEvent`
- src/ccr/claude/process.py — `ClaudeProcess`: idempotent `start()`/`stop()` with SIGTERM+SIGKILL grace, stderr ring buffer (8192 bytes), line-buffered async generator, `send_user_turn()` / `send_permission_response()` writers; public read-only `pid` property
- src/ccr/claude/log.py — `JsonlSessionLog` with `append()`, `read_from(seq)`, `tail()` (asyncio.Event-notified); module-level `prune()` for retention-count and age-based cleanup
- src/ccr/claude/manager.py — `SessionManager`: single asyncio lock enforcing one-session-at-a-time invariant, lifecycle FSM, log-before-publish ordering, synthetic crash event on abnormal exit; `info()` async method returning `{session_id, pid, started_at, status}`; error classes `SessionError`, `NoActiveSessionError`, `StaleSessionError`
- tests/fakes/__init__.py — package marker
- tests/fakes/fake_claude.py — env-var-driven JSONL emitter replacing the real claude binary in tests
- tests/fakes/fake_claude — POSIX shell shim (0o755) that invokes fake_claude.py
- tests/test_event_bus.py — 8 tests: pub/sub, slow-consumer WARN, weakref GC, topic isolation, cancel safety
- tests/test_claude_events.py — 12 tests: variant round-trips, JSON schema non-empty, parse-error fallback, schema-drift fallback, extra=allow round-trip
- tests/test_claude_log.py — 13 tests: sequential seq, resume, in-order read, EOF boundary, tail, concurrent tailers, cancel safety, prune (retention-count, age-based, malformed line, idempotent open, parent-dir creation, missing-dir prune)
- tests/test_session_manager.py — 8 tests: lifecycle, crash detection, idempotent stop, new_session-while-running, send-without-session, stale send_permission, first_prompt truncation, last_event_at debounce

## Relations
- depends on: core
- used by: chat-bot (CCR-008, CCR-009, CCR-010), web-viewer (CCR-012, CCR-013, CCR-015)

## Change history
- [CCR-007]: implemented EventBus, ClaudeEvent discriminated-union schema, ClaudeProcess subprocess wrapper, JsonlSessionLog JSONL logger, SessionManager single-session enforcer, and full fake-claude test harness
- [CCR-018]: added ResultUsage sub-model + optional usage field on ResultEvent; added ClaudeProcess.pid property; added SessionManager.info() snapshot method with started_at tracking
