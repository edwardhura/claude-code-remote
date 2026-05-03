"""Unit tests for :mod:`ccr.claude.usage` — JSONL usage aggregator."""

from __future__ import annotations

import json
from pathlib import Path

from ccr.claude.usage import SessionUsage, aggregate_session_usage


def _write_jsonl(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def test_missing_file_returns_zero_usage(tmp_path: Path) -> None:
    """A non-existent JSONL path returns a zero-valued :class:`SessionUsage`."""
    result = aggregate_session_usage(tmp_path / "does-not-exist.jsonl")
    assert result == SessionUsage()


def test_empty_file_returns_zero_usage(tmp_path: Path) -> None:
    """An empty JSONL file returns a zero-valued :class:`SessionUsage`."""
    path = tmp_path / "empty.jsonl"
    path.touch()
    result = aggregate_session_usage(path)
    assert result == SessionUsage()


def test_single_result_event_extracts_usage_and_duration(tmp_path: Path) -> None:
    """One ``result`` event populates tokens, duration, cost, and turn count."""
    path = tmp_path / "single.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1500,
                "duration_api_ms": 2000,
                "total_cost_usd": 0.05,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 500,
                },
            },
        ],
    )

    result = aggregate_session_usage(path)
    assert result.input_tokens == 10
    assert result.output_tokens == 20
    assert result.cache_creation_input_tokens == 100
    assert result.cache_read_input_tokens == 500
    assert result.num_turns == 1
    assert result.elapsed_ms == 1500
    assert result.total_cost_usd == 0.05
    assert result.tool_call_count == 0


def test_multiple_result_events_accumulate(tmp_path: Path) -> None:
    """Two ``result`` events accumulate every field."""
    path = tmp_path / "multi.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1000,
                "total_cost_usd": 0.01,
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 200,
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 2000,
                "total_cost_usd": 0.02,
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 14,
                    "cache_creation_input_tokens": 70,
                    "cache_read_input_tokens": 300,
                },
            },
        ],
    )

    result = aggregate_session_usage(path)
    assert result.input_tokens == 12
    assert result.output_tokens == 24
    assert result.cache_creation_input_tokens == 120
    assert result.cache_read_input_tokens == 500
    assert result.num_turns == 2
    assert result.elapsed_ms == 3000
    assert abs(result.total_cost_usd - 0.03) < 1e-9


def test_tool_use_blocks_count_tool_calls(tmp_path: Path) -> None:
    """Every :class:`ToolUseBlock` across assistant turns increments ``tool_call_count``."""
    path = tmp_path / "tools.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_2",
                            "name": "Read",
                            "input": {"file_path": "/tmp/foo"},
                        },
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_3",
                            "name": "Edit",
                            "input": {"file_path": "/tmp/foo"},
                        },
                    ],
                },
            },
        ],
    )

    result = aggregate_session_usage(path)
    assert result.tool_call_count == 3


def test_text_blocks_do_not_count_as_tool_calls(tmp_path: Path) -> None:
    """Plain text blocks in assistant turns do NOT increment ``tool_call_count``."""
    path = tmp_path / "text.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {"type": "text", "text": "World"},
                    ],
                },
            },
        ],
    )

    result = aggregate_session_usage(path)
    assert result.tool_call_count == 0


def test_unknown_event_lines_are_ignored(tmp_path: Path) -> None:
    """Lines that fail to parse fall through as ``UnknownEvent`` and do not raise."""
    path = tmp_path / "mixed.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write(json.dumps({"type": "totally-unknown", "raw": {"foo": "bar"}}) + "\n")
        fh.write(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "duration_ms": 500,
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                },
            )
            + "\n",
        )

    result = aggregate_session_usage(path)
    assert result.num_turns == 1
    assert result.input_tokens == 1
    assert result.output_tokens == 2


def test_result_without_usage_field_zeroes_tokens(tmp_path: Path) -> None:
    """A ``result`` event with no ``usage`` still increments turn count and elapsed."""
    path = tmp_path / "no-usage.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 750,
            },
        ],
    )

    result = aggregate_session_usage(path)
    assert result.num_turns == 1
    assert result.elapsed_ms == 750
    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_falls_back_to_duration_api_ms_when_duration_ms_missing(tmp_path: Path) -> None:
    """``duration_api_ms`` is used when ``duration_ms`` is absent (forward-compat)."""
    path = tmp_path / "api-ms.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "result",
                "subtype": "success",
                "duration_api_ms": 1234,
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        ],
    )

    result = aggregate_session_usage(path)
    assert result.elapsed_ms == 1234


def test_error_subtype_does_not_count_as_turn(tmp_path: Path) -> None:
    """``subtype="error_during_execution"`` does NOT increment ``num_turns``."""
    path = tmp_path / "error.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "result",
                "subtype": "error_during_execution",
                "duration_ms": 500,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 100,
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        ],
    )

    result = aggregate_session_usage(path)
    # Only the success counts toward turns.
    assert result.num_turns == 1
    # Both contribute to tokens and elapsed.
    assert result.input_tokens == 2
    assert result.output_tokens == 2
    assert result.elapsed_ms == 600
