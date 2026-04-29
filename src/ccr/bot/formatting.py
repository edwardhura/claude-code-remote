"""Convert :class:`ClaudeEvent` instances into Telegram-ready outbound messages.

Public surface (kept stable for CCR-009 which will refactor
:data:`OutboundMessage` into ``tuple[str, InlineKeyboardMarkup | None]``):

* :func:`chunk_text` — paragraph/sentence-aware splitter that never returns a
  chunk longer than :data:`TELEGRAM_HARD_LIMIT`.
* :func:`event_to_messages` — pattern-match a :class:`ClaudeEvent` to zero or
  more :data:`OutboundMessage` strings.

All user-supplied strings inserted into the outbound HTML are escaped with
:func:`html.escape` so a tool name like ``"<script>"`` or a free-form text
block cannot break Telegram's HTML parse mode (the bot is configured with
``parse_mode="HTML"`` by default).
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from ccr.claude.events import (
    AssistantTurn,
    PermissionRequest,
    ResultEvent,
    SystemInit,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UnknownEvent,
    UserTurn,
)

if TYPE_CHECKING:
    from ccr.claude.events import ClaudeEvent

# CCR-009 will widen this to ``tuple[str, InlineKeyboardMarkup | None]``.
type OutboundMessage = str

TELEGRAM_HARD_LIMIT = 4096
SAFE_CHUNK = 3500

_TOOL_ARG_TRUNCATE = 200
_TOOL_ERROR_TRUNCATE = 200

_ONE_MINUTE_MS = 60_000
_TOKEN_K_THRESHOLD = 10_000


def chunk_text(text: str) -> list[str]:
    """Split ``text`` into chunks no longer than :data:`SAFE_CHUNK` characters.

    Preference order: split on ``\\n\\n`` paragraph boundaries first, then on
    plain ``\\n`` line boundaries, then on ``". "`` sentence boundaries, and
    only as a last resort hard-cut at :data:`SAFE_CHUNK`. The function
    guarantees no chunk exceeds :data:`TELEGRAM_HARD_LIMIT`.
    """
    if not text:
        return []
    if len(text) <= SAFE_CHUNK:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= SAFE_CHUNK:
            chunks.append(remaining)
            break
        head, remaining = _split_once(remaining)
        chunks.append(head)
    # Belt-and-suspenders: enforce the hard cap regardless of the splitter.
    safe_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) <= TELEGRAM_HARD_LIMIT:
            safe_chunks.append(chunk)
            continue
        safe_chunks.extend(
            chunk[offset : offset + TELEGRAM_HARD_LIMIT]
            for offset in range(0, len(chunk), TELEGRAM_HARD_LIMIT)
        )
    return safe_chunks


def _split_once(text: str) -> tuple[str, str]:
    """Return ``(head, tail)`` where ``len(head) <= SAFE_CHUNK``.

    Tries paragraph (``\\n\\n``), line (``\\n``), and sentence (``". "``)
    boundaries inside the first :data:`SAFE_CHUNK` characters in that order.
    Falls back to a hard cut at :data:`SAFE_CHUNK` if no preferred boundary
    exists. The boundary characters are kept on the ``head`` side so visible
    paragraph / sentence shape is preserved.
    """
    window = text[:SAFE_CHUNK]
    for sep in ("\n\n", "\n", ". "):
        idx = window.rfind(sep)
        if idx > 0:
            cut = idx + len(sep)
            return text[:cut], text[cut:]
    return text[:SAFE_CHUNK], text[SAFE_CHUNK:]


def _format_tool_use(block: ToolUseBlock) -> OutboundMessage:
    name = html.escape(block.name)
    short_args = html.escape(repr(block.input)[:_TOOL_ARG_TRUNCATE])
    return f"\U0001f527 {name} {short_args}"


def _format_tool_result_error(block: ToolResultBlock) -> OutboundMessage | None:
    if not block.is_error:
        return None
    tool_use_id = html.escape(block.tool_use_id)
    body = html.escape(str(block.content)[:_TOOL_ERROR_TRUNCATE])
    return f"❌ {tool_use_id}: {body}"


def _format_assistant_turn(event: AssistantTurn) -> list[OutboundMessage]:
    out: list[OutboundMessage] = []
    for block in event.message.content:
        if isinstance(block, TextBlock):
            out.extend(html.escape(chunk) for chunk in chunk_text(block.text))
        elif isinstance(block, ThinkingBlock):
            out.extend(html.escape(chunk) for chunk in chunk_text(block.thinking))
        elif isinstance(block, ToolUseBlock):
            out.append(_format_tool_use(block))
        elif isinstance(block, ToolResultBlock):
            formatted = _format_tool_result_error(block)
            if formatted is not None:
                out.append(formatted)
    return out


def _format_duration(duration_ms: int | None) -> str:
    """Render ``duration_ms`` as ``"2.2s"`` or ``"1m 5s"``; ``"?"`` when missing."""
    if duration_ms is None:
        return "?"
    if duration_ms < _ONE_MINUTE_MS:
        return f"{duration_ms / 1000:.1f}s"
    minutes = duration_ms // _ONE_MINUTE_MS
    seconds = (duration_ms % _ONE_MINUTE_MS) // 1000
    return f"{minutes}m {seconds}s"


def _format_token_count(usage: object) -> str | None:
    """Render combined input+output token count: ``"42k"`` for ≥10_000, raw otherwise.

    Returns ``None`` when ``usage`` is missing so the caller can omit the
    segment entirely.
    """
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", 0)
    total = int(input_tokens) + int(output_tokens)
    if total <= 0:
        return None
    if total >= _TOKEN_K_THRESHOLD:
        return f"{total // 1000}k tokens"
    return f"{total} tokens"


def _format_result(event: ResultEvent) -> list[OutboundMessage]:
    if event.subtype == "success":
        parts: list[str] = ["✅ done", _format_duration(event.duration_ms)]
        token_segment = _format_token_count(event.usage)
        if token_segment is not None:
            parts.append(token_segment)
        return [" · ".join(parts)]
    return [f"❌ failed: {html.escape(event.subtype)}"]


def event_to_messages(event: ClaudeEvent) -> list[OutboundMessage]:
    """Render ``event`` into zero or more outbound Telegram messages.

    Mapping rules (see ticket CCR-008 / plan §6):

    * :class:`AssistantTurn` text/thinking blocks → chunked through
      :func:`chunk_text`.
    * :class:`AssistantTurn` tool-use → one-line summary
      ``"🔧 {name} {short_args}"`` with ``short_args`` truncated to
      ``200`` chars; the full tool input is never emitted.
    * :class:`AssistantTurn` tool-result → only emitted when
      ``is_error=True`` (``"❌ {tool_use_id}: {content[:200]}"``).
    * :class:`ResultEvent` ``subtype="success"`` →
      ``"✅ done · {duration} · {tokens}"``. ``duration`` is rendered as
      ``"2.2s"`` for sub-minute and ``"1m 5s"`` for ≥60s; missing duration
      becomes ``"?"``. ``tokens`` is the sum of ``usage.input_tokens`` and
      ``usage.output_tokens`` rendered as ``"{n // 1000}k tokens"`` for
      ``n >= 10_000`` and ``"{n} tokens"`` otherwise; the segment is omitted
      entirely when ``usage`` is missing or zero.
    * :class:`ResultEvent` non-success → ``"❌ failed: {subtype}"``.
    * :class:`PermissionRequest`, :class:`SystemInit`, :class:`UserTurn`,
      :class:`UnknownEvent` → ``[]``.
    """
    if isinstance(event, AssistantTurn):
        return _format_assistant_turn(event)
    if isinstance(event, ResultEvent):
        return _format_result(event)
    if isinstance(event, (PermissionRequest, SystemInit, UserTurn, UnknownEvent)):
        return []
    return []


__all__ = [
    "SAFE_CHUNK",
    "TELEGRAM_HARD_LIMIT",
    "OutboundMessage",
    "chunk_text",
    "event_to_messages",
]
