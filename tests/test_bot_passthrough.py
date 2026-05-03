"""Tests for ``ccr.bot.handlers.passthrough`` — slash-command whitelist."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject
from aiogram.types import Chat, Message, User

from ccr.bot.handlers.passthrough import cmd_passthrough
from ccr.claude.manager import NoActiveSessionError
from ccr.claude.usage import SessionUsage
from ccr.config import Settings

# --------------------------------------------------------------------------- #
# Helpers / stubs.
# --------------------------------------------------------------------------- #


def _make_message(*, text: str, user_id: int = 42, chat_id: int = 1000) -> Message:
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="A", username="alice"),
        text=text,
    )
    object.__setattr__(msg, "answer", AsyncMock())
    return msg


def _command(name: str, args: str = "") -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args or None)


def _make_fake_settings(tmp_path: Path) -> Settings:
    """Build a :class:`Settings` whose ``data_dir`` lives under ``tmp_path``.

    ``_list_library_agents`` reads from ``data_dir.parent / .claude/agents``,
    so this layout points the glob at ``tmp_path / .claude/agents``.
    """
    return Settings(
        telegram_bot_token="dummy-token",  # type: ignore[arg-type]
        public_url="http://localhost",  # type: ignore[arg-type]
        jwt_secret="x" * 32,  # type: ignore[arg-type]
        data_dir=tmp_path / "data",
    )


class FakeManager:
    """Stub :class:`SessionManager` exposing only the accessors passthrough needs."""

    def __init__(
        self,
        *,
        send_side_effect: Exception | None = None,
        running: list[str] | None = None,
        skills: list[str] | None = None,
        active: bool = True,
        usage: SessionUsage | None = None,
        session_id: uuid.UUID | None = None,
    ) -> None:
        self.send_slash = AsyncMock()
        if send_side_effect is not None:
            self.send_slash.side_effect = send_side_effect
        self._running = list(running) if running is not None else []
        self._skills = list(skills) if skills is not None else []
        self._active = active
        self._usage = usage
        self._session_id = session_id

    def running_subagents(self) -> list[str]:
        # Mirror SessionManager.running_subagents()'s contract: alphabetically
        # sorted, deduplicated.
        return sorted(set(self._running))

    def available_skills(self) -> list[str]:
        # Mirror SessionManager.available_skills()'s contract: alphabetically
        # sorted snapshot.
        return sorted(self._skills)

    def is_session_active(self) -> bool:
        return self._active

    def current_session_usage(self) -> SessionUsage | None:
        # Mirror SessionManager.current_session_usage(): None when idle.
        return self._usage

    @property
    def current_session_id(self) -> uuid.UUID | None:
        # Mirror SessionManager.current_session_id: None when idle.
        return self._session_id


# --------------------------------------------------------------------------- #
# Whitelist branch — /model and /compact still forward verbatim.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_model_calls_send_slash(tmp_path: Path) -> None:
    """``/model`` stays in WHITELIST → forwarded via ``send_slash``."""
    manager = FakeManager()
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/model")

    await cmd_passthrough(
        msg,
        command=_command("model"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_awaited_once_with("model", "")
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_calls_send_slash(tmp_path: Path) -> None:
    """``/compact`` stays in WHITELIST → forwarded via ``send_slash``."""
    manager = FakeManager()
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/compact")

    await cmd_passthrough(
        msg,
        command=_command("compact"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_awaited_once_with("compact", "")
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_no_session_returns_canned_string(tmp_path: Path) -> None:
    """``/model`` while idle → ``NoActiveSessionError`` is caught and replied politely."""
    manager = FakeManager(send_side_effect=NoActiveSessionError("No active session."))
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/model")

    await cmd_passthrough(
        msg,
        command=_command("model"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_awaited_once_with("model", "")
    msg.answer.assert_awaited_once_with("No active session.")


@pytest.mark.asyncio
async def test_model_with_args_passes_args_through(tmp_path: Path) -> None:
    """Arguments after the command are forwarded as-is to ``send_slash``."""
    manager = FakeManager()
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/model claude-3-opus")

    await cmd_passthrough(
        msg,
        command=_command("model", "claude-3-opus"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_awaited_once_with("model", "claude-3-opus")


# --------------------------------------------------------------------------- #
# /cost — locally-computed rich reply (no ``send_slash`` forward).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cost_renders_rich_reply_from_session_usage(tmp_path: Path) -> None:
    """``/cost`` answers locally with HTML labels for every aggregated field."""
    session_id = uuid.UUID("d562aef3-12ea-435b-a588-9aa4d8b39a29")
    usage = SessionUsage(
        input_tokens=12,
        output_tokens=158,
        cache_creation_input_tokens=10373,
        cache_read_input_tokens=42711,
        tool_call_count=2,
        num_turns=2,
        elapsed_ms=5077,
        total_cost_usd=0.0906,
    )
    manager = FakeManager(usage=usage, session_id=session_id, active=True)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/cost")

    await cmd_passthrough(
        msg,
        command=_command("cost"),
        session_manager=manager,
        settings=settings,
    )

    # /cost is no longer in WHITELIST — must NOT call send_slash.
    manager.send_slash.assert_not_awaited()
    text = _captured_text(msg)
    # First eight hex chars of the UUID are surfaced as the session id.
    assert "<b>Session:</b> d562aef3" in text
    assert "<b>Turns:</b> 2" in text
    assert "<b>Input tokens:</b> 12" in text
    assert "<b>Output tokens:</b> 158" in text
    assert "<b>Cache read tokens:</b> 42711" in text
    assert "<b>Tool calls:</b> 2" in text
    assert "<b>Elapsed:</b> 5.1s" in text
    assert "<b>Cost (USD):</b> $0.0906" in text


@pytest.mark.asyncio
async def test_cost_no_active_session_returns_canned_string(tmp_path: Path) -> None:
    """``/cost`` while idle → returns the canonical no-session string (CCR-010 regression)."""
    manager = FakeManager(usage=None, session_id=None, active=False)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/cost")

    await cmd_passthrough(
        msg,
        command=_command("cost"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_not_awaited()
    msg.answer.assert_awaited_once_with("No active session.")


@pytest.mark.asyncio
async def test_cost_omits_cost_line_when_total_cost_is_zero(tmp_path: Path) -> None:
    """Subscription users (``total_cost_usd == 0``) do not see a stray ``$0.0000`` line."""
    session_id = uuid.uuid4()
    usage = SessionUsage(
        input_tokens=6,
        output_tokens=15,
        num_turns=1,
        elapsed_ms=1500,
        total_cost_usd=0.0,
    )
    manager = FakeManager(usage=usage, session_id=session_id, active=True)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/cost")

    await cmd_passthrough(
        msg,
        command=_command("cost"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "Cost (USD)" not in text
    # Other fields still present.
    assert "<b>Input tokens:</b> 6" in text
    assert "<b>Output tokens:</b> 15" in text


def test_render_cost_reply_html_escapes_session_id() -> None:
    """Defensive: ``_render_cost_reply`` escapes its session-id input.

    UUIDs in production are always 32 hex chars, so this codepath is
    unreachable in real life. The test exists so a future refactor that
    swaps :class:`uuid.UUID` for a stringly-typed id still emits safe
    HTML — every interpolated value passes through ``html.escape``.
    """
    from ccr.bot.handlers.passthrough import _render_cost_reply

    text = _render_cost_reply(
        "<bad&id>extra-padding-here",
        SessionUsage(num_turns=0, elapsed_ms=0),
    )
    assert "&lt;bad&amp;id" in text
    # Raw metacharacters must not leak.
    assert "<bad&id>" not in text


@pytest.mark.asyncio
async def test_cost_renders_long_elapsed_in_minute_format(tmp_path: Path) -> None:
    """Elapsed >= 60s renders as ``"Nm Ms"``."""
    session_id = uuid.uuid4()
    usage = SessionUsage(num_turns=3, elapsed_ms=125_000)  # 2m 5s
    manager = FakeManager(usage=usage, session_id=session_id, active=True)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/cost")

    await cmd_passthrough(
        msg,
        command=_command("cost"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "<b>Elapsed:</b> 2m 5s" in text


@pytest.mark.asyncio
async def test_cost_does_not_call_send_slash(tmp_path: Path) -> None:
    """Regression: ``/cost`` is lifted out of WHITELIST and never forwards to Claude."""
    session_id = uuid.uuid4()
    usage = SessionUsage()
    manager = FakeManager(usage=usage, session_id=session_id, active=True)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/cost")

    await cmd_passthrough(
        msg,
        command=_command("cost"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Blocked-interactive branch.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_mcp_is_blocked(tmp_path: Path) -> None:
    """``/mcp`` is blocked-interactive."""
    manager = FakeManager()
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/mcp")

    await cmd_passthrough(
        msg,
        command=_command("mcp"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_not_awaited()
    msg.answer.assert_awaited_once_with(
        "Interactive command — run /mcp in your local Claude Code terminal.",
    )


@pytest.mark.asyncio
async def test_mcp_still_blocked_interactive(tmp_path: Path) -> None:
    """Regression: ``/mcp`` continues to return the canned interactive reply."""
    manager = FakeManager()
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/mcp")

    await cmd_passthrough(
        msg,
        command=_command("mcp"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_not_awaited()
    msg.answer.assert_awaited_once_with(
        "Interactive command — run /mcp in your local Claude Code terminal.",
    )


@pytest.mark.asyncio
async def test_init_still_blocked_interactive(tmp_path: Path) -> None:
    """``/init`` is blocked-interactive (regression coverage missing in CCR-010)."""
    manager = FakeManager()
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/init")

    await cmd_passthrough(
        msg,
        command=_command("init"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_not_awaited()
    msg.answer.assert_awaited_once_with(
        "Interactive command — run /init in your local Claude Code terminal.",
    )


# --------------------------------------------------------------------------- #
# Unknown branch.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_command_returns_usage(tmp_path: Path) -> None:
    """An unrecognised slash command produces the canonical usage hint."""
    manager = FakeManager()
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/totallyunknown")

    await cmd_passthrough(
        msg,
        command=_command("totallyunknown"),
        session_manager=manager,
        settings=settings,
    )

    manager.send_slash.assert_not_awaited()
    msg.answer.assert_awaited_once_with(
        "Unknown command. Whitelisted: /new /stop /clear /view /last /preview "
        "/cost /model /compact /who /agents /skills.",
    )


# --------------------------------------------------------------------------- #
# /agents — Running + Library snapshot.
# --------------------------------------------------------------------------- #


def _seed_agents_library(tmp_path: Path, names: list[str]) -> Path:
    """Create ``tmp_path/.claude/agents/`` with ``<name>.md`` files; return the dir."""
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (agents_dir / f"{name}.md").write_text("body\n", encoding="utf-8")
    return agents_dir


def _captured_text(msg: Message) -> str:
    """Pull the single text argument the handler passed to ``msg.answer``."""
    answer: AsyncMock = msg.answer  # type: ignore[assignment]
    answer.assert_awaited_once()
    args, _kwargs = answer.await_args  # type: ignore[misc]
    return str(args[0])


@pytest.mark.asyncio
async def test_agents_renders_running_and_library_sections(tmp_path: Path) -> None:
    """Both sections render with their headers and bullet points in alphabetical order."""
    manager = FakeManager(running=["python-developer", "architect"])
    _seed_agents_library(tmp_path, ["foo", "bar"])
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/agents")

    await cmd_passthrough(
        msg,
        command=_command("agents"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "<b>Running</b>" in text
    assert "<b>Library</b>" in text
    assert "• python-developer" in text
    assert "• architect" in text
    assert "• foo" in text
    assert "• bar" in text
    # Alphabetical inside each section.
    assert text.index("• architect") < text.index("• python-developer")
    assert text.index("• bar") < text.index("• foo")
    # No interactive blocked-string leakage.
    assert "Interactive command" not in text


@pytest.mark.asyncio
async def test_agents_empty_running_and_empty_library(tmp_path: Path) -> None:
    """Both empty → both sections render the literal ``(none)`` placeholder."""
    manager = FakeManager(running=[])
    # No agents dir at all.
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/agents")

    await cmd_passthrough(
        msg,
        command=_command("agents"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "<b>Running</b>\n(none)" in text
    assert "<b>Library</b>\n(none)" in text


@pytest.mark.asyncio
async def test_agents_only_library_populated(tmp_path: Path) -> None:
    """Empty running, populated library → Running shows ``(none)``, Library lists files."""
    manager = FakeManager(running=[])
    _seed_agents_library(tmp_path, ["alpha", "beta"])
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/agents")

    await cmd_passthrough(
        msg,
        command=_command("agents"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "<b>Running</b>\n(none)" in text
    assert "• alpha" in text
    assert "• beta" in text


@pytest.mark.asyncio
async def test_agents_only_running_populated(tmp_path: Path) -> None:
    """Populated running, no library dir → Library shows ``(none)``, Running lists subagent."""
    manager = FakeManager(running=["python-developer"])
    # Do NOT seed any agents directory.
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/agents")

    await cmd_passthrough(
        msg,
        command=_command("agents"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "• python-developer" in text
    assert "<b>Library</b>\n(none)" in text


@pytest.mark.asyncio
async def test_agents_html_escapes_name_special_chars(tmp_path: Path) -> None:
    """Names containing ``<``, ``>``, ``&`` are HTML-escaped before rendering."""
    manager = FakeManager(running=["a&b"])
    _seed_agents_library(tmp_path, ["<weird>"])
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/agents")

    await cmd_passthrough(
        msg,
        command=_command("agents"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "&lt;weird&gt;" in text
    assert "a&amp;b" in text
    # Raw special chars must not leak into the HTML payload.
    assert "<weird>" not in text
    # The literal "a&b" with an unescaped "&" must not appear.
    # We assert the "&" alone — a&b would be a&amp;b and "&" appears only in escapes.
    assert "• a&b" not in text


@pytest.mark.asyncio
async def test_agents_no_longer_returns_blocked_interactive_canned_string(
    tmp_path: Path,
) -> None:
    """``/agents`` must not return the old blocked-interactive canned string."""
    manager = FakeManager(running=[])
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/agents")

    await cmd_passthrough(
        msg,
        command=_command("agents"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "Interactive command — run /agents in your local Claude Code terminal." not in text


@pytest.mark.asyncio
async def test_agents_library_sorted_alphabetically(tmp_path: Path) -> None:
    """Library files render in alphabetical order regardless of filesystem order."""
    manager = FakeManager(running=[])
    _seed_agents_library(tmp_path, ["zeta", "alpha", "mike"])
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/agents")

    await cmd_passthrough(
        msg,
        command=_command("agents"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    i_alpha = text.index("• alpha")
    i_mike = text.index("• mike")
    i_zeta = text.index("• zeta")
    assert i_alpha < i_mike < i_zeta


@pytest.mark.asyncio
async def test_agents_library_skips_non_md_and_hidden(tmp_path: Path) -> None:
    """Non-``.md`` files, hidden files, and subdirectories are excluded from Library."""
    manager = FakeManager(running=[])
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "foo.md").write_text("body\n", encoding="utf-8")
    (agents_dir / "bar.txt").write_text("body\n", encoding="utf-8")
    (agents_dir / ".hidden.md").write_text("body\n", encoding="utf-8")
    (agents_dir / "subdir").mkdir(parents=True, exist_ok=True)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/agents")

    await cmd_passthrough(
        msg,
        command=_command("agents"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "• foo" in text
    assert "bar" not in text
    assert "hidden" not in text
    assert "subdir" not in text


# --------------------------------------------------------------------------- #
# /skills — list skills carried on the session/init event.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_skills_renders_alphabetical_bullet_list(tmp_path: Path) -> None:
    """``/skills`` with a non-empty list renders the header + each skill as a bullet."""
    manager = FakeManager(
        skills=["update-config", "debug", "simplify", "implement-ticket"],
        active=True,
    )
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/skills")

    await cmd_passthrough(
        msg,
        command=_command("skills"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "<b>Skills</b>" in text
    assert "• debug" in text
    assert "• implement-ticket" in text
    assert "• simplify" in text
    assert "• update-config" in text
    # Alphabetical.
    i_debug = text.index("• debug")
    i_impl = text.index("• implement-ticket")
    i_simp = text.index("• simplify")
    i_upd = text.index("• update-config")
    assert i_debug < i_impl < i_simp < i_upd


@pytest.mark.asyncio
async def test_skills_empty_with_active_session_renders_placeholder(tmp_path: Path) -> None:
    """``/skills`` with an active session and no skills renders the empty placeholder."""
    manager = FakeManager(skills=[], active=True)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/skills")

    await cmd_passthrough(
        msg,
        command=_command("skills"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert text == "<b>Skills</b>\n(none)"


@pytest.mark.asyncio
async def test_skills_no_active_session(tmp_path: Path) -> None:
    """``/skills`` with no active session returns the canonical idle string."""
    manager = FakeManager(skills=[], active=False)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/skills")

    await cmd_passthrough(
        msg,
        command=_command("skills"),
        session_manager=manager,
        settings=settings,
    )

    msg.answer.assert_awaited_once_with("No active session.")


@pytest.mark.asyncio
async def test_skills_html_escapes_special_chars(tmp_path: Path) -> None:
    """Skill names containing ``<``, ``>``, ``&`` are HTML-escaped before rendering."""
    manager = FakeManager(skills=["a&b", "<weird>"], active=True)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/skills")

    await cmd_passthrough(
        msg,
        command=_command("skills"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "&lt;weird&gt;" in text
    assert "a&amp;b" in text
    # Raw special chars must not appear in the rendered payload.
    assert "<weird>" not in text
    assert "• a&b" not in text


@pytest.mark.asyncio
async def test_skills_branch_fires_before_unknown_fallthrough(tmp_path: Path) -> None:
    """Regression: ``/skills`` is handled by its own branch, not the unknown hint."""
    manager = FakeManager(skills=["alpha"], active=True)
    settings = _make_fake_settings(tmp_path)
    msg = _make_message(text="/skills")

    await cmd_passthrough(
        msg,
        command=_command("skills"),
        session_manager=manager,
        settings=settings,
    )

    text = _captured_text(msg)
    assert "Unknown command" not in text
    assert "<b>Skills</b>" in text
