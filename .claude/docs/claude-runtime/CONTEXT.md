# Context: claude-runtime

## Files
- src/ccr/events/__init__.py — re-exports `EventBus` and `Subscriber`
- src/ccr/events/bus.py — `EventBus` with WeakSet-tracked subscriber queues, snapshot-iterating publish, drop-oldest backpressure with WARN, async-generator `subscribe()` with explicit `finally` discard
- src/ccr/claude/__init__.py — public surface re-exports for the claude subpackage
- src/ccr/claude/state.py — `SessionStatus(StrEnum)` with five states: IDLE, RUNNING, COMPLETED, STOPPED, CRASHED
- src/ccr/claude/events.py — Pydantic v2 schema; `ClaudeEvent` plain Union over `_KnownEvent` discriminated union + `UnknownEvent`; `parse_event()` with double-fallback; `ContentBlock` inner/outer variants (text, thinking, tool_use, tool_result); `ResultUsage` sub-model with optional `usage` field on `ResultEvent`; `PermissionRequest` variant removed (CCR-024)
- src/ccr/claude/process.py — `ClaudeProcess`: idempotent `start()`/`stop()` with SIGTERM+SIGKILL grace, stderr ring buffer (8192 bytes), line-buffered async generator, `send_user_turn()` writer; public read-only `pid` property; `send_permission_response()` removed (CCR-024)
- src/ccr/claude/log.py — `JsonlSessionLog` with `append()`, `read_from(seq)`, `tail()` (asyncio.Event-notified); module-level `prune()` for retention-count and age-based cleanup
- src/ccr/claude/manager.py — `SessionManager`: single asyncio lock enforcing one-session-at-a-time invariant, lifecycle FSM, log-before-publish ordering, synthetic crash event on abnormal exit; `info()` async method returning `{session_id, pid, started_at, status}`; error classes `SessionError`, `NoActiveSessionError`, `StaleSessionError`; permission-gating dicts/accessors/helpers/`send_permission` removed (CCR-024)
- src/ccr/bot/formatting.py — `event_to_messages()` returning `OutboundMessage = tuple[str, InlineKeyboardMarkup | None]`; `_PendingKeyboard` sentinel removed; `PermissionRequest` branch removed (CCR-024)
- src/ccr/bot/handlers/permission.py — `cb_permission` body stubbed as `raise NotImplementedError("MCP integration pending — see CCR-025")`; `_parse_callback` helper and `router` registration preserved dormant for CCR-025
- src/ccr/bot/keyboards.py — `permission_kb` keyboard widget preserved dormant for CCR-025 (docstring updated to point at CCR-025)
- src/ccr/server.py — `_broadcast_loop` simplified: no pause/buffer/drain logic; `_materialise_keyboards` / `_flush_buffer` removed (CCR-024)
- tests/fakes/__init__.py — package marker
- tests/fakes/fake_claude.py — env-var-driven JSONL emitter replacing the real claude binary in tests
- tests/fakes/fake_claude — POSIX shell shim (0o755) that invokes fake_claude.py
- tests/test_event_bus.py — 8 tests: pub/sub, slow-consumer WARN, weakref GC, topic isolation, cancel safety
- tests/test_claude_events.py — 12 tests: variant round-trips, JSON schema non-empty, parse-error fallback, schema-drift fallback, extra=allow round-trip
- tests/test_claude_log.py — 13 tests: sequential seq, resume, in-order read, EOF boundary, tail, concurrent tailers, cancel safety, prune (retention-count, age-based, malformed line, idempotent open, parent-dir creation, missing-dir prune)
- tests/test_session_manager.py — lifecycle, crash detection, idempotent stop, new_session-while-running, send-without-session, first_prompt truncation, last_event_at debounce (CCR-009 permission-gating tests + stale send_permission test removed in CCR-024)

## Relations
- depends on: core
- used by: chat-bot (CCR-008, CCR-009, CCR-010), web-viewer (CCR-012, CCR-013, CCR-015)

## Change history
- [CCR-007]: implemented EventBus, ClaudeEvent discriminated-union schema, ClaudeProcess subprocess wrapper, JsonlSessionLog JSONL logger, SessionManager single-session enforcer, and full fake-claude test harness
- [CCR-018]: added ResultUsage sub-model + optional usage field on ResultEvent; added ClaudeProcess.pid property; added SessionManager.info() snapshot method with started_at tracking
- [CCR-009]: added permission gating to SessionManager — four gating dicts, is_telegram_paused/wait_for_resume/is_permission_choice_valid accessors, _record_pending_permission/_clear_pending_permission helpers, teardown clears gate state and wakes orphaned resume Events; process.py TODO(CCR-009) updated to TODO(CCR-019)
- [CCR-024]: removed dead CCR-009 permission-gating code (`PermissionRequest` event variant, `send_permission_response` writer, `SessionManager` gating dicts/accessors/helpers + `send_permission`, `_PendingKeyboard` sentinel, `_broadcast_loop` pause/buffer/drain, `_materialise_keyboards`/`_flush_buffer`); kept `keyboards.permission_kb`, `handlers.permission._parse_callback` + router registration, and `OutboundMessage` tuple shape dormant for CCR-025; `cb_permission` body stubbed to raise `NotImplementedError`
