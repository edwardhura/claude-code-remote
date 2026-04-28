"""Fake `claude` binary for SessionManager tests.

Reads JSONL from stdin (so writes by ``send_user_turn`` /
``send_permission_response`` are observable in tests), emits canned JSONL
to stdout based on env-var directives, optionally writes to stderr, and
exits with a configurable code.

Env directives:

* ``FAKE_CLAUDE_SCRIPT`` — path to a JSONL file whose lines are emitted to
  stdout. Each line is a self-contained event.
* ``FAKE_CLAUDE_DELAY_MS`` — per-line delay (default ``0``).
* ``FAKE_CLAUDE_EXIT_CODE`` — final exit code (default ``0``).
* ``FAKE_CLAUDE_ABORT_AFTER`` — exit after emitting N lines (simulates
  crash). When set, the exit code is ``139`` unless overridden by
  ``FAKE_CLAUDE_EXIT_CODE``.
* ``FAKE_CLAUDE_STDERR`` — text written to stderr before exit.

Run via ``python -m tests.fakes.fake_claude`` or the ``tests/fakes/fake_claude``
shell shim.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def main() -> int:
    script_path = os.environ.get("FAKE_CLAUDE_SCRIPT", "")
    delay_ms = _env_int("FAKE_CLAUDE_DELAY_MS", 0)
    abort_after_raw = os.environ.get("FAKE_CLAUDE_ABORT_AFTER")
    abort_after = int(abort_after_raw) if abort_after_raw else None
    exit_code = _env_int("FAKE_CLAUDE_EXIT_CODE", 0)
    stderr_text = os.environ.get("FAKE_CLAUDE_STDERR", "")

    lines: list[str] = []
    if script_path:
        path = Path(script_path)
        if path.exists():
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    emitted = 0
    for line in lines:
        if abort_after is not None and emitted >= abort_after:
            break
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        emitted += 1
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

    if stderr_text:
        sys.stderr.write(stderr_text)
        sys.stderr.flush()

    if (
        abort_after is not None
        and emitted >= abort_after
        and "FAKE_CLAUDE_EXIT_CODE" not in os.environ
    ):
        # Simulate a crash: nonzero exit unless caller overrode.
        return 139

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
