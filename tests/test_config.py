"""Tests for :mod:`ccr.config` settings loading and validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from ccr.config import Settings

if TYPE_CHECKING:
    from pathlib import Path


VALID_SECRET = "x" * 64


def _write_env(tmp_path: Path, body: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


def _make_settings(env_file: Path) -> Settings:
    return Settings(_env_file=env_file)  # type: ignore[call-arg]


def test_settings_loads_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure no stray process env leaks into the test.
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "PUBLIC_URL",
        "JWT_SECRET",
        "WEB_PORT",
        "PROXY_PORT_ALLOWLIST",
        "TOKEN_TTL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)

    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=123:abc\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "WEB_PORT=9001\n"
        "TOKEN_TTL_SECONDS=600\n"
        "PROXY_PORT_ALLOWLIST=3000,5173\n",
    )
    settings = _make_settings(env)

    assert settings.telegram_bot_token.get_secret_value() == "123:abc"
    assert str(settings.public_url).rstrip("/") == "https://example.com"
    assert settings.jwt_secret.get_secret_value() == VALID_SECRET
    assert settings.web_port == 9001
    assert settings.token_ttl_seconds == 600
    assert settings.proxy_port_allowlist == {3000, 5173}


def test_settings_rejects_short_jwt_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\nPUBLIC_URL=https://example.com\nJWT_SECRET=short\n",
    )
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(env)
    assert "JWT_SECRET" in str(exc_info.value)


def test_settings_rejects_web_port_out_of_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WEB_PORT", raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "WEB_PORT=70000\n",
    )
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(env)
    assert "WEB_PORT" in str(exc_info.value)


def test_settings_rejects_token_ttl_too_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOKEN_TTL_SECONDS", raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "TOKEN_TTL_SECONDS=5\n",
    )
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(env)
    assert "TOKEN_TTL_SECONDS" in str(exc_info.value)


def test_settings_rejects_token_ttl_too_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOKEN_TTL_SECONDS", raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "TOKEN_TTL_SECONDS=999999\n",
    )
    with pytest.raises(ValidationError):
        _make_settings(env)


def test_blank_proxy_port_allowlist_yields_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PROXY_PORT_ALLOWLIST", raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "PROXY_PORT_ALLOWLIST=\n",
    )
    settings = _make_settings(env)
    assert settings.proxy_port_allowlist is None


def test_proxy_port_allowlist_accepts_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PROXY_PORT_ALLOWLIST", raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "PROXY_PORT_ALLOWLIST= 3000 , , 5173 \n",
    )
    settings = _make_settings(env)
    assert settings.proxy_port_allowlist == {3000, 5173}


def test_proxy_port_allowlist_rejects_non_integer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PROXY_PORT_ALLOWLIST", raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "PROXY_PORT_ALLOWLIST=abc\n",
    )
    with pytest.raises(ValidationError):
        _make_settings(env)


def test_settings_uses_defaults_when_optional_keys_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "WEB_HOST",
        "WEB_PORT",
        "TOKEN_TTL_SECONDS",
        "COOKIE_TTL_SECONDS",
        "DATA_DIR",
        "CLAUDE_BIN",
        "PROXY_PORT_ALLOWLIST",
        "LOG_LEVEL",
        "PERMISSION_MODE",
        "ALLOWED_TOOLS",
        "DISALLOWED_TOOLS",
    ):
        monkeypatch.delenv(key, raising=False)
    env = _write_env(
        tmp_path,
        f"TELEGRAM_BOT_TOKEN=t\nPUBLIC_URL=https://example.com\nJWT_SECRET={VALID_SECRET}\n",
    )
    settings = _make_settings(env)
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8765
    assert settings.token_ttl_seconds == 1800
    assert settings.cookie_ttl_seconds == 1800
    assert settings.claude_bin == "claude"
    assert settings.proxy_port_allowlist is None
    assert settings.log_level == "INFO"
    assert settings.permission_mode is None
    assert settings.allowed_tools == []
    assert settings.disallowed_tools == []


@pytest.mark.parametrize(
    "mode",
    ["default", "acceptEdits", "plan", "bypassPermissions"],
)
def test_permission_mode_accepts_valid_literals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.delenv("PERMISSION_MODE", raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        f"PERMISSION_MODE={mode}\n",
    )
    settings = _make_settings(env)
    assert settings.permission_mode == mode


def test_permission_mode_rejects_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PERMISSION_MODE", raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "PERMISSION_MODE=invalid\n",
    )
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(env)
    assert "permission_mode" in str(exc_info.value).lower()


def test_allowed_and_disallowed_tools_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("ALLOWED_TOOLS", "DISALLOWED_TOOLS"):
        monkeypatch.delenv(key, raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "ALLOWED_TOOLS=Read,Grep\n"
        "DISALLOWED_TOOLS=Write,Edit\n",
    )
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(env)
    assert "mutually exclusive" in str(exc_info.value).lower()


def test_default_tools_settings_produce_no_argv_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("PERMISSION_MODE", "ALLOWED_TOOLS", "DISALLOWED_TOOLS"):
        monkeypatch.delenv(key, raising=False)
    env = _write_env(
        tmp_path,
        f"TELEGRAM_BOT_TOKEN=t\nPUBLIC_URL=https://example.com\nJWT_SECRET={VALID_SECRET}\n",
    )
    settings = _make_settings(env)
    assert settings.permission_mode is None
    assert settings.allowed_tools == []
    assert settings.disallowed_tools == []


def test_tool_lists_parse_comma_separated_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("ALLOWED_TOOLS", "DISALLOWED_TOOLS"):
        monkeypatch.delenv(key, raising=False)
    env = _write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\n"
        "PUBLIC_URL=https://example.com\n"
        f"JWT_SECRET={VALID_SECRET}\n"
        "DISALLOWED_TOOLS= Write , Edit , Bash \n",
    )
    settings = _make_settings(env)
    assert settings.disallowed_tools == ["Write", "Edit", "Bash"]
    assert settings.allowed_tools == []
