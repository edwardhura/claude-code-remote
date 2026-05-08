"""Lifecycle-status enum for the global Claude subprocess session.

CCR-044 promoted ``IDLE`` from an in-memory-only sentinel to a real persisted
SQL value. ``Session.status`` now accepts five values:
``idle | running | completed | stopped | crashed``. The lifecycle is:

* ``INSERT idle`` — fresh ``/new`` (or plain-text first prompt) inserts a
  :class:`Session` row with ``status='idle'`` and ``claude_session_id IS
  NULL``. The row exists, but it is not yet addressable via ``/continue``
  or ``/sessions`` — both filter idle rows out.
* ``UPDATE running`` — the same fire-and-forget task that persists
  ``claude_session_id`` from the first ``SystemInit`` event also flips
  ``status`` to ``'running'`` in a single atomic UPDATE
  (:meth:`SessionManager._update_claude_session_id`).
* ``finalize completed | stopped | crashed`` — terminal states are written
  by :meth:`SessionManager._db_finalize_session` (clean exit / explicit
  ``/stop``) or :meth:`SessionManager.reconcile_orphans` (bot restart).
  Finalize is never called with ``IDLE`` — the guard inside
  ``_db_finalize_session`` enforces this.

Imports (``import_claude_session``) bypass ``idle`` and insert directly with
``status='stopped'`` because the foreign session arrives with a known Claude
id and a known terminal lifetime.
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
