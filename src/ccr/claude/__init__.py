"""Claude subprocess wrapper, event schema, JSONL log, session manager.

Public surface consumed by downstream tickets (CCR-008 chat-bot, CCR-009
permission gating, CCR-010 slash passthrough, CCR-012 web SSE):

* :class:`SessionManager` — globally enforces a single running Claude
  session; owns a :class:`ClaudeProcess`, a :class:`JsonlSessionLog`, the
  consumer + exit tasks, and the DB write side.
* :class:`SessionStatus` — lifecycle states (in-memory ``IDLE`` plus the
  four SQL-persisted states).
* :data:`ClaudeEvent` / :data:`ContentBlock` — Pydantic v2 discriminated
  unions; consumers pattern-match on ``event.type``.
* :class:`JsonlSessionLog` — read-from-seq + tail iteration for SSE replay.
* :class:`SessionError` / :class:`NoActiveSessionError` /
  :class:`StaleSessionError` — error hierarchy.
"""

from __future__ import annotations

from ccr.claude.events import ClaudeEvent, ContentBlock
from ccr.claude.log import JsonlSessionLog
from ccr.claude.manager import (
    NoActiveSessionError,
    SessionError,
    SessionManager,
    StaleSessionError,
)
from ccr.claude.state import SessionStatus

__all__ = [
    "ClaudeEvent",
    "ContentBlock",
    "JsonlSessionLog",
    "NoActiveSessionError",
    "SessionError",
    "SessionManager",
    "SessionStatus",
    "StaleSessionError",
]
