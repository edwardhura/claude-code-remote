"""Document ``idle`` as a valid persisted ``Session.status`` value.

Revision ID: 0006_add_idle_to_session_status
Revises: 0005_drop_sessions_claude_session_id_unique
Create Date: 2026-05-08

CCR-044 promoted ``SessionStatus.IDLE`` from an in-memory-only sentinel to a
real persisted SQL value. ``Session.status`` is declared as ``String(16)``
with no CHECK constraint (see :class:`ccr.db.models.Session`), so the column
already accepts any string the application writes — there is nothing to
ALTER. The lifecycle is:

* ``INSERT status='idle'`` with ``claude_session_id IS NULL`` on every fresh
  ``/new`` (and plain-text first-prompt) call.
* ``UPDATE status='running'`` in the single atomic UPDATE that also persists
  ``claude_session_id`` from the first observed ``SystemInit`` event
  (:meth:`SessionManager._update_claude_session_id`).
* ``UPDATE status='completed'|'stopped'|'crashed'`` on finalize / restart
  reconciliation.

No backfill: pre-existing rows where ``claude_session_id IS NULL`` keep
their existing terminal status (``stopped`` / ``completed`` / ``crashed``)
and remain non-addressable via ``/continue`` / ``/rename`` exactly as
today. Pre-CCR-044 rows are legacy artefacts; reading them does not
require the new ``idle`` value to exist on disk historically. The
:func:`upgrade` and :func:`downgrade` bodies are intentionally ``pass``
because the schema is unchanged — this migration exists only so the
revision graph records that ``idle`` is now a valid persisted value at
the application level.
"""

from __future__ import annotations

revision: str = "0006_add_idle_to_session_status"
down_revision: str | None = "0005_drop_sessions_claude_session_id_unique"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
