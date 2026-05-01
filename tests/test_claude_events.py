"""Tests for the Pydantic discriminated-union event schema."""

from __future__ import annotations

import json

from pydantic import TypeAdapter

from ccr.claude.events import (
    AssistantTurn,
    ClaudeEvent,
    ResultEvent,
    SystemInit,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UnknownEvent,
    UserTurn,
    parse_event,
)


def test_round_trip_system_init() -> None:
    line = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "sess-abc",
            "model": "claude-3-5-sonnet",
            "tools": ["Read", "Bash"],
        }
    )
    event = parse_event(line)
    assert isinstance(event, SystemInit)
    assert event.session_id == "sess-abc"
    assert event.model == "claude-3-5-sonnet"
    assert event.tools == ["Read", "Bash"]


def test_round_trip_user_turn_string_content() -> None:
    line = json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": "hello"},
        }
    )
    event = parse_event(line)
    assert isinstance(event, UserTurn)
    assert event.message.content == "hello"


def test_round_trip_user_turn_block_content() -> None:
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}],
            },
        }
    )
    event = parse_event(line)
    assert isinstance(event, UserTurn)
    assert isinstance(event.message.content, list)
    assert isinstance(event.message.content[0], TextBlock)
    assert event.message.content[0].text == "hi"


def test_round_trip_assistant_turn_with_text_thinking_tool_use() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "text", "text": "answer"},
                    {
                        "type": "tool_use",
                        "id": "tu-1",
                        "name": "Read",
                        "input": {"path": "/tmp"},
                    },
                ],
            },
        }
    )
    event = parse_event(line)
    assert isinstance(event, AssistantTurn)
    blocks = event.message.content
    assert isinstance(blocks[0], ThinkingBlock)
    assert isinstance(blocks[1], TextBlock)
    assert isinstance(blocks[2], ToolUseBlock)
    assert blocks[2].name == "Read"
    assert blocks[2].input == {"path": "/tmp"}


def test_round_trip_result_success_and_error() -> None:
    success = parse_event(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1234,
                "total_cost_usd": 0.04,
            }
        )
    )
    assert isinstance(success, ResultEvent)
    assert success.subtype == "success"
    assert success.duration_ms == 1234

    error = parse_event(json.dumps({"type": "result", "subtype": "error_during_execution"}))
    assert isinstance(error, ResultEvent)
    assert error.subtype == "error_during_execution"


def test_unknown_type_falls_through_to_unknown_event() -> None:
    line = json.dumps({"type": "weather_report", "temp": 72})
    event = parse_event(line)
    assert isinstance(event, UnknownEvent)
    assert event.type == "weather_report"
    assert event.raw == {"type": "weather_report", "temp": 72}


def test_malformed_json_returns_unknown_event_parse_error() -> None:
    event = parse_event("not json at all {")
    assert isinstance(event, UnknownEvent)
    assert event.type == "parse_error"
    assert "line" in event.raw


def test_extra_fields_preserved_in_model_extra() -> None:
    line = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "extra_unknown": "future-field",
        }
    )
    event = parse_event(line)
    assert isinstance(event, SystemInit)
    assert event.model_extra == {"extra_unknown": "future-field"}


def test_json_schema_is_non_empty() -> None:
    schema = TypeAdapter(ClaudeEvent).json_schema()
    assert isinstance(schema, dict)
    assert len(schema) > 0


def test_known_variant_with_validation_failure_falls_through_to_unknown() -> None:
    """Belt-and-suspenders: forward-compat schema drift on a known type still
    surfaces as UnknownEvent rather than raising ValidationError."""
    line = json.dumps(
        {
            "type": "result",
            "subtype": "totally-new-subtype",  # not in the Literal set
        }
    )
    event = parse_event(line)
    assert isinstance(event, UnknownEvent)
    assert event.type == "result"
    assert event.raw["subtype"] == "totally-new-subtype"


def test_dict_input_accepted() -> None:
    obj = {"type": "system", "subtype": "init"}
    event = parse_event(obj)
    assert isinstance(event, SystemInit)
