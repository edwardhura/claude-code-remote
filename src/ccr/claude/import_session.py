"""Import an existing local Claude session into the CCR DB.

Pure async helpers, no global state. The CLI (``ccr session save``) and the
REPL (``ccr console`` -> ``session save``) both call
:func:`import_claude_session` through their own thin wrappers, mirroring the
dual-entrypoint pattern that :func:`ccr.auth.pairing.approve` already serves.

Why scan-by-id rather than encode-then-lookup
---------------------------------------------

Claude stores its per-session JSONL at
``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`` where ``<encoded-cwd>``
is the absolute filesystem path with every ``/`` replaced by ``-``. On macOS
the canonicalisation step that runs *before* encoding can resolve
``/tmp`` -> ``/private/tmp``, so a round-trip ``encoded_cwd_for(os.getcwd())``
will not always match the on-disk directory. The import function therefore
scans every ``<projects_root>/*/`` directory for ``<session_id>.jsonl`` rather
than computing a single expected path. The session id is a UUID4 so cross-dir
collisions are not a real concern.

JSONL parsing rules
-------------------

- Lines that fail to JSON-decode are silently skipped (drift-tolerance posture
  matching :func:`ccr.claude.events.parse_event`).
- The very first line is sometimes a ``type: "file-history-snapshot"`` whose
  ``timestamp`` is nested rather than top-level. We only honour the FIRST line
  that carries a top-level ``timestamp``; the first ``type: "user"`` event
  always has one.
- If no top-level ``timestamp`` is found in the entire file, ``started_at``
  falls back to ``datetime.now(UTC)`` and a structured warning is logged. The
  import is NOT aborted — the row is still useful for ``/continue``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from ccr.auth.pairing import get_owner
from ccr.claude.state import SessionStatus
from ccr.db.models import Session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


log = structlog.get_logger(__name__)


_DEFAULT_PROJECTS_ROOT = Path("~/.claude/projects").expanduser()


# Strict UUID4 pattern. Claude session ids are UUID4s and the regex doubles as
# a path-traversal guard: ``/``, ``\``, and ``..`` are all rejected before the
# id is interpolated into ``<projects_root>/<dir>/<id>.jsonl``. CCR-036 F3.
_CLAUDE_SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


class ImportSessionError(Exception):
    """Base class for :func:`import_claude_session` failures."""


class ClaudeSessionFileNotFoundError(ImportSessionError):
    """Raised when no ``~/.claude/projects/*/<id>.jsonl`` matches the id."""


class DuplicateClaudeSessionError(ImportSessionError):
    """Raised when a row with this ``claude_session_id`` already exists."""


class NoOwnerError(ImportSessionError):
    """Raised when no owner exists in ``paired_users``.

    ``started_by_tg_user_id`` requires a Telegram user context, and an
    offline imported session has none. We seed it from the registered
    owner; if the owner is missing we fail rather than insert a row with a
    bogus user id.
    """


def encoded_cwd_for(path: Path) -> str:
    """Encode an absolute filesystem path into Claude's project-dir naming.

    Format observed in ``~/.claude/projects/``: replace every ``/`` with ``-``
    (so ``/Users/edward/foo`` -> ``-Users-edward-foo``). The leading ``/``
    becomes a leading ``-``.

    Documented for completeness — :func:`find_claude_session_file` does NOT
    use this for its lookup because macOS path canonicalisation
    (``/tmp`` -> ``/private/tmp``) can break the round-trip. Scan-by-id is
    the settled approach for the actual file lookup.
    """
    return str(path).replace("/", "-")


def find_claude_session_file(
    claude_session_id: str,
    *,
    projects_root: Path | None = None,
) -> Path:
    """Return the JSONL path for ``claude_session_id``, scanning every project dir.

    Default ``projects_root`` is ``~/.claude/projects/``. The function does
    NOT require the caller to know which ``cwd`` Claude used — it walks every
    ``<projects_root>/*/`` directory and returns the first match for
    ``<claude_session_id>.jsonl``. Raises
    :class:`ClaudeSessionFileNotFoundError` on miss.

    The id is validated against a strict UUID4 pattern *before* any filesystem
    access. This both matches the actual format Claude writes and rejects
    anything containing ``/``, ``\\``, or ``..`` so a malicious owner-supplied
    argument cannot escape ``projects_root``. An invalid id raises
    :class:`ImportSessionError`.
    """
    if not _CLAUDE_SESSION_ID_RE.fullmatch(claude_session_id):
        message = "Invalid Claude session id format"
        raise ImportSessionError(message)
    root = projects_root if projects_root is not None else _DEFAULT_PROJECTS_ROOT
    if not root.is_dir():
        message = f"No local Claude session found with id {claude_session_id}"
        raise ClaudeSessionFileNotFoundError(message)
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        candidate = entry / f"{claude_session_id}.jsonl"
        if candidate.is_file():
            return candidate
    message = f"No local Claude session found with id {claude_session_id}"
    raise ClaudeSessionFileNotFoundError(message)


def _parse_started_at(jsonl_path: Path) -> datetime:
    """Read ``jsonl_path`` once and return the first top-level ``timestamp``.

    Falls back to ``datetime.now(UTC)`` (with a structured warning) when no
    top-level ``timestamp`` is found in the entire file. Lines that fail to
    JSON-decode are silently skipped.
    """
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = obj.get("timestamp")
            if not isinstance(ts, str):
                continue
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                continue
    log.warning(
        "import_session.no_timestamp",
        jsonl_path=str(jsonl_path),
    )
    return datetime.now(UTC)


async def import_claude_session(
    db: AsyncSession,
    claude_session_id: str,
    *,
    projects_root: Path | None = None,
) -> Session:
    """Read Claude's local JSONL for ``claude_session_id`` and write a row.

    Steps:

    1. :func:`find_claude_session_file` — raise :class:`ClaudeSessionFileNotFoundError`
       on miss.
    2. Stream the JSONL once, capturing the first line that carries a top-level
       ``timestamp`` -> ``started_at``. (The first ``type: "user"`` line always
       has one; ``type: "file-history-snapshot"`` lines are skipped.) Lines that
       fail to JSON-decode are silently skipped. If no top-level ``timestamp``
       is found, ``started_at`` falls back to ``datetime.now(UTC)`` with a
       warning — the import is NOT aborted.
    3. Look up the owner via :func:`ccr.auth.pairing.get_owner` -> raise
       :class:`NoOwnerError` if ``None``.
    4. SELECT-before-INSERT duplicate guard: query for any existing
       :class:`Session` row sharing ``claude_session_id`` and raise
       :class:`DuplicateClaudeSessionError(claude_session_id)` on hit.
       CCR-041 dropped the DB-side UNIQUE flag on
       ``ix_sessions_claude_session_id_not_null`` because resume chains
       legitimately produce multiple rows pointing at the same Claude
       conversation; the import-time double-write guard now lives here.
    5. Insert a :class:`Session` row with ``claude_session_id``, ``started_at``,
       ``started_by_tg_user_id=owner.tg_user_id``, ``status="stopped"``.

    Returns the persisted :class:`Session`.

    Does NOT copy the JSONL into ``data/logs/`` — Claude owns its history.
    The CCR-side ``data/logs/<our_uuid>.jsonl`` is only created when a
    continuation actually streams (``SessionManager.continue_session``).

    Does NOT write ``first_prompt`` or ``name``: the foreign session's first
    user prompt may be a slash command or a multi-block content, and the
    column is documented as "the prompt that started OUR session". CCR-037
    handles ``name`` for going-forward sessions and explicitly leaves
    imported rows NULL.
    """
    jsonl_path = find_claude_session_file(
        claude_session_id,
        projects_root=projects_root,
    )
    started_at = _parse_started_at(jsonl_path)

    owner = await get_owner(db)
    if owner is None:
        message = "No owner registered. Pair the owner first."
        raise NoOwnerError(message)

    existing = await db.scalar(
        select(Session).where(Session.claude_session_id == claude_session_id),
    )
    if existing is not None:
        raise DuplicateClaudeSessionError(claude_session_id)

    # CCR-044: imported sessions never pass through ``idle`` — they arrive
    # with a non-NULL ``claude_session_id`` and a known terminal lifetime,
    # so they go straight to ``stopped``. ``idle`` is reserved for fresh
    # ``/new`` rows whose Claude id is still unknown.
    row = Session(
        started_at=started_at,
        status=SessionStatus.STOPPED.value,
        started_by_tg_user_id=owner.tg_user_id,
        claude_session_id=claude_session_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


__all__ = [
    "ClaudeSessionFileNotFoundError",
    "DuplicateClaudeSessionError",
    "ImportSessionError",
    "NoOwnerError",
    "encoded_cwd_for",
    "find_claude_session_file",
    "import_claude_session",
]
