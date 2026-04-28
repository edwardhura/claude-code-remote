"""In-memory session-status enum.

``IDLE`` is intentionally absent from ``Session.status`` SQL writes — the SQL
column accepts ``running | completed | stopped | crashed`` only. ``IDLE`` is
the in-memory state that :meth:`SessionManager.status` returns when no
subprocess is active.
"""

from __future__ import annotations

from enum import StrEnum


class SessionStatus(StrEnum):
    """Lifecycle states for the global Claude subprocess session."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    CRASHED = "crashed"


__all__ = ["SessionStatus"]
