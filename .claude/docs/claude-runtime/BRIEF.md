# Brief: claude-runtime

## Overview
The `claude-runtime` feature provides the core subprocess and event infrastructure that all higher-level features depend on. It delivers three cooperating subsystems: an in-process `EventBus` for fan-out pub/sub between the bot, the web layer, and the session manager; a Pydantic v2 discriminated-union event schema (`ClaudeEvent`) with drift-tolerant `UnknownEvent` fallback; and `SessionManager`, which enforces the single-running-session invariant, owns the `ClaudeProcess` lifecycle (SIGTERM+SIGKILL graceful shutdown), appends every event to an append-only `JsonlSessionLog` before publishing to the bus (preserving the SSE replay-then-tail invariant), and transitions `Session` rows to `COMPLETED`, `STOPPED`, or `CRASHED` on subprocess exit. A fake-claude test harness drives the full lifecycle without invoking the real binary.

## Files
- src/ccr/events/__init__.py — re-exports `EventBus` and `Subscriber`
- src/ccr/events/bus.py — `EventBus` with WeakSet-tracked subscriber queues, snapshot-iterating publish, drop-oldest backpressure with WARN, async-generator `subscribe()` with explicit `finally` discard
- src/ccr/claude/__init__.py — public surface re-exports for the claude subpackage
- src/ccr/claude/state.py — `SessionStatus(StrEnum)` with five states: IDLE, RUNNING, COMPLETED, STOPPED, CRASHED
- src/ccr/claude/events.py — Pydantic v2 schema; `ClaudeEvent` plain Union over `_KnownEvent` discriminated union + `UnknownEvent`; `parse_event()` with double-fallback; `ContentBlock` inner/outer variants
- src/ccr/claude/process.py — `ClaudeProcess`: idempotent `start()`/`stop()` with SIGTERM+SIGKILL grace, stderr ring buffer, line-buffered async generator, user-turn and permission-response writers
- src/ccr/claude/log.py — `JsonlSessionLog` with `append()`, `read_from(seq)`, `tail()`; module-level `prune()` for retention-count and age-based cleanup
- src/ccr/claude/manager.py — `SessionManager`: single asyncio lock, lifecycle FSM, log-before-publish ordering, synthetic crash event; error classes `SessionError`, `NoActiveSessionError`, `StaleSessionError`
- tests/fakes/__init__.py — package marker
- tests/fakes/fake_claude.py — env-var-driven JSONL emitter replacing the real claude binary in tests
- tests/fakes/fake_claude — POSIX shell shim (0o755) invoking fake_claude.py
- tests/test_event_bus.py — 8 EventBus tests
- tests/test_claude_events.py — 12 event-schema tests
- tests/test_claude_log.py — 13 JSONL-log tests
- tests/test_session_manager.py — 8 SessionManager tests

Status: IN PROGRESS
Tickets: CCR-007, CCR-024, CCR-025, CCR-028, CCR-029, CCR-036
