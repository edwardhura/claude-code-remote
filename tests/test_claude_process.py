"""Tests for ``ccr.claude.process.ClaudeProcess`` argv construction.

Covers the CCR-025 ``mcp_argv`` parameter: the MCP control flags are
spliced AFTER our resume flag and BEFORE the user-supplied
``claude_extra_args``. Drives the fake claude binary with
``FAKE_CLAUDE_ARGV_FILE`` so the constructed argv is observable as a
plain text file.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ccr.claude.process import ClaudeProcess
from ccr.config import Settings

if TYPE_CHECKING:
    import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = REPO_ROOT / "tests" / "fakes" / "fake_claude"


def _make_settings(tmp_path: Path, *, extra_args: str = "") -> Settings:
    return Settings(
        telegram_bot_token="dummy-token",  # type: ignore[arg-type]
        public_url="http://localhost",  # type: ignore[arg-type]
        jwt_secret="x" * 32,  # type: ignore[arg-type]
        data_dir=tmp_path,
        claude_bin=str(FAKE_CLAUDE),
        claude_extra_args=extra_args,
        subprocess_grace_kill_seconds=2,
    )


async def test_argv_includes_mcp_flags_when_mcp_argv_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", "")

    settings = _make_settings(tmp_path)
    proc = ClaudeProcess(
        settings=settings,
        mcp_argv=[
            "--permission-prompt-tool",
            "mcp__ccr__ccr_permission_prompt",
            "--mcp-config",
            "/tmp/x.json",
        ],
    )
    await proc.start()
    await proc.wait()

    argv_lines = argv_file.read_text(encoding="utf-8").splitlines()
    # Position assertions: control flags first, then MCP block, then no
    # user extras (none configured).
    assert "fake_claude" in argv_lines[0]
    assert "-p" in argv_lines
    assert "--input-format=stream-json" in argv_lines
    assert "--output-format=stream-json" in argv_lines
    assert "--verbose" in argv_lines
    perm_idx = argv_lines.index("--permission-prompt-tool")
    assert argv_lines[perm_idx + 1] == "mcp__ccr__ccr_permission_prompt"
    assert argv_lines[perm_idx + 2] == "--mcp-config"
    assert argv_lines[perm_idx + 3] == "/tmp/x.json"
    # All control flags appear BEFORE the MCP block.
    assert argv_lines.index("-p") < perm_idx
    assert argv_lines.index("--verbose") < perm_idx


async def test_argv_user_extra_args_still_appended_after_mcp_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", "")

    settings = _make_settings(tmp_path, extra_args="--my-extra-flag value")
    proc = ClaudeProcess(
        settings=settings,
        mcp_argv=[
            "--permission-prompt-tool",
            "mcp__ccr__ccr_permission_prompt",
            "--mcp-config",
            "/tmp/x.json",
        ],
    )
    await proc.start()
    await proc.wait()

    argv_lines = argv_file.read_text(encoding="utf-8").splitlines()
    perm_idx = argv_lines.index("--permission-prompt-tool")
    extra_idx = argv_lines.index("--my-extra-flag")
    assert extra_idx > perm_idx
    assert argv_lines[extra_idx + 1] == "value"
    # The user's tokens are last — nothing comes after them.
    assert extra_idx + 1 == len(argv_lines) - 1


async def test_argv_continue_appears_before_mcp_flags_when_resume_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", "")

    settings = _make_settings(tmp_path)
    proc = ClaudeProcess(
        settings=settings,
        resume=True,
        mcp_argv=[
            "--permission-prompt-tool",
            "mcp__ccr__ccr_permission_prompt",
            "--mcp-config",
            "/tmp/x.json",
        ],
    )
    await proc.start()
    await proc.wait()

    argv_lines = argv_file.read_text(encoding="utf-8").splitlines()
    cont_idx = argv_lines.index("--continue")
    perm_idx = argv_lines.index("--permission-prompt-tool")
    assert cont_idx < perm_idx
