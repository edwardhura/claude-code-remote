"""Subprocess wrapper around ``claude -p --input-format=stream-json``.

One :class:`ClaudeProcess` owns one ``asyncio.subprocess.Process``; the
:class:`SessionManager` is the single permitted creator. Stdout is parsed
line-by-line into :data:`ClaudeEvent` objects via :func:`parse_event` —
schema drift or malformed JSON falls through to :class:`UnknownEvent`. A
sibling stderr-reader task keeps the last :data:`STDERR_TAIL_BYTES` bytes
in a ring buffer that the manager surfaces only on subprocess crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import signal
from typing import TYPE_CHECKING, Any

import structlog

from ccr.claude.events import ContentBlock, parse_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from ccr.config import Settings


log = structlog.get_logger(__name__)


STDERR_TAIL_BYTES = 8192


class ClaudeProcess:
    """Owns one ``claude`` subprocess.

    Lifecycle:

    1. :meth:`start` spawns the subprocess. Idempotent guard: a second call
       raises :class:`RuntimeError`.
    2. :meth:`events` is the single permitted reader of stdout; it yields
       parsed :data:`ClaudeEvent` objects until EOF.
    3. :meth:`send_user_turn` writes a JSONL line to stdin, followed by
       ``\\n`` and a drain.
    4. :meth:`stop` is idempotent: SIGTERM, wait up to ``grace`` seconds,
       SIGKILL, return the final exit code (or ``-1`` if already stopped).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        cwd: Path | None = None,
        resume: bool | str = False,
    ) -> None:
        self._settings = settings
        self._cwd = cwd
        self._resume: bool | str = resume
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_tail: bytearray = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._started = False
        self._stopped = False

    async def start(self) -> None:
        """Spawn the subprocess.

        Command line: ``claude -p --input-format=stream-json
        --output-format=stream-json --verbose`` plus any
        ``settings.claude_extra_args`` (whitespace-split via :mod:`shlex`).

        Raises :class:`RuntimeError` if called more than once on the same
        instance, or :class:`FileNotFoundError` if ``claude_bin`` is not
        on PATH (caller is expected to wrap that into a friendlier error).
        """
        if self._started:
            message = "ClaudeProcess.start() may only be called once."
            raise RuntimeError(message)
        self._started = True

        argv: list[str] = [
            self._settings.claude_bin,
            "-p",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--verbose",
        ]
        # Insert resume flag(s) after our fixed control flags but before any
        # user-supplied ``claude_extra_args`` so the user can override us by
        # appending. ``resume=False`` / ``resume=""`` produce no flag.
        if self._resume is True:
            argv.append("--continue")
        elif isinstance(self._resume, str) and self._resume:
            argv.extend(["--resume", self._resume])
        extra = (self._settings.claude_extra_args or "").strip()
        if extra:
            argv.extend(shlex.split(extra))

        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._cwd) if self._cwd is not None else None,
        )

        self._stderr_task = asyncio.create_task(
            self._drain_stderr(),
            name="claude-stderr-drain",
        )

    async def _drain_stderr(self) -> None:
        """Background task that keeps the last ``STDERR_TAIL_BYTES`` bytes."""
        if self._proc is None:  # pragma: no cover — only called after start()
            return
        stderr = self._proc.stderr
        if stderr is None:  # pragma: no cover — PIPE was requested
            return
        while True:
            chunk = await stderr.read(4096)
            if not chunk:
                return
            self._stderr_tail.extend(chunk)
            overflow = len(self._stderr_tail) - STDERR_TAIL_BYTES
            if overflow > 0:
                del self._stderr_tail[:overflow]

    async def send_user_turn(self, content: str | list[ContentBlock]) -> None:
        """Encode and write a user-turn line to stdin.

        Wire format::

            {"type": "user", "message": {"role": "user", "content": ...}}

        ``content`` may be a plain string or a list of
        :data:`ContentBlock` instances; each block is serialised via Pydantic's
        ``model_dump(mode="json")`` so discriminator fields and any extras
        round-trip.
        """
        if isinstance(content, list):
            content_payload: Any = [block.model_dump(mode="json") for block in content]
        else:
            content_payload = content
        payload: dict[str, Any] = {
            "type": "user",
            "message": {"role": "user", "content": content_payload},
        }
        await self._write_line(payload)

    async def _write_line(self, payload: dict[str, Any]) -> None:
        """Serialise ``payload`` as JSON, append ``\\n``, drain."""
        if self._proc is None or self._proc.stdin is None:
            message = "ClaudeProcess.start() has not been called."
            raise RuntimeError(message)
        if self._proc.stdin.is_closing():
            message = "ClaudeProcess stdin has been closed."
            raise RuntimeError(message)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    async def events(self) -> AsyncIterator[Any]:
        """Yield :data:`ClaudeEvent` objects parsed from stdout.

        Reads stdout in 4 KiB chunks, accumulates into a buffer, and only
        calls :func:`parse_event` for ``\\n``-terminated lines. A trailing
        partial line at EOF is logged at WARN and discarded. Empty lines
        are skipped silently.
        """
        if self._proc is None or self._proc.stdout is None:
            message = "ClaudeProcess.start() has not been called."
            raise RuntimeError(message)
        stdout = self._proc.stdout
        buf = bytearray()
        while True:
            chunk = await stdout.read(4096)
            if not chunk:
                if buf:
                    log.warning(
                        "claude_process.partial_line_at_eof",
                        size=len(buf),
                    )
                return
            buf.extend(chunk)
            while True:
                idx = buf.find(b"\n")
                if idx < 0:
                    break
                raw = bytes(buf[:idx])
                del buf[: idx + 1]
                if not raw.strip():
                    continue
                event = parse_event(raw)
                yield event

    async def stop(self, grace: float | None = None) -> int:
        """Idempotent shutdown.

        Order: SIGTERM → wait up to ``grace`` seconds → SIGKILL → wait for
        exit. Returns the final exit code, or ``-1`` if the process was
        already torn down (or never started).
        """
        if self._stopped:
            return -1
        if self._proc is None:
            self._stopped = True
            return -1

        wait = grace if grace is not None else self._settings.subprocess_grace_kill_seconds
        proc = self._proc

        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):  # pragma: no cover — race
                proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):  # pragma: no cover — race
                    proc.kill()
                await proc.wait()

        # Make sure the stderr drainer fully consumed remaining output.
        if self._stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError):  # pragma: no cover — defensive
                await self._stderr_task

        self._stopped = True
        return proc.returncode if proc.returncode is not None else -1

    @property
    def pid(self) -> int | None:
        """PID of the running subprocess, or ``None`` if not started / already stopped."""
        if self._proc is None:
            return None
        return self._proc.pid

    @property
    def stderr_tail(self) -> bytes:
        """Last :data:`STDERR_TAIL_BYTES` bytes of subprocess stderr."""
        return bytes(self._stderr_tail)

    @property
    def returncode(self) -> int | None:
        """Pass-through for the underlying ``Process.returncode``."""
        if self._proc is None:
            return None
        return self._proc.returncode

    async def wait(self) -> int:
        """Wait for the subprocess to exit; return its exit code."""
        if self._proc is None:
            message = "ClaudeProcess.start() has not been called."
            raise RuntimeError(message)
        rc = await self._proc.wait()
        if self._stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError):  # pragma: no cover — defensive
                await self._stderr_task
        return rc


__all__ = ["STDERR_TAIL_BYTES", "ClaudeProcess"]
