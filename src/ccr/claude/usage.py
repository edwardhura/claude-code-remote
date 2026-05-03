"""Read-only JSONL aggregator for per-session usage / cost stats.

Walks a single ``data/logs/<session_id>.jsonl`` file and accumulates token
counts, tool-call counts, total elapsed time, and total cost across every
``result`` event encountered. The aggregator parses each line through
:func:`ccr.claude.events.parse_event` so malformed or unknown variants
fall through harmlessly as :class:`UnknownEvent` rather than raising.

Used by:

* :meth:`ccr.claude.manager.SessionManager.current_session_usage` — the
  live ``/cost`` enrichment path consumed by the bot's ``passthrough.py``.

This module performs **no** DB writes, opens **no** subprocesses, and does
**not** modify the JSONL file. It is safe to call concurrently with the
single :class:`~ccr.claude.log.JsonlSessionLog` writer because we open the
file in read-only mode and tolerate truncated trailing bytes (a file we
catch mid-append still parses cleanly through :func:`parse_event` because
the partial line decodes as a parse-error :class:`UnknownEvent`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ccr.claude.events import (
    AssistantTurn,
    ResultEvent,
    ToolUseBlock,
    parse_event,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class SessionUsage:
    """Aggregated usage stats for a single Claude session.

    All token / count fields default to ``0`` when no ``result`` event has
    been seen yet (e.g. a freshly-started session that has not produced a
    turn). ``elapsed_ms`` is the sum of every ``result.duration_ms`` (or
    ``result.duration_api_ms`` when the former is missing) so an aggregate
    over a multi-turn session reflects total wall time spent inside Claude.
    ``total_cost_usd`` mirrors :class:`~ccr.claude.events.ResultEvent`'s
    ``total_cost_usd`` summed across turns; rendered for transparency only,
    NOT a billing source of truth.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    tool_call_count: int = 0
    num_turns: int = 0
    elapsed_ms: int = 0
    total_cost_usd: float = 0.0


@dataclass(slots=True)
class _Accumulator:
    """Mutable scratch space for the aggregator's running totals."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    tool_call_count: int = 0
    num_turns: int = 0
    elapsed_ms: int = 0
    total_cost_usd: float = 0.0

    def freeze(self) -> SessionUsage:
        return SessionUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
            tool_call_count=self.tool_call_count,
            num_turns=self.num_turns,
            elapsed_ms=self.elapsed_ms,
            total_cost_usd=self.total_cost_usd,
        )


def _absorb_result(acc: _Accumulator, event: ResultEvent) -> None:
    """Fold one :class:`ResultEvent` into ``acc``."""
    if event.subtype == "success":
        acc.num_turns += 1
    if event.usage is not None:
        acc.input_tokens += int(event.usage.input_tokens)
        acc.output_tokens += int(event.usage.output_tokens)
        acc.cache_creation_input_tokens += int(event.usage.cache_creation_input_tokens)
        acc.cache_read_input_tokens += int(event.usage.cache_read_input_tokens)
    duration = event.duration_ms
    if duration is None:
        # ``duration_api_ms`` is a forward-compatible field surfaced via
        # ``model_extra``; fall back to it when ``duration_ms`` is absent.
        duration_api = (event.model_extra or {}).get("duration_api_ms")
        if isinstance(duration_api, int):
            duration = duration_api
    if duration is not None:
        acc.elapsed_ms += int(duration)
    if event.total_cost_usd is not None:
        acc.total_cost_usd += float(event.total_cost_usd)


def _absorb_assistant_turn(acc: _Accumulator, event: AssistantTurn) -> None:
    """Count :class:`ToolUseBlock` blocks across the turn into ``acc``."""
    for block in event.message.content:
        if isinstance(block, ToolUseBlock):
            acc.tool_call_count += 1


def aggregate_session_usage(jsonl_path: Path) -> SessionUsage:
    """Walk ``jsonl_path`` and return an aggregated :class:`SessionUsage`.

    Returns a zero-valued :class:`SessionUsage` if the file does not exist
    or is empty. Lines that fail to parse fall through as
    :class:`~ccr.claude.events.UnknownEvent` and are ignored — the
    aggregator is forward-compatible with schema drift.

    Field semantics:

    * ``input_tokens`` / ``output_tokens`` / ``cache_*`` — summed across
      every ``result`` event's ``usage`` field. Cache fields default to
      ``0`` when the underlying :class:`ResultUsage` lacks them.
    * ``tool_call_count`` — number of :class:`ToolUseBlock` blocks across
      every assistant turn. Tool *results* are not counted (one per use,
      not per round-trip).
    * ``num_turns`` — count of ``result`` events with ``subtype="success"``.
      Mirrors the user-visible "turn" — ``num_turns`` from the result event
      itself counts internal API turns and would over-report.
    * ``elapsed_ms`` — sum of ``duration_ms`` (or ``duration_api_ms`` when
      ``duration_ms`` is ``None``) across every ``result`` event.
    * ``total_cost_usd`` — sum of ``total_cost_usd`` across every ``result``
      event, with ``None`` treated as ``0.0``.
    """
    if not jsonl_path.exists():
        return SessionUsage()

    acc = _Accumulator()
    with jsonl_path.open("rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line.strip():
                continue
            event = parse_event(line)
            if isinstance(event, ResultEvent):
                _absorb_result(acc, event)
            elif isinstance(event, AssistantTurn):
                _absorb_assistant_turn(acc, event)
    return acc.freeze()


__all__ = ["SessionUsage", "aggregate_session_usage"]
