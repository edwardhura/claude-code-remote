"""Tests for the argparse skeleton in :mod:`ccr.cli`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import ccr.server as server_mod
from ccr.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ccr", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def test_help_exits_zero_and_lists_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr().out
    for expected in ("serve", "console", "pair", "doctor", "init-db"):
        assert expected in captured


def test_unknown_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["does-not-exist"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err or "does-not-exist" in err


def test_no_subcommand_exits_two() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2


def test_pair_without_subcommand_exits_two() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["pair"])
    assert exc_info.value.code == 2


def test_serve_dispatches_to_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ccr serve` should hand off to ccr.server.serve via asyncio.run."""
    captured: dict[str, object] = {}

    def fake_run(coro: object) -> None:
        captured["ran"] = True
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[attr-defined]

    async def fake_serve(settings: object) -> None:  # pragma: no cover - never awaited
        captured["settings"] = settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("PUBLIC_URL", "https://example.test")
    monkeypatch.setattr("ccr.cli.asyncio.run", fake_run)
    monkeypatch.setattr(server_mod, "serve", fake_serve)

    main(["serve"])

    assert captured.get("ran") is True


def test_pair_approve_requires_code() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["pair", "approve"])
    assert exc_info.value.code == 2


def test_module_help_via_subprocess() -> None:
    result = _run_module("--help")
    assert result.returncode == 0
    for expected in ("serve", "console", "pair", "doctor", "init-db"):
        assert expected in result.stdout


def test_module_unknown_subcommand_via_subprocess() -> None:
    result = _run_module("does-not-exist")
    assert result.returncode == 2
