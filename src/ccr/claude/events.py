"""Pydantic v2 discriminated-union schema for Claude Code stream-json events.

The schema is forward-compatible — every model carries
``model_config = ConfigDict(extra="allow")`` so unknown fields surface in
``model_extra`` rather than failing validation. The catch-all
:class:`UnknownEvent` (``type: str``) absorbs unrecognised ``type`` values
and validation failures via :func:`parse_event`.

Synthetic events (not from Claude Code's wire protocol) reuse the
:class:`UnknownEvent` shape:

* crash → ``UnknownEvent(type="error", raw={"reason", "exit_code", "stderr_tail"})``
* malformed JSON → ``UnknownEvent(type="parse_error", raw={"line": ...})``
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

# --- inner content blocks --------------------------------------------------- #


class TextBlock(BaseModel):
    """Plain-text assistant or user content."""

    model_config = ConfigDict(extra="allow")
    type: Literal["text"]
    text: str


class ThinkingBlock(BaseModel):
    """Chain-of-thought scratch content surfaced via stream-json."""

    model_config = ConfigDict(extra="allow")
    type: Literal["thinking"]
    thinking: str


class ToolUseBlock(BaseModel):
    """An assistant tool invocation."""

    model_config = ConfigDict(extra="allow")
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    """A tool result returned to the assistant.

    ``content`` may be a plain string, a list of structured chunks (each a
    free-form dict), or absent entirely — Claude Code's wire format varies.
    """

    model_config = ConfigDict(extra="allow")
    type: Literal["tool_result"]
    tool_use_id: str
    content: str | list[dict[str, Any]] | None = None
    is_error: bool = False


ContentBlock = Annotated[
    TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type"),
]


# --- outer events ----------------------------------------------------------- #


class _EventBase(BaseModel):
    """Shared config for outer event variants — forward-compatible."""

    model_config = ConfigDict(extra="allow")


class SystemInit(_EventBase):
    """The first event Claude Code emits per session."""

    type: Literal["system"]
    subtype: Literal["init"]
    session_id: str | None = None
    model: str | None = None
    tools: list[str] | None = None


class _UserMessage(BaseModel):
    """Inner ``message`` shape carried by :class:`UserTurn`."""

    model_config = ConfigDict(extra="allow")
    role: Literal["user"]
    content: str | list[ContentBlock]


class UserTurn(_EventBase):
    """A user-side message either being sent or echoed back."""

    type: Literal["user"]
    message: _UserMessage


class _AssistantMessage(BaseModel):
    """Inner ``message`` shape carried by :class:`AssistantTurn`."""

    model_config = ConfigDict(extra="allow")
    role: Literal["assistant"]
    content: list[ContentBlock]


class AssistantTurn(_EventBase):
    """An assistant turn made up of one or more :data:`ContentBlock`."""

    type: Literal["assistant"]
    message: _AssistantMessage


class ResultUsage(BaseModel):
    """Token-usage stats reported alongside a :class:`ResultEvent`.

    Forward-compatible: extra fields (e.g. ``server_tool_use``,
    ``service_tier``) surface through ``model_extra`` rather than failing
    validation.
    """

    model_config = ConfigDict(extra="allow")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ResultEvent(_EventBase):
    """Final per-turn outcome event from Claude Code."""

    type: Literal["result"]
    subtype: Literal["success", "error_during_execution"]
    duration_ms: int | None = None
    total_cost_usd: float | None = None
    usage: ResultUsage | None = None


class PermissionRequest(_EventBase):
    """A permission prompt awaiting an inline-button response."""

    type: Literal["permission_request"]
    request_id: str
    tool_use_id: str | None = None
    tool_name: str
    input: dict[str, Any] = Field(default_factory=dict)
    options: list[str]


class UnknownEvent(_EventBase):
    """Catch-all for unrecognised ``type`` values OR validation failures.

    ``type`` is a plain ``str`` (not a ``Literal``) — :class:`UnknownEvent`
    is *not* a member of the :data:`ClaudeEvent` discriminated union (which
    Pydantic v2 requires to use ``Literal``-typed discriminators). Instead,
    :func:`parse_event` falls back to :class:`UnknownEvent` whenever the
    discriminated-union validation fails. Synthetic crash and parse-error
    events reuse this shape.
    """

    type: str
    raw: dict[str, Any]


_KnownEvent = Annotated[
    SystemInit | UserTurn | AssistantTurn | ResultEvent | PermissionRequest,
    Field(discriminator="type"),
]

# ``ClaudeEvent`` is the public type for downstream consumers — it is the
# closed union of all five known variants plus the :class:`UnknownEvent`
# catch-all. Pydantic v2 requires discriminator fields to be ``Literal``,
# so :class:`UnknownEvent` (which has ``type: str``) cannot be part of a
# discriminated union; we fall back to a plain Union here. Validation goes
# through :func:`parse_event`, which uses ``_KnownEvent`` first and only
# constructs :class:`UnknownEvent` on validation failure.
ClaudeEvent = SystemInit | UserTurn | AssistantTurn | ResultEvent | PermissionRequest | UnknownEvent


_ADAPTER: TypeAdapter[Any] = TypeAdapter(_KnownEvent)

# Truncation limit for parse-error fallbacks so a runaway producer cannot
# bloat the JSONL with 10 MB lines.
_PARSE_ERROR_LINE_TRUNCATION = 4096


def _decode(line: str | bytes | dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort decode of one stream-json line.

    Returns the decoded mapping or ``None`` if the line cannot be turned
    into a JSON object (so the caller can emit ``parse_error``).
    """
    if isinstance(line, dict):
        return line
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def parse_event(line: str | bytes | dict[str, Any]) -> Any:
    """Parse a single stream-json line into a :data:`ClaudeEvent`.

    Falls back to :class:`UnknownEvent` on JSON-decode failure, schema
    mismatch, or unknown ``type``. Belt-and-suspenders: even if a *known*
    variant fails validation (forward-compat schema drift), the event still
    surfaces as :class:`UnknownEvent` rather than crashing the consumer.
    """
    if isinstance(line, (str, bytes)):
        truncated = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
        if len(truncated) > _PARSE_ERROR_LINE_TRUNCATION:
            truncated = truncated[:_PARSE_ERROR_LINE_TRUNCATION]
    else:
        truncated = ""

    obj = _decode(line)
    if obj is None:
        return UnknownEvent(type="parse_error", raw={"line": truncated})

    try:
        return _ADAPTER.validate_python(obj)
    except ValidationError:
        return UnknownEvent(type=str(obj.get("type", "unknown")), raw=obj)


__all__ = [
    "AssistantTurn",
    "ClaudeEvent",
    "ContentBlock",
    "PermissionRequest",
    "ResultEvent",
    "ResultUsage",
    "SystemInit",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "UnknownEvent",
    "UserTurn",
    "parse_event",
]
