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


def _make_settings(
    tmp_path: Path,
    *,
    extra_args: str = "",
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
) -> Settings:
    return Settings(
        telegram_bot_token="dummy-token",  # type: ignore[arg-type]
        public_url="http://localhost",  # type: ignore[arg-type]
        jwt_secret="x" * 32,  # type: ignore[arg-type]
        data_dir=tmp_path,
        claude_bin=str(FAKE_CLAUDE),
        claude_extra_args=extra_args,
        subprocess_grace_kill_seconds=2,
        permission_mode=permission_mode,  # type: ignore[arg-type]
        allowed_tools=allowed_tools or [],
        disallowed_tools=disallowed_tools or [],
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


async def test_argv_permission_mode_inserted_between_resume_and_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", "")

    settings = _make_settings(
        tmp_path,
        extra_args="--my-extra-flag value",
        permission_mode="acceptEdits",
    )
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
    mode_idx = argv_lines.index("--permission-mode")
    perm_idx = argv_lines.index("--permission-prompt-tool")
    extra_idx = argv_lines.index("--my-extra-flag")

    assert argv_lines[mode_idx + 1] == "acceptEdits"
    # Order: --continue, --permission-mode, --permission-prompt-tool, user extras.
    assert cont_idx < mode_idx < perm_idx < extra_idx
    # User extras land last in argv.
    assert argv_lines[extra_idx + 1] == "value"
    assert extra_idx + 1 == len(argv_lines) - 1


async def test_argv_disallowed_tools_inserted_before_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", "")

    settings = _make_settings(tmp_path, disallowed_tools=["Write", "Edit"])
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
    dis_idx = argv_lines.index("--disallowed-tools")
    perm_idx = argv_lines.index("--permission-prompt-tool")
    assert argv_lines[dis_idx + 1] == "Write,Edit"
    assert dis_idx < perm_idx
    # No --allowed-tools flag in argv when only disallowed is set.
    assert "--allowed-tools" not in argv_lines


async def test_argv_allowed_tools_inserted_before_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", "")

    settings = _make_settings(tmp_path, allowed_tools=["Read", "Grep"])
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
    allow_idx = argv_lines.index("--allowed-tools")
    perm_idx = argv_lines.index("--permission-prompt-tool")
    assert argv_lines[allow_idx + 1] == "Read,Grep"
    assert allow_idx < perm_idx
    assert "--disallowed-tools" not in argv_lines


async def test_argv_defaults_unchanged_with_no_new_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", "")

    settings = _make_settings(tmp_path)
    proc = ClaudeProcess(settings=settings)
    await proc.start()
    await proc.wait()

    argv_lines = argv_file.read_text(encoding="utf-8").splitlines()
    assert "--permission-mode" not in argv_lines
    assert "--allowed-tools" not in argv_lines
    assert "--disallowed-tools" not in argv_lines
