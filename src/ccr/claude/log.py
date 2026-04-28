"""Per-session append-only JSONL log + retention pruning.

Sequence numbers are line indices (0-based). The :class:`JsonlSessionLog`
serialises appends with a single :class:`asyncio.Lock` and notifies tailers
through a shared :class:`asyncio.Event`. The notify design satisfies three
properties (see plan §"_notify invariants"):

1. **No missed wakeup.** :meth:`tail` snapshots ``_seq`` *before* the first
   ``_notify.wait()`` so an append that runs between snapshot and wait is
   still drained on the first pass.
2. **Multiple concurrent tailers.** ``Event.set()`` wakes all waiters; each
   tailer manages its own snapshot of progress.
3. **Cancel safety.** The file handle is reopened per drain pass and closed
   before the next ``await`` so cancellation never leaks an FD.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from ccr.claude.events import parse_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


log = structlog.get_logger(__name__)


class JsonlSessionLog:
    """Append-only JSONL file with sequence numbers and a tail() iterator."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._notify = asyncio.Event()
        self._seq = 0
        self._opened = False

    @property
    def path(self) -> Path:
        """Filesystem path of the JSONL log."""
        return self._path

    @property
    def seq(self) -> int:
        """Current sequence number (== number of appended lines)."""
        return self._seq

    async def open(self) -> None:
        """Compute initial ``_seq`` from any existing file. Idempotent."""
        if self._opened:
            return
        self._opened = True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()
            self._seq = 0
            return
        count = 0
        with self._path.open("rb") as fh:
            for _ in fh:
                count += 1
        self._seq = count

    async def append(self, event: Any) -> int:
        """Append one event as a JSON line. Returns the assigned seq."""
        if not self._opened:
            await self.open()
        line = event.model_dump_json() + "\n"
        async with self._lock:
            assigned = self._seq
            with self._path.open("ab") as fh:
                fh.write(line.encode("utf-8"))
                fh.flush()
            self._seq += 1
            self._notify.set()
        return assigned

    async def read_from(self, seq: int) -> AsyncIterator[tuple[int, Any]]:
        """Yield ``(index, event)`` pairs starting at line ``seq``.

        Stops at the current EOF; cancel-safe: the file handle is closed on
        cancellation and on normal exit.
        """
        if not self._opened:
            await self.open()
        seq = max(seq, 0)

        fh = self._path.open("rb")
        try:
            current = 0
            while True:
                raw = fh.readline()
                if not raw:
                    return
                if current >= seq:
                    text = raw.decode("utf-8", errors="replace").rstrip("\n")
                    if text.strip():
                        yield current, parse_event(text)
                current += 1
        finally:
            fh.close()

    async def tail(self) -> AsyncIterator[tuple[int, Any]]:
        """Yield events appended after this call begins.

        Multiple concurrent ``tail()`` callers are supported; each clears
        the shared ``_notify`` event independently.
        """
        if not self._opened:
            await self.open()

        snapshot = self._seq
        while True:
            # Clear *before* each drain pass so a concurrent append between
            # the drain and the next wait still wakes us.
            self._notify.clear()
            # Drain everything from snapshot to current EOF.
            fh = self._path.open("rb")
            try:
                current = 0
                while True:
                    raw = fh.readline()
                    if not raw:
                        break
                    if current >= snapshot:
                        text = raw.decode("utf-8", errors="replace").rstrip("\n")
                        if text.strip():
                            yield current, parse_event(text)
                            snapshot = current + 1
                        else:
                            snapshot = current + 1
                    current += 1
            finally:
                fh.close()

            # Re-check current state under the lock-free assumption: if new
            # lines arrived between drain and wait, _notify will already
            # be set so wait() returns immediately.
            await self._notify.wait()


def prune(
    logs_dir: Path,
    *,
    retention_count: int,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Startup-time cleanup of ``data/logs/``.

    Keeps the last ``retention_count`` ``.jsonl`` files by mtime; deletes
    any file with mtime older than ``retention_days``. Returns the number
    of files deleted.

    .. warning::

       Must NOT be called concurrently with :meth:`JsonlSessionLog.append`
       on a file under ``logs_dir``. Intended to run at startup, before any
       new session is spawned.
    """
    if not logs_dir.exists() or not logs_dir.is_dir():
        return 0

    files = sorted(
        (p for p in logs_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    cutoff_now = now if now is not None else datetime.now(UTC)
    cutoff_ts = cutoff_now.timestamp() - (retention_days * 86400)

    deleted = 0
    keep_count = max(retention_count, 0)

    for index, path in enumerate(files):
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:  # pragma: no cover — race
            continue
        is_over_count = index >= keep_count
        is_over_age = mtime < cutoff_ts
        if is_over_count or is_over_age:
            try:
                path.unlink()
            except FileNotFoundError:  # pragma: no cover — race
                continue
            deleted += 1

    if deleted:
        log.info(
            "claude_log.prune",
            logs_dir=str(logs_dir),
            deleted=deleted,
            retention_count=retention_count,
            retention_days=retention_days,
        )
    return deleted


__all__ = ["JsonlSessionLog", "prune"]
