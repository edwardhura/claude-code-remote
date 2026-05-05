"""Typed application settings loaded from environment / `.env`.

Field validators enforce the constraints documented in the project plan:

* ``JWT_SECRET`` must be at least 32 characters.
* ``WEB_PORT`` must be in the valid TCP range 1-65535.
* ``PROXY_PORT_ALLOWLIST`` is parsed from a comma-separated string into a
  ``set[int]``; a blank value yields ``None`` (meaning "any port").
* ``TOKEN_TTL_SECONDS`` must be between 60 seconds and 24 hours.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

JWT_SECRET_MIN_LENGTH = 32
WEB_PORT_MIN = 1
WEB_PORT_MAX = 65535
TOKEN_TTL_MIN = 60
TOKEN_TTL_MAX = 86400
MCP_TIMEOUT_MIN = 1
MCP_TIMEOUT_MAX = 3600


class Settings(BaseSettings):
    """Runtime configuration for the Claude Code Remote process.

    Values are loaded from environment variables (case-insensitive) and from a
    ``.env`` file in the current working directory if present. Unknown keys
    are ignored so that the project ``.env`` can carry forward-compatible
    extras without breaking the loader.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: SecretStr
    public_url: HttpUrl
    jwt_secret: SecretStr

    web_host: str = "127.0.0.1"
    web_port: int = 8765
    token_ttl_seconds: int = 1800
    cookie_ttl_seconds: int = 1800
    data_dir: Path = Path("./data")
    claude_bin: str = "claude"
    claude_extra_args: str = ""
    log_retention_count: int = 200
    log_retention_days: int = 30
    log_level: str = "INFO"
    pairing_code_ttl_seconds: int = 900
    subprocess_grace_kill_seconds: int = 5
    mcp_permission_timeout_seconds: int = 120
    ask_user_question_timeout_seconds: int = 600
    permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions"] | None = None
    allowed_tools: Annotated[list[str], NoDecode] = []
    disallowed_tools: Annotated[list[str], NoDecode] = []
    proxy_port_allowlist: set[int] | None = None

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_min_length(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret) < JWT_SECRET_MIN_LENGTH:
            message = (
                f"JWT_SECRET must be at least {JWT_SECRET_MIN_LENGTH} characters "
                f"(got {len(secret)})."
            )
            raise ValueError(message)
        return value

    @field_validator("web_port")
    @classmethod
    def _web_port_in_range(cls, value: int) -> int:
        if not WEB_PORT_MIN <= value <= WEB_PORT_MAX:
            message = f"WEB_PORT must be between {WEB_PORT_MIN} and {WEB_PORT_MAX} (got {value})."
            raise ValueError(message)
        return value

    @field_validator("token_ttl_seconds")
    @classmethod
    def _token_ttl_in_range(cls, value: int) -> int:
        if not TOKEN_TTL_MIN <= value <= TOKEN_TTL_MAX:
            message = (
                f"TOKEN_TTL_SECONDS must be between {TOKEN_TTL_MIN} and {TOKEN_TTL_MAX} "
                f"(got {value})."
            )
            raise ValueError(message)
        return value

    @field_validator("mcp_permission_timeout_seconds")
    @classmethod
    def _mcp_permission_timeout_in_range(cls, value: int) -> int:
        if not MCP_TIMEOUT_MIN <= value <= MCP_TIMEOUT_MAX:
            message = (
                f"MCP_PERMISSION_TIMEOUT_SECONDS must be between "
                f"{MCP_TIMEOUT_MIN} and {MCP_TIMEOUT_MAX} (got {value})."
            )
            raise ValueError(message)
        return value

    @field_validator("ask_user_question_timeout_seconds")
    @classmethod
    def _ask_user_question_timeout_in_range(cls, value: int) -> int:
        if not MCP_TIMEOUT_MIN <= value <= MCP_TIMEOUT_MAX:
            message = (
                f"ASK_USER_QUESTION_TIMEOUT_SECONDS must be between "
                f"{MCP_TIMEOUT_MIN} and {MCP_TIMEOUT_MAX} (got {value})."
            )
            raise ValueError(message)
        return value

    @field_validator("allowed_tools", "disallowed_tools", mode="before")
    @classmethod
    def _parse_tool_list(cls, value: Any) -> list[str]:
        """Parse a comma-separated string of tool names into a list.

        Mirrors the env-friendly handling for ``proxy_port_allowlist``: blank
        / whitespace-only values yield an empty list, already-parsed iterables
        are passed through, and individual entries are stripped.
        """
        if value is None:
            return []
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            return [token.strip() for token in stripped.split(",") if token.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        message = f"tool list must be a string or iterable, got {type(value).__name__}."
        raise TypeError(message)

    @model_validator(mode="after")
    def _tools_mutually_exclusive(self) -> Settings:
        if self.allowed_tools and self.disallowed_tools:
            message = "ALLOWED_TOOLS and DISALLOWED_TOOLS are mutually exclusive; set at most one."
            raise ValueError(message)
        return self

    @field_validator("proxy_port_allowlist", mode="before")
    @classmethod
    def _parse_port_allowlist(cls, value: Any) -> set[int] | None:
        """Parse a comma-separated string of ports into a set.

        Blank / whitespace-only values yield ``None`` (meaning "any port is
        allowed"). Already-parsed iterables are passed through unchanged. Any
        non-integer entry raises ``ValueError`` so misconfiguration fails fast.
        """
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            ports: set[int] = set()
            for raw in stripped.split(","):
                token = raw.strip()
                if not token:
                    continue
                try:
                    port = int(token)
                except ValueError as exc:
                    message = f"PROXY_PORT_ALLOWLIST entry {token!r} is not an integer."
                    raise ValueError(message) from exc
                if not WEB_PORT_MIN <= port <= WEB_PORT_MAX:
                    message = (
                        f"PROXY_PORT_ALLOWLIST entry {port} is outside "
                        f"{WEB_PORT_MIN}-{WEB_PORT_MAX}."
                    )
                    raise ValueError(message)
                ports.add(port)
            return ports or None
        if isinstance(value, (set, frozenset, list, tuple)):
            return {int(item) for item in value}
        message = f"PROXY_PORT_ALLOWLIST must be a string or iterable, got {type(value).__name__}."
        raise TypeError(message)


__all__ = ["Settings"]
